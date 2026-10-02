"""
Global Macro Pulse (9:00 AM IST), Global Bellwether Earnings, ADR Radar & Geopolitical Shocks
==============================================================================================
1. Sends a comprehensive global macro summary every morning at ~9:00 AM IST.
2. 7-Day Forward Calendar:
   - Dynamically tracks 1st-of-month (Auto Sales & GST Collections).
   - Tracks Central Bank policy meetings, US CPI, and Market Holidays.
   - Tracks FTSE & MSCI Rebalance cycles (Announcements & Implementation Days)
     with stock names and institutional inflow/outflow dollar allocations.
3. Live ADR Radar: Scans INFY, HDB, WIT, IBN in US markets; alerts if move exceeds 2%.
4. Curated Radar: US/Global Sector Leader Earnings & Guidance (TSMC, Intel, etc.).
5. Polls global news RSS feeds every 15 mins for high-impact geopolitical shocks
   (with strict filtering to remove domestic/local non-macro noise).
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
    print("WARNING: yfinance not installed. Macro pulse and ADR radar will fail without it.")

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

# Max earnings/ADR alerts per company within a rolling 24-hour window
MAX_ALERTS_PER_COMPANY_24H = 2
ADR_ALERT_COOLDOWN_24H = 1

# ----------------------------------------------------------------------
# 1. TIER-1 TRUSTED FINANCIAL NEWS SOURCES
# ----------------------------------------------------------------------
TRUSTED_FINANCIAL_SOURCES = [
    "reuters", "bloomberg", "cnbc", "barron's", "barrons",
    "wall street journal", "wsj", "financial times", "marketwatch",
    "investor's business daily", "yahoo finance", "associated press"
]

# ----------------------------------------------------------------------
# 2. GEOPOLITICAL SHOCK KEYWORDS & LOCAL NOISE FILTERS
# ----------------------------------------------------------------------
SHOCK_KEYWORDS = [
    "assassinat", "missile strike", "nuclear", "airstrike", 
    "military invasion", "invades", "martial law", "coup d'etat", 
    "terrorist attack", "geopolitical"
]

NON_MACRO_NOISE = [
    "school", "bus", "medical", "patient", "hospital", "crash", 
    "traffic", "murder", "domestic", "tornado", "hurricane", "police",
    "county", "local", "district"
]

# ----------------------------------------------------------------------
# 3. GLOBAL SECTOR LEADERS & INDIAN ADRs
# ----------------------------------------------------------------------
GLOBAL_LEADERS = {
    # IT Services & Enterprise Tech
    "accenture": {"name": "Accenture (ACN)", "impact": "Direct primary bellwether for Indian IT"},
    "cognizant": {"name": "Cognizant (CTSH)", "impact": "Direct peer benchmark for Indian IT offshore delivery"},
    "epam": {"name": "EPAM Systems (EPAM)", "impact": "Direct peer to Indian IT; benchmark for offshore engineering"},
    "ibm": {"name": "IBM", "impact": "Enterprise IT infrastructure & consulting budget indicator"},
    
    # AI, Cloud & Mega-Cap Tech
    "nvidia": {"name": "Nvidia (NVDA)", "impact": "Global AI hardware infrastructure anchor"},
    "microsoft": {"name": "Microsoft (MSFT)", "impact": "Enterprise cloud (Azure) & commercial AI capex"},
    "amazon": {"name": "Amazon (AMZN)", "impact": "AWS cloud spending & global consumer demand"},
    "google": {"name": "Alphabet (GOOGL)", "impact": "Digital ad spend & cloud infrastructure capex"},
    "meta": {"name": "Meta Platforms (META)", "impact": "AI capex cycle & digital ad revenue"},
    "apple": {"name": "Apple (AAPL)", "impact": "Global consumer electronics supply chain driver"},
    
    # Semiconductors & Hardware Cycle
    "amd": {"name": "AMD", "impact": "Data center compute & enterprise PC demand"},
    "micron": {"name": "Micron (MU)", "impact": "Memory chip cycle (DRAM/NAND)"},
    "tsmc": {"name": "TSMC", "impact": "World's largest chip foundry; global tech demand anchor"},
    "broadcom": {"name": "Broadcom (AVGO)", "impact": "Custom AI silicon & enterprise networking"},
    "asml": {"name": "ASML", "impact": "Lithography equipment; forward indicator of chip capex"},
    "sandisk": {"name": "SanDisk / WDC", "impact": "Memory & tech hardware supply chain indicator"},
    "western digital": {"name": "SanDisk / WDC", "impact": "Memory & tech hardware supply chain indicator"},
    "intel": {"name": "Intel (INTC)", "impact": "Global semiconductor bellwether"},
    "tesla": {"name": "Tesla (TSLA)", "impact": "EV sector sentiment & battery metals"},

    # Asian Giants
    "alibaba": {"name": "Alibaba (BABA)", "impact": "Chinese consumer consumption barometer"},
    "huawei": {"name": "Huawei", "impact": "Global telecom infrastructure benchmark"}
}

INDIAN_ADRS = {
    "INFY": "Infosys (IT Proxy)",
    "WIT": "Wipro (IT Proxy)",
    "HDB": "HDFC Bank (BankNifty Proxy)",
    "IBN": "ICICI Bank (BankNifty Proxy)",
    "RDY": "Dr. Reddy's (Pharma Proxy)",
    "MMYT": "MakeMyTrip (Consumption Proxy)"
}

EARNINGS_KEYWORDS = [
    "earnings", "revenue", "guidance", "profit", "quarterly result", "q1", "q2", "q3", "q4",
    "forecast", "outlook", "slashes", "raises outlook", "cuts outlook", "beats", "misses",
]

NEWS_FEEDS = [
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=when%3A4h%20(Accenture%20OR%20Cognizant%20OR%20Nvidia%20OR%20Microsoft%20OR%20Apple%20OR%20Amazon%20OR%20Google%20OR%20Meta%20OR%20AMD%20OR%20Micron%20OR%20TSMC%20OR%20Tesla%20OR%20Alibaba%20OR%20Huawei%20OR%20EPAM%20OR%20SanDisk%20OR%20Western%20Digital%20OR%20Intel)%20(earnings%20OR%20guidance%20OR%20revenue%20OR%20results)&hl=en-US&gl=US&ceid=US:en"
]

# ----------------------------------------------------------------------
# 4. MACRO ASSETS (9:00 AM PULSE)
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
}

# ----------------------------------------------------------------------
# 5. MACRO EVENT & INDEX REBALANCE CALENDAR (Auto/GST injected dynamically)
# ----------------------------------------------------------------------
MACRO_CALENDAR = [
    # --- October 2026 ---
    {"date": "2026-10-02", "event": "NSE/BSE Holiday (Mahatma Gandhi Jayanti)", "type": "Holiday 🛑", "details": []},
    {"date": "2026-10-05", "event": "RBI MPC Meeting Begins", "type": "Central Bank 🏛️", "details": []},
    {"date": "2026-10-07", "event": "RBI MPC Policy Decision (Repo Rate)", "type": "Central Bank 🏛️", "details": []},
    {"date": "2026-10-14", "event": "US CPI (Inflation Data Release)", "type": "Data Release 📊", "details": []},
    {"date": "2026-10-20", "event": "NSE/BSE Holiday (Dussehra)", "type": "Holiday 🛑", "details": []},
    {"date": "2026-10-27", "event": "US Fed FOMC Meeting Begins", "type": "Central Bank 🏛️", "details": []},
    {"date": "2026-10-28", "event": "US Fed FOMC Policy Decision", "type": "Central Bank 🏛️", "details": []},

    # --- November 2026 (MSCI & FTSE Cycles) ---
    {
        "date": "2026-11-10", 
        "event": "MSCI Semi-Annual Review Announcement", 
        "type": "Index Announcement 📢",
        "details": [
            "Official list of stock additions, deletions, and weight changes released post-US close."
        ]
    },
    {
        "date": "2026-11-20", 
        "event": "FTSE GEIS Review Announcement", 
        "type": "Index Announcement 📢",
        "details": [
            "FTSE preliminary list of additions, deletions & weight adjustments published."
        ]
    },
    {
        "date": "2026-11-30", 
        "event": "MSCI Implementation Day (Passive Flows at 3:15-3:30 PM)", 
        "type": "Index Rebalance ⚖️",
        "details": [
            "Expected Inclusions: [DIXON (+$210M), POLYCAB (+$185M), TRENT (+$160M)]",
            "Expected Exclusions: [BANDHANBNK (-$85M)]",
            "Execution: Heavy volume spikes expected on closing auction (3:15 PM - 3:30 PM)."
        ]
    },

    # --- December 2026 (FTSE Implementation) ---
    {
        "date": "2026-12-18", 
        "event": "FTSE GEIS Quarterly Rebalance Implementation", 
        "type": "Index Rebalance ⚖️",
        "details": [
            "Passive tracking funds execute weight adjustments at closing auction."
        ]
    }
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("macro_news")

# ----------------------------------------------------------------------
# TELEGRAM & STORAGE HELPERS
# ----------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or not TELEGRAM_BOT_TOKEN:
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
    except Exception:
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
# 9:00 AM MACRO PULSE & DYNAMIC CALENDAR
# ----------------------------------------------------------------------
def get_upcoming_events(days_ahead=7) -> str:
    today_ist = get_ist_now().date()
    end_date = today_ist + datetime.timedelta(days=days_ahead)
    
    events_list = []
    
    # 1. Inject Dynamic 1st-of-the-month (Auto Sales & GST)
    curr_date = today_ist
    while curr_date <= end_date:
        if curr_date.day == 1:
            events_list.append({
                "date_obj": curr_date,
                "type": "Data Release 📊",
                "event": "Indian Auto Sales & GST Collections",
                "details": [
                    "OEM monthly domestic sales dispatches & MoF GST collection figures."
                ]
            })
        curr_date += datetime.timedelta(days=1)
        
    # 2. Inject Static & Rebalance Events
    for item in MACRO_CALENDAR:
        evt_date = datetime.date.fromisoformat(item["date"])
        if today_ist <= evt_date <= end_date:
            events_list.append({
                "date_obj": evt_date,
                "type": item["type"],
                "event": item["event"],
                "details": item.get("details", [])
            })
            
    # Sort chronologically
    events_list.sort(key=lambda x: x["date_obj"])
    
    if not events_list:
        return ""
        
    upcoming = []
    for evt in events_list:
        diff = (evt["date_obj"] - today_ist).days
        day_str = "Today" if diff == 0 else "Tomorrow" if diff == 1 else f"In {diff} days"
        line = f"• <b>{evt['date_obj'].strftime('%d %b')}</b> ({day_str}): {evt['type']} — {evt['event']}"
        
        # Render stocks & allocations if populated
        if evt.get("details"):
            for d in evt["details"]:
                line += f"\n   └ <i>{d}</i>"
                
        upcoming.append(line)
        
    return "\n🗓️️ <b>7-Day Macro & Rebalance Calendar:</b>\n" + "\n".join(upcoming)

def fetch_macros() -> str:
    if not yf:
        return "⚠️ yfinance library not installed."
    
    lines = ["📊 <b>9:00 AM Global Macro Pulse</b>\n"]
    for name, ticker in MACRO_TICKERS.items():
        if ticker == "BTC-USD":
            try:
                res = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5).json()
                current, chg_pct = float(res['lastPrice']), float(res['priceChangePercent'])
                sign, color = ("+", "🟢") if chg_pct > 0 else ("", "🔴") if chg_pct < 0 else ("", "⚪")
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
                    continue
                chg_pct = 0.0
            else:
                prev_close, current = hist["Close"].iloc[-2], hist["Close"].iloc[-1]
                chg_pct = ((current - prev_close) / prev_close) * 100
            
            sign, color = ("+", "🟢") if chg_pct > 0 else ("", "🔴") if chg_pct < 0 else ("", "⚪")
            if ticker == "^TNX":
                lines.append(f"{color} {name}: <b>{current:.3f}%</b> ({sign}{chg_pct:.2f}%)")
            else:
                lines.append(f"{color} {name}: <b>{current:,.2f}</b> ({sign}{chg_pct:.2f}%)")
        except Exception:
            pass
            
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
            send_telegram_message(fetch_macros())
            state["last_pulse_date"] = today_str
            save_json(STATE_FILE, state)

# ----------------------------------------------------------------------
# LIVE BREAKING RADAR: ADRs, EARNINGS & SHOCKS
# ----------------------------------------------------------------------
def check_breaking_news():
    seen_links = set(load_json(SEEN_NEWS_FILE, []))
    state = load_json(STATE_FILE, {})
    company_history = state.get("company_alerts", {})
    now_epoch = time.time()
    day_seconds = 24 * 3600
    
    # Prune alerts older than 24h
    for c_key in list(company_history.keys()):
        company_history[c_key] = [item for item in company_history[c_key] if (now_epoch - item.get("time", 0)) < day_seconds]
        if not company_history[c_key]:
            del company_history[c_key]

    # --- 1. INDIAN ADR LIVE VOLATILITY RADAR ---
    adr_alerts = []
    if yf:
        for ticker, name in INDIAN_ADRS.items():
            recent_adr_alerts = [a for a in company_history.get(ticker, []) if a.get("type") == "ADR_VOLATILITY"]
            if len(recent_adr_alerts) >= ADR_ALERT_COOLDOWN_24H:
                continue
                
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="2d")
                if len(hist) >= 2:
                    prev_close = float(hist["Close"].iloc[-2])
                    current = float(hist["Close"].iloc[-1])
                    chg_pct = ((current - prev_close) / prev_close) * 100
                    
                    if abs(chg_pct) >= 2.0:
                        adr_alerts.append((name, current, chg_pct))
                        if ticker not in company_history:
                            company_history[ticker] = []
                        company_history[ticker].append({"time": now_epoch, "type": "ADR_VOLATILITY"})
            except Exception:
                pass
                
    for name, current, chg in adr_alerts:
        sign, color = ("+", "🟢") if chg > 0 else ("", "🔴")
        msg = (f"🚨 <b>MAJOR ADR MOVE DETECTED</b> 🇮🇳🇺🇸\n\n"
               f"<b>{name}</b> is trading with high volatility in US markets right now.\n\n"
               f"Current US Price: <b>${current:.2f}</b>\n"
               f"Move: <b>{color} {sign}{chg:.2f}%</b>\n\n"
               f"<i>*Direct precursor for Nifty/BankNifty opening gap.</i>")
        send_telegram_message(msg)
        time.sleep(1.2)

    # --- 2. GLOBAL RSS SCANNER (Earnings & Shocks) ---
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
                
                if not raw_title or not link or link in seen_links:
                    continue
                title_lower, clean_title = raw_title.lower(), html.escape(raw_title)
                source_name = raw_title.split(" - ")[-1].strip() if " - " in raw_title else ""
                source_lower = source_name.lower()
                
                # Check Global Earnings
                matched_key, matched_meta = None, None
                for key, meta in GLOBAL_LEADERS.items():
                    if re.search(rf"\b{key}\b", title_lower):
                        matched_key, matched_meta = key, meta
                        break
                        
                if matched_key and any(re.search(rf"\b{kw}\b", title_lower) for kw in EARNINGS_KEYWORDS):
                    if not any(ts in source_lower for ts in TRUSTED_FINANCIAL_SOURCES):
                        seen_links.add(link)
                        continue
                    recent_alerts = [a for a in company_history.get(matched_key, []) if a.get("type") == "EARNINGS"]
                    if len(recent_alerts) >= MAX_ALERTS_PER_COMPANY_24H or source_lower in [a.get("source", "").lower() for a in recent_alerts]:
                        seen_links.add(link)
                        continue
                        
                    earnings_alerts.append((clean_title, link, pub_date, matched_meta, source_name))
                    seen_links.add(link)
                    if matched_key not in company_history:
                        company_history[matched_key] = []
                    company_history[matched_key].append({"time": now_epoch, "type": "EARNINGS", "source": source_name})
                    continue

                # Check Geopolitical Shocks
                if any(re.search(rf"\b{kw}\b", title_lower) for kw in SHOCK_KEYWORDS):
                    # Strict Noise Filter: Ignore local police, hospital, and vehicle accident reports
                    if not any(re.search(rf"\b{noise}\b", title_lower) for noise in NON_MACRO_NOISE):
                        shock_alerts.append((clean_title, link, pub_date))
                        seen_links.add(link)
                    
        except Exception:
            pass
            
    for title, link, date in shock_alerts:
        send_telegram_message(f"🚨 <b>BREAKING MACRO SHOCK</b> 🚨\n\n<b>{title}</b>\n\n🕐 {date}\n🔗 <a href='{link}'>Read Report</a>")
        time.sleep(1.2)
        
    for title, link, date, comp, source in earnings_alerts:
        send_telegram_message(f"📢 <b>GLOBAL BELLWETHER RESULTS / GUIDANCE</b> 🇺🇸\n\n<b>{title}</b>\n\n🏢 <b>Entity:</b> {comp['name']}\n📰 <b>Source:</b> {source if source else 'Wire'}\n🎯 <b>Indian Impact:</b> {comp['impact']}\n🕐 {date}\n🔗 <a href='{link}'>Read Breakdown</a>")
        time.sleep(1.2)
        
    if shock_alerts or earnings_alerts or adr_alerts or seen_links:
        state["company_alerts"] = company_history
        save_json(STATE_FILE, state)
        save_json(SEEN_NEWS_FILE, list(seen_links)[-500:])

# ----------------------------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------------------------
def main():
    if "--once" in sys.argv:
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
