"""Phone price tracker — scrapes Amazon.in + Flipkart, alerts on price drops via Telegram."""

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
AMAZON_PAGES = int(os.environ.get("AMAZON_PAGES", "3"))
FLIPKART_PAGES = int(os.environ.get("FLIPKART_PAGES", "3"))

STATE_FILE = Path(__file__).parent / "state.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
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


IMPERSONATE_PROFILES = ["chrome120", "chrome124", "chrome131"]


def fetch(url, retries=2):
    for attempt in range(retries + 1):
        try:
            r = cffi_requests.get(
                url,
                headers=get_headers(),
                timeout=30,
                impersonate=random.choice(IMPERSONATE_PROFILES),
            )
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
            print(f"[fetch] {url} status={r.status_code} size={len(r.text)}", file=sys.stderr)
        except Exception as e:
            print(f"[fetch] {url} attempt {attempt+1} failed: {e}", file=sys.stderr)
        time.sleep(random.uniform(2, 5))
    return None


def parse_int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def scrape_amazon(pages=3):
    products = {}
    for page in range(1, pages + 1):
        url = f"https://www.amazon.in/s?k=android+mobile+phone&page={page}"
        html = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('div[data-component-type="s-search-result"]')
        for card in cards:
            asin = card.get("data-asin")
            if not asin:
                continue
            title_el = card.select_one("h2 span") or card.select_one("h2 a span")
            price_whole = card.select_one(".a-price .a-price-whole")
            if not title_el or not price_whole:
                continue
            price = parse_int(price_whole.get_text())
            if not price or price < 2000:
                continue
            products[asin] = {
                "title": title_el.get_text(strip=True),
                "price": price,
                "url": f"https://www.amazon.in/dp/{asin}",
            }
        time.sleep(random.uniform(2, 4))
    return products


def scrape_flipkart(pages=3):
    products = {}
    for page in range(1, pages + 1):
        url = f"https://www.flipkart.com/search?q=android+mobile&page={page}"
        html = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select("div[data-id]"):
            pid = card.get("data-id", "")
            if not pid or not pid.startswith("MOB") or pid in products:
                continue
            title_el = card.select_one(".RG5Slk, .KzDlHZ, .wjcEIp, ._4rR01T, .s1Q9rs")
            price_el = card.select_one(".hZ3P6w, .HZ0E6r, .Nx9bqj, ._30jeq3")
            link_el = card.select_one("a[href*='/p/']")
            if not (title_el and price_el and link_el):
                continue
            price = parse_int(price_el.get_text())
            if not price or price < 2000:
                continue
            href = link_el.get("href", "")
            full_url = href if href.startswith("http") else f"https://www.flipkart.com{href}"
            products[pid] = {
                "title": title_el.get_text(strip=True),
                "price": price,
                "url": full_url.split("&lid=")[0],
            }
        time.sleep(random.uniform(2, 4))
    return products


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"amazon": {}, "flipkart": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def detect_drops(site, new_products, state):
    alerts = []
    prev = state.get(site, {})
    for pid, info in new_products.items():
        old = prev.get(pid)
        if not old:
            continue
        if info["price"] >= old["price"]:
            continue
        drop_pct = ((old["price"] - info["price"]) / old["price"]) * 100
        if drop_pct >= DROP_THRESHOLD_PCT:
            alerts.append({
                "site": site,
                "title": info["title"],
                "old_price": old["price"],
                "new_price": info["price"],
                "drop_pct": drop_pct,
                "url": info["url"],
            })
    return alerts


def format_alert(a):
    site_emoji = "🛒" if a["site"] == "amazon" else "🛍️"
    title = a["title"][:120] + ("..." if len(a["title"]) > 120 else "")
    return (
        f"{site_emoji} <b>{a['site'].upper()} Price Drop</b>\n"
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


def main():
    state = load_state()

    print("Scraping Amazon...")
    amazon = scrape_amazon(AMAZON_PAGES)
    print(f"  → {len(amazon)} products")

    print("Scraping Flipkart...")
    flipkart = scrape_flipkart(FLIPKART_PAGES)
    print(f"  → {len(flipkart)} products")

    alerts = detect_drops("amazon", amazon, state) + detect_drops("flipkart", flipkart, state)
    print(f"Found {len(alerts)} price drops (threshold {DROP_THRESHOLD_PCT}%)")

    for alert in alerts:
        send_telegram(format_alert(alert))
        time.sleep(1)

    state["amazon"].update(amazon)
    state["flipkart"].update(flipkart)
    save_state(state)

    print("Done.")


if __name__ == "__main__":
    main()