"""Amazon.in price tracker — async parallel scraping on Termux (residential IP).

Moved off GitHub Actions because datacenter IPs get rate-limited; residential IP
from a phone gets full result sets and can run every 2 min instead of 15.

State at ~/.amazon_state.json. Affiliate URLs supported via AMAZON_AFFILIATE_TAG env var.
"""

import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import httpx
import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DROP_THRESHOLD_PCT = float(os.environ.get("DROP_THRESHOLD_PCT", "5"))
PAGES_PER_BRAND = int(os.environ.get("PAGES_PER_BRAND", "1"))
AMAZON_AFFILIATE_TAG = os.environ.get("AMAZON_AFFILIATE_TAG", "")
# Fire "back in stock" alert when a product reappears after this many hours absent.
BACK_IN_STOCK_HOURS = float(os.environ.get("BACK_IN_STOCK_HOURS", "6"))

STATE_FILE = Path.home() / ".amazon_state.json"

# Multi-search per brand: premium models (iPhone 17 Pro, S26 Ultra, Pixel 10 Pro XL) rarely
# appear in a generic brand search's top results. One narrow query per model family ensures
# coverage. Total ~25 searches run in parallel-async; ~10-15 sec full scan.
BRANDS = {
    "samsung":  {"searches": ["samsung galaxy s24", "samsung galaxy s25", "samsung galaxy s26"],
                 "match": ["samsung", "galaxy"]},
    "apple":    {"searches": ["iphone 15", "iphone 16", "iphone 17"],
                 "match": ["iphone", "apple"]},
    "xiaomi":   {"searches": ["redmi a4", "redmi 15c"],
                 "match": ["xiaomi", "redmi"]},
    "realme":   {"searches": ["realme p4"],
                 "match": ["realme"]},
    "vivo":     {"searches": ["vivo t4", "vivo t5x", "vivo y19", "vivo y31"],
                 "match": ["vivo"]},
    "oppo":     {"searches": ["oppo k13", "oppo k14x", "oppo f31"],
                 "match": ["oppo"]},
    "iqoo":     {"searches": ["iqoo z10 lite"],
                 "match": ["iqoo"]},
    "motorola": {"searches": ["motorola g35", "motorola g57", "motorola g67", "motorola g96",
                              "motorola edge 60 fusion", "motorola edge 70 fusion"],
                 "match": ["motorola", "moto "]},
    "pixel":    {"searches": ["google pixel 9", "google pixel 10"],
                 "match": ["pixel"]},
}

MODEL_WHITELIST = {
    "samsung":  [r"\bs2[456]\b", r"\bs2[456]\s*(fe|ultra|plus|\+)"],
    "apple":    [r"iphone\s*1[567]\b"],
    "xiaomi":   [r"redmi\s*a4\b", r"redmi\s*15c\b"],
    "realme":   [r"\bp4\b", r"p4\s*lite", r"\bp4x\b", r"p4\s*power"],
    "oppo":     [r"\bk13\b", r"\bk13x\b", r"\bk14x\b", r"\bf31\b"],
    "vivo":     [r"\by19e\b", r"\by19s\b", r"\by31\b", r"\bt4\b", r"t4\s*lite",
                 r"\bt4x\b", r"\bt4r\b", r"\bt5x\b"],
    "iqoo":     [r"z10\s*lite"],
    "motorola": [r"\bg35\b", r"\bg57\b", r"\bg67\b", r"\bg96\b", r"[67]0\s*fusion"],
    "pixel":    [r"pixel\s*9a?\b", r"pixel\s*10a?\b"],
}
_COMPILED_WHITELIST = {b: [re.compile(p) for p in pats] for b, pats in MODEL_WHITELIST.items()}

ACCESSORY_KEYWORDS = [
    "earbuds", "earbud", "buds", "headphone", "headphones", "earphone", "earphones",
    "power bank", "powerbank", "charger", "cable", "adapter",
    "case", "cover", "screen guard", "tempered glass", "protector",
    "watch", "smartwatch", "band ", "fitness band",
    "speaker", "soundbar", "stylo", "stylus",
    "tab ", "tablet", "ipad", "tv ", "monitor", "router",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }


async def fetch(client: httpx.AsyncClient, url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = await client.get(url, headers=get_headers(), timeout=30)
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
            print(f"[fetch] {url[:80]} status={r.status_code} size={len(r.text)}", file=sys.stderr)
        except Exception as e:
            print(f"[fetch] {url[:80]} attempt {attempt+1} failed: {e}", file=sys.stderr)
        await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return None


def parse_int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def title_matches_brand(title, keywords):
    t = f" {title.lower()} "
    return any(kw in t for kw in keywords)


def is_accessory(title):
    t = f" {title.lower()} "
    return any(kw in t for kw in ACCESSORY_KEYWORDS)


def matches_model_whitelist(title, brand_key):
    patterns = _COMPILED_WHITELIST.get(brand_key)
    if not patterns:
        return True
    t = title.lower()
    return any(p.search(t) for p in patterns)


async def scrape_brand(client, brand_key, searches, match_keywords, pages):
    products = {}
    for search in searches:
        query = search.replace(" ", "+")
        for page in range(1, pages + 1):
            url = f"https://www.amazon.in/s?k={query}&page={page}"
            html = await fetch(client, url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for card in soup.select('div[data-component-type="s-search-result"]'):
                asin = card.get("data-asin")
                if not asin or asin in products:
                    continue
                price_whole = card.select_one(".a-price .a-price-whole")
                if not price_whole:
                    continue
                img = card.select_one("img.s-image")
                title = (img.get("alt") if img else "") or ""
                if not title or len(title) < 15:
                    h2_span = card.select_one("h2 span") or card.select_one("h2 a span")
                    if h2_span:
                        title = h2_span.get_text(strip=True)
                if not title or len(title) < 10:
                    continue
                if not title_matches_brand(title, match_keywords):
                    continue
                if is_accessory(title):
                    continue
                if not matches_model_whitelist(title, brand_key):
                    continue
                price = parse_int(price_whole.get_text())
                if not price or price < 2000:
                    continue
                products[asin] = {
                    "brand": brand_key,
                    "title": title,
                    "price": price,
                    "url": f"https://www.amazon.in/dp/{asin}",
                }
    return products


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"products": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def prune_state(state):
    before = len(state.get("products", {}))
    state["products"] = {
        k: v for k, v in state.get("products", {}).items()
        if v.get("brand") in BRANDS and matches_model_whitelist(v.get("title", ""), v.get("brand"))
    }
    after = len(state["products"])
    if before != after:
        print(f"Pruned {before - after} stale products from state ({before} → {after})")


def detect_drops(new_products, state):
    alerts = []
    prev = state.get("products", {})
    for asin, info in new_products.items():
        old = prev.get(asin)
        if not old or info["price"] >= old["price"]:
            continue
        drop_pct = ((old["price"] - info["price"]) / old["price"]) * 100
        if drop_pct >= DROP_THRESHOLD_PCT:
            alerts.append({
                "brand": info.get("brand", "?"),
                "title": info["title"],
                "old_price": old["price"],
                "new_price": info["price"],
                "drop_pct": drop_pct,
                "url": info["url"],
            })
    return alerts


def detect_back_in_stock(new_products, state, now_ts):
    """Fire when a product reappears after >BACK_IN_STOCK_HOURS absence."""
    alerts = []
    prev = state.get("products", {})
    threshold = BACK_IN_STOCK_HOURS * 3600
    for asin, info in new_products.items():
        old = prev.get(asin)
        if not old:
            continue  # first time seen → baseline, no alert
        last_seen = old.get("lastSeenAt")
        if not last_seen:
            continue  # legacy entry without timestamp — skip first cycle
        if now_ts - last_seen > threshold:
            alerts.append({
                "brand": info.get("brand", "?"),
                "title": info["title"],
                "price": info["price"],
                "url": info["url"],
                "hours_absent": (now_ts - last_seen) / 3600,
            })
    return alerts


def with_affiliate(url: str) -> str:
    """Append Amazon Associates tag to amazon.in URL when AMAZON_AFFILIATE_TAG is set."""
    if not AMAZON_AFFILIATE_TAG:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={AMAZON_AFFILIATE_TAG}"


def format_alert(a):
    title = a["title"][:120] + ("..." if len(a["title"]) > 120 else "")
    url = with_affiliate(a["url"])
    return (
        f"🛒 <b>AMAZON {a['brand'].upper()} Drop</b>\n"
        f"{title}\n"
        f"<s>₹{a['old_price']:,}</s> → <b>₹{a['new_price']:,}</b> "
        f"(-{a['drop_pct']:.1f}%)\n"
        f"{url}"
    )


def format_back_in_stock(a):
    title = a["title"][:120] + ("..." if len(a["title"]) > 120 else "")
    url = with_affiliate(a["url"])
    hours = a["hours_absent"]
    absent = f"{hours:.0f}h" if hours < 24 else f"{hours / 24:.0f}d"
    return (
        f"🔁 <b>AMAZON {a['brand'].upper()} — Back in Stock</b>\n"
        f"{title}\n"
        f"<b>₹{a['price']:,}</b> (was out for {absent})\n"
        f"{url}"
    )


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=15)
        if not r.ok:
            print(f"[telegram] error {r.status_code}: {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[telegram] exception: {e}", file=sys.stderr)


async def main_async():
    t0 = time.time()
    state = load_state()
    prune_state(state)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [
            scrape_brand(client, k, c["searches"], c["match"], PAGES_PER_BRAND)
            for k, c in BRANDS.items()
        ]
        results = await asyncio.gather(*tasks)

    all_products = {}
    for items in results:
        all_products.update(items)

    for b in BRANDS:
        n = sum(1 for p in all_products.values() if p.get("brand") == b)
        print(f"  {b}: {n}")

    print(f"Total: {len(all_products)} products in {time.time() - t0:.1f}s")

    now_ts = int(time.time())

    back_in_stock = detect_back_in_stock(all_products, state, now_ts)
    print(f"Found {len(back_in_stock)} back-in-stock items (threshold {BACK_IN_STOCK_HOURS}h)")
    for alert in back_in_stock:
        send_telegram(format_back_in_stock(alert))

    drops = detect_drops(all_products, state)
    print(f"Found {len(drops)} price drops (threshold {DROP_THRESHOLD_PCT}%)")
    for alert in drops:
        send_telegram(format_alert(alert))

    # Stamp every currently-seen product with the timestamp so we can detect future absences.
    for asin in all_products:
        all_products[asin]["lastSeenAt"] = now_ts
    state["products"].update(all_products)
    save_state(state)
    print("Done.")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
