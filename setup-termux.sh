#!/data/data/com.termux/files/usr/bin/bash
# One-shot Termux installer for the phone price tracker.
# Usage:
#   TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy bash <(curl -fsSL \
#     https://raw.githubusercontent.com/tech-tracker/phone-price-tracker/main/setup-termux.sh)

set -e

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars before running this script." >&2
    echo "" >&2
    echo "Example:" >&2
    echo '  TELEGRAM_BOT_TOKEN="123:ABC" TELEGRAM_CHAT_ID="-100..." bash <(curl -fsSL https://raw.githubusercontent.com/tech-tracker/phone-price-tracker/main/setup-termux.sh)' >&2
    exit 1
fi

AMAZON_AFFILIATE_TAG="${AMAZON_AFFILIATE_TAG:-}"
FLIPKART_AFFILIATE_TAG="${FLIPKART_AFFILIATE_TAG:-}"

echo "==> [1/7] Updating package index + installing system packages..."
pkg update -y >/dev/null
pkg install -y python git cronie termux-services termux-api

echo "==> [2/7] Installing Python deps..."
pip install --quiet requests beautifulsoup4 httpx

echo "==> [3/7] Cloning tracker repo..."
if [ ! -d "$HOME/phone-price-tracker" ]; then
    git clone https://github.com/tech-tracker/phone-price-tracker.git "$HOME/phone-price-tracker"
else
    git -C "$HOME/phone-price-tracker" pull -q
fi

echo "==> [4/7] Creating runner script..."
cat > "$HOME/run_tracker.sh" << EOF
#!/data/data/com.termux/files/usr/bin/bash
export TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
export TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID"
export AMAZON_AFFILIATE_TAG="$AMAZON_AFFILIATE_TAG"
export FLIPKART_AFFILIATE_TAG="$FLIPKART_AFFILIATE_TAG"
cd ~/phone-price-tracker
git pull -q origin main
{
  echo "=== \$(date) ==="
  python amazon_scraper.py
  python flipkart_scraper.py
} >> ~/tracker.log 2>&1
EOF
chmod +x "$HOME/run_tracker.sh"

echo "==> [5/7] Registering crond service (manual fallback if needed)..."
if [ ! -d "$PREFIX/var/service/crond" ]; then
    mkdir -p "$PREFIX/var/service/crond/log"
    ln -sf "$PREFIX/share/termux-services/svlogger" "$PREFIX/var/service/crond/log/run"
    cat > "$PREFIX/var/service/crond/run" << 'CRONEOF'
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
exec crond -n -m off
CRONEOF
    chmod +x "$PREFIX/var/service/crond/run"
fi
sv-enable crond >/dev/null 2>&1 || true
sv up crond 2>/dev/null || true

echo "==> [6/7] Adding 2-min cron entry..."
echo "*/2 * * * * $HOME/run_tracker.sh" | crontab -

echo "==> [7/7] Setting up boot autostart + wakelock..."
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-cron.sh" << 'BOOTEOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sv up crond
BOOTEOF
chmod +x "$HOME/.termux/boot/start-cron.sh"
termux-wake-lock 2>/dev/null || true

echo ""
echo "✅ Setup complete!"
echo ""
echo "Sanity check:"
echo "  ~/run_tracker.sh && tail -20 ~/tracker.log"
echo ""
echo "⚠️  Final manual step — in Android Settings → Apps:"
echo "   Set Battery to 'Unrestricted' for Termux, Termux:Boot, Termux:API"
echo "   Otherwise Android will kill cron after a few hours."
