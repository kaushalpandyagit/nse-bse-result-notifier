"""
Global Macro & Geopolitical News Radar
================================================================
File: global_macro_news.py

Features:
  1. 9:00 AM IST Macro Pulse (US Futures, 10Y Yield, DXY, Crude, Gold, BTC).
  2. 7-Day Economic Event Calendar Tracker (RBI, Fed, CPI).
  3. Live Geopolitical & Global Bellwether News Polling.
  4. USFDA, PLI Scheme, and Tariff/Duty News Tracking.
"""

import os
import re
import sys
import json
import time
import logging
import datetime
from pathlib import Path
import html

import requests

try:
    import yfinance as yf
except ImportError:
    yf = None
    print("WARNING: yfinance not installed. Macro data will be limited.")

try:
    import feedparser
except ImportError:
    feedparser = None
    print("WARNING: feedparser not installed. News fetching will be limited.")

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
SCRIPT_TAG = "🤖 [global_macro_news.py]"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

POLL_INTERVAL_MINUTES = 15

STATE_FILE = Path(__file__).parent / "global_macro_state.json"
LOG_FILE = Path(__file__).parent / "global_macro.log"

MACRO_SYMBOLS = {
    "S&P 500 Futures": "ES=F",
    "Nasdaq 100 Futures": "NQ=F",
    "Dollar Index (DXY)": "DX-Y.NYB",
    "US 10Y Bond Yield": "^TNX",
    "Brent Crude": "BZ=F",
    "Spot Gold": "GC=F",
    "USD / INR": "INR=X",
    "Nikkei 225": "^N225",
    "Shanghai Comp": "000001.SS"
}

# ----------------------------------------------------------------------
# 7-DAY MACRO EVENT CALENDAR (RESTORED)
# ----------------------------------------------------------------------
MACRO_EVENTS_SCHEDULE = [
    # Format: ("YYYY-MM-DD", "Event Description")
    ("2026-10-07", "🇮🇳 RBI MPC Interest Rate Decision"),
    ("2026-10-08", "🇺🇸 US FOMC Meeting Minutes"),
    ("2026-10-13", "🇺🇸 US CPI (Inflation Data)"),
    ("2026-10-28", "🇺🇸 US Fed FOMC Interest Rate Decision"),
    ("2026-11-06", "🇺🇸 US Non-Farm Payrolls (NFP)"),
    ("2026-11-12", "🇮🇳 India CPI Inflation Data"),
    ("2026-11-13", "🇺🇸 US CPI (Inflation Data)"),
    ("2026-12-04", "🇮🇳 RBI MPC Interest Rate Decision"),
    ("2026-12-09", "🇺🇸 US Fed FOMC Interest Rate Decision"),
    ("2026-12-11", "🇺🇸 US CPI (Inflation Data)"),
    ("2027-01-27", "🇺🇸 US Fed FOMC Interest Rate Decision"),
    ("2027-02-05", "🇮🇳 RBI MPC Interest Rate Decision"),
]

# RSS feeds for Global Macro, USFDA, and Market News
RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?id=10000664",
    "https://feeds.bloomberg.com/markets/news.rss"
]

# Keywords to filter breaking impact news
BELLWETHER_KEYWORDS = ["nvidia", "apple", "tesla", "microsoft", "tsmc", "guidance", "revenue booms", "earnings", "ai demand"]
GEOPOLITICAL_KEYWORDS = ["war", "strike", "missile", "sanctions", "geopolitical", "opec", "fed", "federal reserve", "rate cut", "rbi"]
REGULATORY_KEYWORDS = ["usfda", "fda", "pli scheme", "tariff", "duty", "import tax", "export ban", "anti-dumping"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("global_macro")

# ----------------------------------------------------------------------
# TELEGRAM DISPATCHER
# ----------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram credentials not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{SCRIPT_TAG}\n{text}",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        return resp.status_code == 200
    except requests.RequestException as e:
        log.error("Telegram send error: %s", e)
        return False

# ----------------------------------------------------------------------
# UTILITIES
# ----------------------------------------------------------------------
def get_ist_now():
    """Forces IST timezone (UTC + 5:30) unconditionally."""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_news": [], "last_pulse_date": None}

def save_state(state: dict):
    state["seen_news"] = state.get("seen_news", [])[-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))

def fetch_upcoming_events() -> str:
    """Returns a formatted string of major macro events happening today or in the next 7 days."""
    now = get_ist_now().date()
    seven_days_from_now = now + datetime.timedelta(days=7)
    
    upcoming = []
    for date_str, event in MACRO_EVENTS_SCHEDULE:
        event_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        if now <= event_date <= seven_days_from_now:
            days_left = (event_date - now).days
            if days_left == 0:
                upcoming.append(f"🚨 <b>TODAY: {event}</b>")
            elif days_left == 1:
                upcoming.append(f"⏳ <b>TOMORROW:</b> {event}")
            else:
                upcoming.append(f"📅 <b>{event_date.strftime('%d %b')}:</b> {event}")
                
    if not upcoming:
        return ""
        
    return "\n\n🗓️ <b>UPCOMING 7-DAY MACRO CALENDAR:</b>\n" + "\n".join(upcoming)

# ----------------------------------------------------------------------
# MORNING MACRO PULSE (9:00 AM)
# ----------------------------------------------------------------------
def fetch_binance_btc() -> str:
    try:
        resp = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            price = float(data["lastPrice"])
            change = float(data["priceChangePercent"])
            sign = "+" if change > 0 else ""
            return f"<b>Bitcoin (BTC)</b>: ${price:,.2f} ({sign}{change:.2f}%)"
    except Exception:
        pass
    return "<b>Bitcoin (BTC)</b>: Data unavailable"

def fetch_macro_pulse_data() -> str:
    """Pulls live pricing for Bonds, Currencies, Crude, Global Futures, and Event Calendar."""
    log.info("Fetching Global Macro Data...")
    lines = [f"🌍 <b>PRE-MARKET GLOBAL MACRO PULSE</b>\n<i>{get_ist_now().strftime('%d %b %Y | %I:%M %p IST')}</i>\n"]
    
    if yf is None:
        return "\n".join(lines) + "yfinance module missing."

    for name, ticker in MACRO_SYMBOLS.items():
        try:
            t = yf.Ticker(ticker)
            fast = t.fast_info
            current_price = fast.get("lastPrice") or fast.get("last_price")
            prev_close = fast.get("previousClose") or fast.get("previous_close")
            
            if current_price and prev_close and prev_close > 0:
                pct_change = ((current_price - prev_close) / prev_close) * 100
                sign = "+" if pct_change > 0 else ""
                
                if "Yield" in name:
                    lines.append(f"🏛️ <b>{name}</b>: {current_price:.3f}% ({sign}{pct_change:.2f}%)")
                elif "Crude" in name:
                    lines.append(f"🛢️ <b>{name}</b>: ${current_price:.2f} ({sign}{pct_change:.2f}%)")
                elif "Gold" in name:
                    lines.append(f"🥇 <b>{name}</b>: ${current_price:.2f} ({sign}{pct_change:.2f}%)")
                elif "Futures" in name or "Nikkei" in name or "Shanghai" in name:
                    lines.append(f"📈 <b>{name}</b>: {current_price:,.2f} ({sign}{pct_change:.2f}%)")
                elif "Dollar" in name or "INR" in name:
                    lines.append(f"💵 <b>{name}</b>: {current_price:.2f} ({sign}{pct_change:.2f}%)")
            else:
                lines.append(f"• <b>{name}</b>: Market Closed / No Data")
        except Exception as e:
            log.warning(f"Failed to fetch {name} ({ticker}): {e}")
            
    # Add Crypto Pulse
    lines.append(f"🪙 {fetch_binance_btc()}")
    
    # Add Upcoming Macro Events
    events_text = fetch_upcoming_events()
    if events_text:
        lines.append(events_text)
    
    return "\n".join(lines)

def check_and_send_morning_pulse(state: dict) -> dict:
    now = get_ist_now()
    today_str = now.strftime("%Y-%m-%d")
    
    # WIDE TIME WINDOW: 8:45 AM to 9:30 AM IST
    is_morning_window = (now.hour == 8 and now.minute >= 45) or (now.hour == 9 and now.minute <= 30)
    
    # STATE CHECK: Has it already sent today?
    already_sent = state.get("last_pulse_date") == today_str
    
    # Abort if it's the weekend
    is_weekday = now.weekday() < 5
    
    if is_morning_window and not already_sent and is_weekday:
        try:
            log.info("Inside morning window. Triggering Global Macro Pulse...")
            macro_msg = fetch_macro_pulse_data()
            
            if send_telegram_message(macro_msg):
                state["last_pulse_date"] = today_str
                log.info("Morning Macro Pulse sent and locked for the day.")
            else:
                log.error("Failed to send morning pulse to Telegram.")
                
        except Exception as e:
            log.error("Error during morning pulse generation: %s", e)
            
    return state

# ----------------------------------------------------------------------
# NEWS POLLING (RSS & ALERTS)
# ----------------------------------------------------------------------
def check_keyword_match(text: str, keywords: list) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)

def poll_news(state: dict) -> dict:
    if not feedparser:
        return state

    log.info("Polling global RSS feeds for breaking macro news...")
    seen = state.get("seen_news", [])
    new_alerts = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                published = entry.get("published", "")
                
                fp = re.sub(r"[^A-Za-z0-9]", "", title.upper())[:40]
                if not fp or fp in seen:
                    continue

                category = None
                icon = "📢"
                header = "GLOBAL NEWS ALERT"
                
                if check_keyword_match(title, BELLWETHER_KEYWORDS):
                    category = "bellwether"
                    icon = "📢"
                    header = "GLOBAL BELLWETHER RESULTS / GUIDANCE 🇺🇸"
                elif check_keyword_match(title, GEOPOLITICAL_KEYWORDS):
                    category = "geopolitical"
                    icon = "⚠️"
                    header = "GEOPOLITICAL / MACRO SHOCK ALERT 🌍"
                elif check_keyword_match(title, REGULATORY_KEYWORDS):
                    category = "regulatory"
                    icon = "🏛️"
                    header = "REGULATORY / TRADE DUTY ALERT ⚖️"

                if category:
                    seen.append(fp)
                    safe_title = html.escape(title)
                    
                    msg = (
                        f"{icon} <b>{header}</b>\n\n"
                        f"{safe_title}\n\n"
                        f"🕐 {published}\n"
                        f"🔗 <a href='{link}'>Read Breakdown</a>"
                    )
                    new_alerts.append(msg)
                    
        except Exception as e:
            log.warning(f"Error parsing feed {feed_url}: {e}")

    for alert in new_alerts:
        send_telegram_message(alert)
        time.sleep(1)

    state["seen_news"] = seen
    return state

# ----------------------------------------------------------------------
# MAIN EXECUTION LOOP
# ----------------------------------------------------------------------
def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Global Macro News Radar%s", " (single-shot mode)" if one_shot else "")
    
    state = load_state()

    if one_shot:
        try:
            state = poll_news(state)
            state = check_and_send_morning_pulse(state)
            save_state(state)
        except Exception as e:
            log.exception("Error during single poll: %s", e)
        return

    # Continuous execution
    while True:
        try:
            state = poll_news(state)
            state = check_and_send_morning_pulse(state)
            save_state(state)
        except Exception as e:
            log.exception("Error during cycle: %s", e)
            
        log.info(f"Cycle complete. Sleeping for {POLL_INTERVAL_MINUTES} minutes...")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
