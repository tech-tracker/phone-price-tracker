# Phone Price Tracker

Scrapes Amazon.in + Flipkart listing pages for Android phones every 15 minutes via GitHub Actions. Sends a Telegram alert when a product's price drops by ≥ 5% from the last-seen price.

## How it works

- `scraper.py` — scrapes N pages from each site, compares to `state.json`, sends Telegram alerts on drops, writes new state.
- `.github/workflows/scrape.yml` — runs every 15 min, commits updated `state.json` back to the repo.
- State persists in the repo itself (no external DB needed).

## Setup

### 1. Create GitHub repo

```bash
cd c:/Shiv/phone-price-tracker
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<your-username>/phone-price-tracker.git
git push -u origin main
```

### 2. Add secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — your Telegram chat ID

### 3. Enable Actions

Repo → **Actions** tab → enable workflows if prompted. First run happens within ~15 min, or trigger manually via **Run workflow** on the `Scrape phone prices` workflow.

## Configuration

Tweak env vars in `.github/workflows/scrape.yml`:

| Var | Default | Meaning |
|-----|---------|---------|
| `DROP_THRESHOLD_PCT` | 5 | Minimum % drop to trigger alert |
| `AMAZON_PAGES` | 3 | Listing pages to scrape |
| `FLIPKART_PAGES` | 3 | Listing pages to scrape |

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
python scraper.py
```

## Notes

- GitHub Actions cron is best-effort — can be delayed 5-15 min under load.
- First run seeds `state.json` with current prices — no alerts will fire until the next run sees a lower price.
- Amazon/Flipkart occasionally return empty or blocked pages; the scraper skips those silently and tries again next cycle.
