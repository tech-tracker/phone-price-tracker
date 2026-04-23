"""Phone price tracker — scrapes Amazon.in by brand, alerts on price drops via Telegram."""

import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DROP_THRESHOLD_PCT = float(os.environ.get("DROP_THRESHOLD_PCT", "5"))
PAGES_PER_BRAND = int(os.environ.get("PAGES_PER_BRAND", "2"))

# Drop accessories — they leak into brand searches (e.g. OnePlus earbuds in oneplus search)
ACCESSORY_KEYWORDS = [
    "earbuds", "earbud", "buds", "headphone", "headphones", "earphone", "earphones",
    "power bank", "powerbank", "charger", "cable", "adapter",
    "case", "cover", "screen guard", "tempered glass", "protector",
    "watch", "smartwatch", "band ", "fitness band",
    "speaker", "soundbar",
    "stylo", "stylus", "pen ",
    "tab ", "tablet", "ipad",
    "tv ", "monitor",
    "router", "wifi",
]

STATE_FILE = Path(__file__).parent / "state.json"

# Brand → Amazon search query + title-match keywords (lowercase substring).
# Keywords prevent unrelated sponsored listings from polluting per-brand results.
BRANDS = {
    "samsung":  {"search": "samsung galaxy s",   "match": ["samsung", "galaxy"]},
    "apple":    {"search": "iphone",             "match": ["iphone", "apple"]},
    "xiaomi":   {"search": "redmi a4 15c",       "match": ["xiaomi", "redmi"]},
    "realme":   {"search": "realme p4",          "match": ["realme"]},
    "vivo":     {"search": "vivo t4 t5 y19 y31", "match": ["vivo"]},
    "oppo":     {"search": "oppo k13 k14 f31",   "match": ["oppo"]},
    "iqoo":     {"search": "iqoo z10 lite",      "match": ["iqoo"]},
    "motorola": {"search": "motorola g35 g57 g67 g96 edge fusion", "match": ["motorola", "moto "]},
    "pixel":    {"search": "google pixel 9 10",  "match": ["pixel"]},
}

# Per-brand model whitelist (regex patterns; lowercase match against title).
# Only products matching at least one pattern for their brand are kept.
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]
IMPERSONATE_PROFILES = ["chrome120", "chrome124", "chrome131"]


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }


BACKOFF_SCHEDULE = [2, 5, 10, 20]


def fetch(url, retries=4):
    for attempt in range(retries):
        try:
            r = cffi_requests.get(
                url,
                headers=get_headers(),
                timeout=30,
                impersonate=random.choice(IMPERSONATE_PROFILES),
            )
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
            print(f"[fetch] {url[:80]} status={r.status_code} size={len(r.text)}", file=sys.stderr)
        except Exception as e:
            print(f"[fetch] {url[:80]} attempt {attempt+1} failed: {e}", file=sys.stderr)
        wait = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
        time.sleep(wait + random.uniform(0, 2))
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


def scrape_brand(brand_key, search, match_keywords, pages):
    products = {}
    query = search.replace(" ", "+")
    for page in range(1, pages + 1):
        url = f"https://www.amazon.in/s?k={query}&page={page}"
        html = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select('div[data-component-type="s-search-result"]'):
            asin = card.get("data-asin")
            if not asin:
                continue
            price_whole = card.select_one(".a-price .a-price-whole")
            if not price_whole:
                continue
            # Title priority: img alt (full product name) > h2 span > aria-label
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
        time.sleep(random.uniform(3, 6))
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


def format_alert(a):
    title = a["title"][:120] + ("..." if len(a["title"]) > 120 else "")
    return (
        f"🔥 <b>{a['brand'].upper()} Price Drop</b>\n"
        f"{title}\n"
        f"<s>₹{a['old_price']:,}</s> → <b>₹{a['new_price']:,}</b> "
        f"(-{a['drop_pct']:.1f}%)\n"
        f"{a['url']}"
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


def prune_state(state):
    """Drop products from state whose brand is no longer tracked or whose title no longer matches the whitelist."""
    before = len(state.get("products", {}))
    state["products"] = {
        k: v for k, v in state.get("products", {}).items()
        if v.get("brand") in BRANDS and matches_model_whitelist(v.get("title", ""), v.get("brand"))
    }
    after = len(state["products"])
    if before != after:
        print(f"Pruned {before - after} stale products from state ({before} → {after})")


def main():
    state = load_state()
    prune_state(state)
    all_products = {}

    # Shuffle brand order so different brands get the "fresh slot" each run —
    # Amazon's 503 rate-limiting kicks in mid-run, so the brands scraped first
    # are most likely to succeed.
    brand_items = list(BRANDS.items())
    random.shuffle(brand_items)

    for brand_key, cfg in brand_items:
        print(f"Scraping {brand_key}...")
        items = scrape_brand(brand_key, cfg["search"], cfg["match"], PAGES_PER_BRAND)
        all_products.update(items)
        print(f"  → {len(items)} {brand_key} products")

    print(f"\nTotal: {len(all_products)} products across {len(BRANDS)} brands")

    alerts = detect_drops(all_products, state)
    print(f"Found {len(alerts)} price drops (threshold {DROP_THRESHOLD_PCT}%)")

    for alert in alerts:
        send_telegram(format_alert(alert))
        time.sleep(1)

    state["products"].update(all_products)
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
