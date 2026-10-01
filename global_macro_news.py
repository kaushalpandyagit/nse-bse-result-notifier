"""
Global Macro Pulse (9:00 AM IST), Global Bellwether Earnings & Geopolitical Shock Radar
========================================================================================
1. Sends a comprehensive global macro summary every morning at ~9:00 AM IST.
2. Includes a 7-Day Forward Calendar for RBI/Fed meets, CPI/PMI, and Market Holidays.
3. Live Radar for US/Global Sector Leader Earnings & Guidance (Accenture, Nvidia, etc.).
4. Polls global news RSS feeds every 15 mins for high-impact geopolitical shocks.
"""

import os
import re
import sys
import json
import time
import html
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

# ----------------------------------------------------------------------
# 1. GEOPOLITICAL SHOCK KEYWORDS
# ----------------------------------------------------------------------
SHOCK_KEYWORDS = [
    "war", "assassinat", "missile", "nuclear", "strike", "attack", 
    "invasion", "invades", "terrorist", "geopolitical", "emergency", 
    "martial law", "coup", "bombing", "airstrike"
]

# ----------------------------------------------------------------------
# 2. GLOBAL SECTOR LEADERS & EARNINGS TRIGGERS
# ----------------------------------------------------------------------
GLOBAL_LEADERS = {
    # IT Services & Enterprise Tech (Direct Indian IT Proxies)
    "accenture": {"name": "Accenture (ACN)", "impact": "Direct primary bellwether for Indian IT (TCS, INFY, HCLTECH, WIPRO)"},
    "cognizant": {"name": "Cognizant (CTSH)", "impact": "Direct peer benchmark for Indian IT offshore delivery & billing rates"},
    "capgemini": {"name": "Capgemini", "impact": "European & global IT services demand benchmark"},
    "ibm": {"name": "IBM", "impact": "Enterprise IT infrastructure & consulting budget indicator"},
    
    # AI, Cloud & Mega-Cap Tech
    "nvidia": {"name": "Nvidia (NVDA)", "impact": "Global AI hardware infrastructure anchor & market liquidity driver"},
    "microsoft": {"name": "Microsoft (MSFT)", "impact": "Enterprise cloud (Azure) & commercial AI capex indicator"},
    "amazon": {"name": "Amazon (AMZN)", "impact": "AWS cloud spending & global consumer demand benchmark"},
    "google": {"name": "Alphabet / Google (GOOGL)", "impact": "Digital ad spend & cloud infrastructure capex bellwether"},
    "alphabet": {"name": "Alphabet / Google (GOOGL)", "impact": "Digital ad spend & cloud infrastructure capex bellwether"},
    "meta": {"name": "Meta Platforms (META)", "impact": "AI capex cycle & digital ad revenue benchmark"},
    "apple": {"name": "Apple (AAPL)", "impact": "Global consumer electronics & electronics manufacturing supply chain driver"},
    
    # Semiconductors & Hardware Cycle
    "amd": {"name": "AMD", "impact": "Data center compute & enterprise PC demand barometer"},
    "micron": {"name": "Micron Technology (MU)", "impact": "Memory chip cycle (DRAM/NAND) & tech hardware early indicator"},
    "tsmc": {"name": "TSMC", "impact": "World's largest chip foundry; earliest indicator of global tech demand"},
    "broadcom": {"name": "Broadcom (AVGO)", "impact": "Custom AI silicon & enterprise networking infrastructure driver"},
    "asml": {"name": "ASML", "impact": "Lithography equipment monopoly; forward indicator of global chip capex"},
    "tesla": {"name": "Tesla (TSLA)", "impact": "EV sector sentiment, battery metals & global auto tech barometer"}
}

EARNINGS_KEYWORDS = [
    "earnings", "revenue", "guidance", "profit", "quarterly result", "q1", "q2", "q3", "q4",
    "forecast", "outlook", "slashes", "raises outlook", "cuts outlook", "beats", "misses",
    "top-line", "bottom-line", "fiscal result"
]

# RSS Feeds: World Headlines, Business (India + US), and Search Feed for US Leaders
NEWS_FEEDS = [
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=when%3A4h%20(Accenture%20OR%20Cognizant%20OR%20Nvidia%20OR%20Microsoft%20OR%20Apple%20OR%20Amazon%20OR%20Google%20OR%20Meta%20OR%20AMD%20OR%20Micron%20OR%20TSMC%20OR%20Tesla)%20(earnings%20OR%20guidance%20OR%20revenue%20OR%20results)&hl=en-US&gl=US&ceid=US:en"
]

# ----------------------------------------------------------------------
# 3. MACRO ASSETS (9:00 AM PULSE)
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# 4. MACRO EVENT CALENDAR (Next 7 Days Scanner)
# ----------------------------------------------------------------------
MACRO_CALENDAR = [
    {"date": "2026-10-02", "event": "NSE/BSE Holiday (Mahatma Gandhi Jayanti)", "type": "Holiday 🛑"},
    {"date": "2026-10-05", "event": "RBI MPC Meeting Begins", "type": "Central Bank 🏛️"},
    {"date": "2026-10-07", "event": "RBI MPC Policy Decision (Repo Rate)", "type": "Central Bank 🏛️"},
    {"date": "2026-10-14", "event": "US CPI (Inflation Data Release)", "type": "Data Release 📊"},
    {"date": "2026-10-20", "event": "NSE/BSE Holiday (Dussehra)", "type": "Holiday 🛑"},
    {"date": "2026-10-27", "event": "US Fed FOMC Meeting Begins", "type": "Central Bank 🏛️"},
    {"date": "2026-10-28", "event": "US Fed FOMC Policy Decision", "type": "Central Bank 🏛️"},
    {"date": "2026-11-10", "event": "NSE/BSE Holiday (Diwali-Balipratipada)", "type": "Holiday 🛑"},
    {"date": "2026-12-08", "event": "US Fed FOMC Policy Decision", "type": "Central Bank 🏛️"}
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("macro_news")

# ----------------------------------------------------------------------
# TELEGRAM & STORAGE HELPERS
# ----------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or not TELEGRAM_BOT_TOKEN:
        log.error("Telegram credentials not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{SCRIPT_TAG}\n{text}",
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code != 200:
            log.error("Telegram failed [%s]: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as e:
        log.error("Telegram send exception: %s", e)
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
# 9:00 AM MACRO PULSE & CALENDAR ENGINE
# ----------------------------------------------------------------------
def get_upcoming_events(days_ahead=7) -> str:
    today_ist = get_ist_now().date()
    end_date = today_ist + datetime.timedelta(days=days_ahead)
    
    upcoming = []
    for item in MACRO_CALENDAR:
        evt_date = datetime.date.fromisoformat(item["date"])
        if today_ist <= evt_date <= end_date:
            diff = (evt_date - today_ist).days
            day_str = "Today" if diff == 0 else "Tomorrow" if diff == 1 else f"In {diff} days"
            upcoming.append(f"• <b>{evt_date.strftime('%d %b')}</b> ({day_str}): {item['type']} — {item['event']}")
            
    if not upcoming:
        return ""
        
    return "\n🗓️ <b>7-Day Macro Event Calendar:</b>\n" + "\n".join(upcoming)

def fetch_macros() -> str:
    if not yf:
        return "⚠️ yfinance library not installed."
    
    lines = ["📊 <b>9:00 AM Global Macro Pulse</b>\n"]
    
    for name, ticker in MACRO_TICKERS.items():
        # BTC Bulletproof Bypass via Binance API
        if ticker == "BTC-USD":
            try:
                res = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5).json()
                current = float(res['lastPrice'])
                chg_pct = float(res['priceChangePercent'])
                sign = "+" if chg_pct > 0 else ""
                color = "🟢" if chg_pct > 0 else "🔴" if chg_pct < 0 else "⚪"
                lines.append(f"{color} {name}: <b>${current:,.0f}</b> ({sign}{chg_pct:.2f}%)")
                continue
            except Exception:
                pass
                
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            
            if len(hist) < 2:
                current = t.fast_info.get("lastPrice", 0)
                if current == 0:
                    lines.append(f"• {name}: <i>No data</i>")
                    continue
                chg_pct = 0.0
            else:
                prev_close = hist["Close"].iloc[-2]
                current = hist["Close"].iloc[-1]
                chg_pct = ((current - prev_close) / prev_close) * 100
            
            sign = "+" if chg_pct > 0 else ""
            color = "🟢" if chg_pct > 0 else "🔴" if chg_pct < 0 else "⚪"
            
            if ticker == "^TNX":
                lines.append(f"{color} {name}: <b>{current:.3f}%</b> ({sign}{chg_pct:.2f}%)")
            elif ticker == "BTC-USD":
                lines.append(f"{color} {name}: <b>${current:,.0f}</b> ({sign}{chg_pct:.2f}%)")
            else:
                lines.append(f"{color} {name}: <b>{current:,.2f}</b> ({sign}{chg_pct:.2f}%)")
                
        except Exception as e:
            log.warning("Failed to fetch %s: %s", name, e)
            lines.append(f"• {name}: <i>Data error</i>")
            
    calendar_text = get_upcoming_events(days_ahead=7)
    if calendar_text:
        lines.append(f"\n{calendar_text}")
        
    return "\n".join(lines)

def run_macro_pulse_if_needed():
    now = get_ist_now()
    if (now.hour == 8 and now.minute >= 50) or (now.hour == 9 and now.minute <= 15):
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
# LIVE BREAKING RADAR: GEOPOLITICAL SHOCKS & GLOBAL EARNINGS
# ----------------------------------------------------------------------
def check_breaking_news():
    seen = set(load_json(SEEN_NEWS_FILE, []))
    shock_alerts = []
    earnings_alerts = []
    
    for feed_url in NEWS_FEEDS:
        try:
            resp = requests.get(feed_url, timeout=12)
            if resp.status_code != 200:
                continue
                
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:20]:
                raw_title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                
                if not raw_title or not link or link in seen:
                    continue
                    
                title_lower = raw_title.lower()
                clean_title = html.escape(raw_title)
                
                # Check for Global Sector Leader Earnings / Guidance
                matched_company = None
                for key, meta in GLOBAL_LEADERS.items():
                    if re.search(rf"\b{key}\b", title_lower):
                        matched_company = meta
                        break
                        
                if matched_company:
                    if any(re.search(rf"\b{kw}\b", title_lower) for kw in EARNINGS_KEYWORDS):
                        earnings_alerts.append((clean_title, link, pub_date, matched_company))
                        seen.add(link)
                        continue

                # Check for Geopolitical Shock Keywords
                if any(re.search(rf"\b{kw}\b", title_lower) for kw in SHOCK_KEYWORDS):
                    shock_alerts.append((clean_title, link, pub_date))
                    seen.add(link)
                    
        except Exception as e:
            log.warning("News fetch failed for feed %s: %s", feed_url, e)
            
    # Dispatch Geopolitical Shocks
    for title, link, date in shock_alerts:
        msg = (
            f"🚨 <b>BREAKING MACRO SHOCK</b> 🚨\n\n"
            f"<b>{title}</b>\n\n"
            f"🕐 {date}\n"
            f"🔗 <a href='{link}'>Read Full Report</a>"
        )
        send_telegram_message(msg)
        time.sleep(1.2)
        
    # Dispatch Global Bellwether Results / Guidance Alerts
    for title, link, date, comp in earnings_alerts:
        msg = (
            f"📢 <b>GLOBAL BELLWETHER RESULTS / GUIDANCE</b> 🇺🇸\n\n"
            f"<b>{title}</b>\n\n"
            f"🏢 <b>Entity:</b> {comp['name']}\n"
            f"🎯 <b>Indian Market Impact:</b> {comp['impact']}\n"
            f"🕐 {date}\n"
            f"🔗 <a href='{link}'>Read Detailed Breakdown</a>"
        )
        send_telegram_message(msg)
        time.sleep(1.2)
        
    if shock_alerts or earnings_alerts:
        save_json(SEEN_NEWS_FILE, list(seen)[-500:])

# ----------------------------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------------------------
def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Global Macro, Earnings & News Radar.")
    
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
