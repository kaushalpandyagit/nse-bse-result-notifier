"""
Global Macro Pulse (9:00 AM IST) & Live Geopolitical Shock Radar
================================================================
1. Sends a comprehensive global macro summary every morning at ~9:00 AM IST.
2. Polls global news RSS feeds every 15 mins for high-impact geopolitical triggers.
"""

import os
import re
import sys
import json
import time
import datetime
import logging
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
try:
    import yfinance as yf
except ImportError:
    yf = None
    print("WARNING: yfinance not installed. Macro pulse will fail without it.")

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
SCRIPT_TAG = "🌍 [global_macro_news.py]"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

POLL_INTERVAL_MINUTES = 15

# Memory Files
STATE_FILE = Path(__file__).parent / "macro_state.json"
SEEN_NEWS_FILE = Path(__file__).parent / "seen_news.json"

# News Triggers (Words that cause massive market volatility)
SHOCK_KEYWORDS = [
    "war", "assassinat", "missile", "nuclear", "strike", "attack", 
    "invasion", "invades", "terrorist", "geopolitical", "emergency", 
    "martial law", "coup", "bombing", "airstrike"
]

# RSS Feeds (Google News World + Business)
NEWS_FEEDS = [
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en"
]

# Tickers mapped exactly to your request
MACRO_TICKERS = {
    "🇺🇸 Nasdaq 100 Futures": "NQ=F",
    "🇺🇸 S&P 500 Futures": "ES=F",
    "🛢️ Brent Crude Oil": "BZ=F",
    "🪙 Spot Gold": "GC=F",
    "💱 USD / INR": "INR=X",
    "🇺🇸 US 10Y Bond Yield": "^TNX",
    "₿ Bitcoin": "BTC-USD",
    "🇯🇵 Japan (Nikkei 225)": "^N225",
    "🇨🇳 China (Shanghai)": "000001.SS",
    "🇰🇷 Korea (KOSPI)": "^KS11",
    "🇹🇼 Taiwan (TAIEX)": "^TWII",
    "🇸🇬 Singapore (STI)": "^STI"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("macro_news")

# ----------------------------------------------------------------------
# TELEGRAM & MEMORY
# ----------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN:
        log.error("Telegram credentials not set.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{SCRIPT_TAG}\n{text}",
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, data=payload, timeout=15)
        return True
    except Exception as e:
        log.error("Telegram send error: %s", e)
        return False

def load_json(filepath: Path, default):
    if filepath.exists():
        try:
            return json.loads(filepath.read_text())
        except Exception:
            pass
    return default

def save_json(filepath: Path, data):
    filepath.write_text(json.dumps(data))

def get_ist_now():
    if IST:
        return datetime.datetime.now(IST)
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)

# ----------------------------------------------------------------------
# 9:00 AM MACRO PULSE ENGINE
# ----------------------------------------------------------------------
def fetch_macros() -> str:
    if not yf:
        return "⚠️ yfinance not installed."
    
    lines = ["📊 <b>9:00 AM Global Macro Pulse</b>\n"]
    
    for name, ticker in MACRO_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            # Use fast_info or fallback to history to get live/previous close
            hist = t.history(period="2d")
            if len(hist) < 2:
                lines.append(f"• {name}: <i>No data</i>")
                continue
                
            prev_close = hist["Close"].iloc[-2]
            current = hist["Close"].iloc[-1]
            chg_pct = ((current - prev_close) / prev_close) * 100
            
            # Format nicely
            sign = "+" if chg_pct > 0 else ""
            color = "🟢" if chg_pct > 0 else "🔴" if chg_pct < 0 else "⚪"
            
            # If it's a yield, limit decimals differently
            if ticker == "^TNX":
                lines.append(f"{color} {name}: <b>{current:.3f}%</b> ({sign}{chg_pct:.2f}%)")
            elif ticker == "BTC-USD":
                lines.append(f"{color} {name}: <b>${current:,.0f}</b> ({sign}{chg_pct:.2f}%)")
            else:
                lines.append(f"{color} {name}: <b>{current:,.2f}</b> ({sign}{chg_pct:.2f}%)")
                
        except Exception as e:
            log.warning("Failed to fetch %s: %s", name, e)
            lines.append(f"• {name}: <i>Data error</i>")
            
    return "\n".join(lines)

def run_macro_pulse_if_needed():
    now = get_ist_now()
    # We want to run between 8:50 AM and 9:15 AM
    if TRUE;
        state = load_json(STATE_FILE, {})
        today_str = now.strftime("%Y-%m-%d")
        
        if state.get("last_pulse_date") != today_str:
            log.info("Triggering 9:00 AM Macro Pulse...")
            msg = fetch_macros()
            send_telegram_message(msg)
            
            state["last_pulse_date"] = today_str
            save_json(STATE_FILE, state)
            log.info("Macro Pulse dispatched.")

# ----------------------------------------------------------------------
# GEOPOLITICAL BREAKING NEWS ENGINE
# ----------------------------------------------------------------------
def check_breaking_news():
    seen = set(load_json(SEEN_NEWS_FILE, []))
    new_alerts = []
    
    for feed_url in NEWS_FEEDS:
        try:
            resp = requests.get(feed_url, timeout=10)
            if resp.status_code != 200:
                continue
                
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:15]: # Check top 15 news items per feed
                title = item.find("title").text
                link = item.find("link").text
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                
                # Fingerprint using URL to avoid duplicates
                if link in seen:
                    continue
                    
                # Check for shock keywords
                title_lower = title.lower()
                if any(re.search(rf"\b{kw}\b", title_lower) for kw in SHOCK_KEYWORDS):
                    new_alerts.append((title, link, pub_date))
                    seen.add(link)
                    
        except Exception as e:
            log.warning("News fetch failed for %s: %s", feed_url, e)
            
    if new_alerts:
        for title, link, date in new_alerts:
            msg = f"🚨 <b>BREAKING MACRO SHOCK</b> 🚨\n\n<b>{title}</b>\n\n🕐 {date}\n🔗 <a href='{link}'>Read Full Alert</a>"
            send_telegram_message(msg)
            time.sleep(1)
            
        # Trim memory to last 500 links
        save_json(SEEN_NEWS_FILE, list(seen)[-500:])

# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------
def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Global Macro & News Radar.")
    
    if one_shot:
        run_macro_pulse_if_needed()
        check_breaking_news()
        return

    while True:
        try:
            run_macro_pulse_if_needed()
            check_breaking_news()
        except Exception as e:
            log.exception("Error in main loop: %s", e)
            
        time.sleep(POLL_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
