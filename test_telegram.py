"""
Standalone Telegram connection test.
Run this on its own to confirm your bot token + chat ID actually work,
independent of NSE/BSE data fetching.

Usage:
    python3 test_telegram.py
"""

import os
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8974222959:AAG7S_dPYmDXBOX_ZnDWXMEenwqrmygkC-4")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1689560854")

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
payload = {
    "chat_id": TELEGRAM_CHAT_ID,
    "text": "✅ Test message from result_notifier.py -- if you see this, Telegram is configured correctly!",
}

resp = requests.post(url, data=payload, timeout=15)
print("Status code:", resp.status_code)
print("Response body:", resp.text)

if resp.status_code == 200:
    print("\nSUCCESS -- check your Telegram now.")
else:
    print("\nFAILED -- see the response body above for the exact error Telegram gave.")
