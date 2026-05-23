#!/usr/bin/env bash
# Crypto Range Monitor - Linux VPS setup.
# Creates a virtualenv, installs deps, captures Telegram credentials into .env,
# sends a live Telegram test message, then runs a one-off console scan.
#
#   chmod +x setup.sh && ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Crypto Range Monitor setup (Linux) ==="

# 1) Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it first, e.g.:" >&2
    echo "    sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl" >&2
    exit 1
fi
python3 --version

# 2) Virtualenv + dependency (avoids the system 'externally-managed' pip block)
if [ ! -d .venv ]; then
    echo "Creating virtualenv (.venv)..."
    python3 -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade pip --quiet
./.venv/bin/python -m pip install requests --quiet
echo "Dependencies installed."

# 3) Credentials -> .env (kept local, never committed)
if [ ! -f .env ]; then
    echo
    echo "Enter your Telegram credentials (saved only in .env on this machine):"
    read -rp "TELEGRAM_BOT_TOKEN: " TOKEN
    read -rp "TELEGRAM_CHAT_ID: " CHAT
    umask 077
    printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$TOKEN" "$CHAT" > .env
    echo ".env created (permissions 600)."
else
    echo ".env already exists - leaving it unchanged."
fi

# 4) Live Telegram check (confirms credentials immediately)
TOKEN=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env | head -n1)
CHAT=$(sed -n 's/^TELEGRAM_CHAT_ID=//p' .env | head -n1)
if [ -n "${TOKEN}" ] && [ -n "${CHAT}" ] && command -v curl >/dev/null 2>&1; then
    echo
    echo "Sending a Telegram test message..."
    if curl -fsS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${CHAT}" \
            --data-urlencode "text=Crypto Range Monitor: setup test OK." >/dev/null; then
        echo "Sent - check your Telegram chat."
    else
        echo "Telegram test FAILED - double-check the token/chat id in .env" >&2
    fi
fi

# 5) Console test scan (--test never sends Telegram alerts)
echo
echo "Running a one-off scan (--test)..."
./.venv/bin/python crypto_range_monitor.py --test

echo
echo "Done. To run it 24/7 with auto-restart on reboot:"
echo "    sudo ./install_service.sh"
