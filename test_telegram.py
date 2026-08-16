"""
Standalone Telegram connection test.
Run this on its own to confirm your bot token + chat ID actually work,
independent of NSE/BSE data fetching.

Usage:
    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment
    variables first (never hardcode them here -- see note below),
    then:
        python3 test_telegram.py
"""

import os
import sys
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print(
        "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID are not set as "
        "environment variables.\n"
        "Set them before running this script, e.g.:\n"
        "  export TELEGRAM_BOT_TOKEN=your_token_here\n"
        "  export TELEGRAM_CHAT_ID=your_chat_id_here\n"
        "  python3 test_telegram.py\n\n"
        "Never hardcode real token/chat-id values directly in this "
        "file -- this repo is public, and anything committed here "
        "is visible to everyone."
    )
    sys.exit(1)

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
payload = {
    "chat_id": TELEGRAM_CHAT_ID,
    "text": "\u2705 Test message from result_notifier.py -- if you see this, Telegram is configured correctly!",
}

resp = requests.post(url, data=payload, timeout=15)
print("Status code:", resp.status_code)
print("Response body:", resp.text)

if resp.status_code == 200:
    print("\nSUCCESS -- check your Telegram now.")
else:
    print("\nFAILED -- see the response body above for the exact error Telegram gave.")
