"""
Global Macro & Geopolitical News Radar
================================================================
File: global_macro_news.py

Features:
  1. 9:00 AM IST Macro Pulse (US Futures, 10Y Yield, DXY, Crude, Gold, BTC).
  2. 7-Day Economic Event Calendar Tracker (RBI, Fed, CPI).
  3. Live Geopolitical & Global Bellwether News Polling.
  4. USFDA, PLI Scheme, Tariff/Duty News Tracking, & Indian PM/FM Economic Statements.
  5. Commodity Supply-Disruption Watch: weather / strike / outage / export-curb events
     in the top producing nations of key energy, metal and agri commodities.
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
import urllib.parse

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

# RSS feeds for Global Macro, USFDA, Market News, and targeted Indian Leaders Google News RSS
RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?id=10000664",
    "https://feeds.bloomberg.com/markets/news.rss",
    # Targeted Google News RSS feed for Indian Prime Minister & Finance Minister statements on Economy, Finance, & Semiconductors
    "https://news.google.com/rss/search?q=" + urllib.parse.quote("(PM Modi OR Finance Minister Nirmala Sitharaman) AND (semiconductor OR economy OR finance OR GDP OR fiscal OR inflation OR budget)") + "&hl=en-IN&gl=IN&ceid=IN:en"
]

# Keywords to filter breaking impact news
BELLWETHER_KEYWORDS = ["nvidia", "apple", "tesla", "microsoft", "tsmc", "guidance", "revenue booms", "earnings", "ai demand"]
GEOPOLITICAL_KEYWORDS = ["war", "strike", "missile", "sanctions", "geopolitical", "opec", "fed", "federal reserve", "rate cut", "rbi"]
REGULATORY_KEYWORDS = ["usfda", "fda", "pli scheme", "tariff", "duty", "import tax", "export ban", "anti-dumping"]
LEADER_STATEMENT_KEYWORDS = ["semiconductor", "economy", "finance", "fiscal", "gdp", "inflation", "investment", "rupee", "budget", "fabs", "chip"]

# ----------------------------------------------------------------------
# COMMODITY SUPPLY-DISRUPTION WATCH
# ----------------------------------------------------------------------
# Country aliases are regexes matched case-insensitively, except "US" which must be
# upper-case so the pronoun "us" never matches.
COUNTRY_ALIASES = {
    "United States": [r"u\.s\.", r"united states", r"america", r"texas", r"gulf of mexico", r"permian", r"midwest", r"corn belt", r"iowa", r"illinois"],
    "Russia": [r"russia", r"russian"],
    "Saudi Arabia": [r"saudi", r"aramco"],
    "Iran": [r"iran", r"iranian"],
    "China": [r"china", r"chinese"],
    "India": [r"india", r"indian", r"monsoon"],
    "Indonesia": [r"indonesia", r"indonesian", r"kalimantan", r"sulawesi"],
    "Australia": [r"australia", r"australian", r"pilbara"],
    "Brazil": [r"brazil", r"brazilian", r"minas gerais", r"vale"],
    "Chile": [r"chile", r"chilean", r"codelco", r"escondida"],
    "DR Congo": [r"congo", r"drc", r"katanga"],
    "Peru": [r"peru", r"peruvian", r"las bambas"],
    "Philippines": [r"philippines", r"philippine"],
    "Bangladesh": [r"bangladesh"],
    "Argentina": [r"argentina", r"argentine"],
    "Vietnam": [r"vietnam", r"vietnamese"],
    "Colombia": [r"colombia", r"colombian"],
    "Kenya": [r"kenya", r"kenyan"],
}

# commodity -> (headline keywords, top producers, Indian market read-through)
COMMODITY_WATCH = {
    "Crude Oil":   ([r"crude", r"oil output", r"oil production", r"oilfield", r"oil field", r"refinery", r"refineries", r"opec", r"oil exports?", r"oil supply", r"pipeline"],
                    ["United States", "Russia", "Saudi Arabia"],
                    "OMCs (BPCL, HPCL, IOC), ONGC/Oil India, paints, aviation, tyres"),
    "Natural Gas": ([r"natural gas", r"\blng\b", r"gas field", r"gas supply", r"gas exports?", r"gas pipeline"],
                    ["United States", "Russia", "Iran", "China"],
                    "Petronet LNG, GAIL, IGL, MGL, Gujarat Gas, fertilisers"),
    "Coal":        ([r"coal"], ["China", "India", "Indonesia"],
                    "Coal India, power utilities (NTPC, Tata Power), cement, steel"),
    "Iron Ore":    ([r"iron ore"], ["Australia", "Brazil", "China"],
                    "NMDC, steel makers (Tata Steel, JSW, SAIL), Lloyds Metals"),
    "Gold":        ([r"gold mine", r"gold mining", r"gold output", r"gold production", r"gold miner"], ["China", "Russia", "Australia"],
                    "Titan, Kalyan, Senco, Muthoot / Manappuram (gold loans)"),
    "Copper":      ([r"copper"], ["Chile", "DR Congo", "Peru"],
                    "Hindustan Copper, Hindalco, Vedanta, cable makers (Polycab, KEI)"),
    "Nickel":      ([r"nickel"], ["Indonesia", "Philippines", "Russia"],
                    "Stainless steel (Jindal Stainless), battery / EV supply chain"),
    "Wheat":       ([r"wheat"], ["China", "India", "Russia"],
                    "Flour / FMCG (Britannia, Nestle), food inflation, export policy"),
    "Rice":        ([r"rice"], ["China", "India", "Bangladesh"],
                    "KRBL, LT Foods, Chaman Lal Setia, export policy"),
    "Corn":        ([r"corn", r"maize"], ["United States", "China", "Brazil"],
                    "Poultry feed (Venky's, Godrej Agrovet), ethanol, starch makers"),
    "Soybeans":    ([r"soybeans?", r"soy crop", r"soymeal", r"soya"], ["Brazil", "United States", "Argentina"],
                    "Edible oils (AWL Agri, Patanjali Foods), poultry feed"),
    "Sugar":       ([r"sugar", r"sugarcane", r"cane crush", r"cane crop"], ["Brazil", "India", "China"],
                    "Balrampur, Triveni, EID Parry, Dalmia Bharat Sugar, ethanol"),
    "Coffee":      ([r"coffee", r"arabica", r"robusta"], ["Brazil", "Vietnam", "Colombia"],
                    "CCL Products, Tata Consumer"),
    "Tea":         ([r"\btea\b", r"tea crop", r"tea output", r"tea production", r"tea estate"], ["China", "India", "Kenya"],
                    "Tata Consumer, McLeod Russel, Jay Shree Tea, HUL"),
}

# event type -> trigger words (whole-word, case-insensitive)
DISRUPTION_EVENTS = {
    "Weather": [r"drought", r"floods?", r"flooding", r"heat ?wave", r"cyclone", r"hurricane", r"typhoon", r"frost",
                r"freeze", r"cold snap", r"el nino", r"la nina", r"wildfires?", r"storms?", r"heavy rains?", r"dry spell",
                r"poor monsoon", r"weak monsoon", r"monsoon (?:deficit|delay|failure)", r"crop damage", r"harvest delay"],
    "Strike / Labour": [r"strikes?", r"walkout", r"protests?", r"blockades?", r"unrest", r"lockout"],
    "Outage / Accident": [r"outage", r"force majeure", r"shut ?down", r"collapse", r"explosion", r"blaze",
                          r"fire (?:at|hits|breaks out)", r"landslide", r"earthquake", r"leak", r"attack(?:s|ed)?", r"drones?",
                          # bare "halt"/"suspend" matched e.g. "halt copper decline"; require an operations object
                          r"(?:halts?|halted|halting|suspends?|suspended|stops?|stopped) (?:\w+ ){0,2}(?:production|output|operations|mining|exports?|shipments?|loading|supply|supplies)",
                          r"(?:production|output|operations|mining|exports?|shipments?|loading) (?:halted|suspended|stopped)"],
    "Supply / Policy": [r"export ban", r"bans? exports?", r"export curbs?", r"export duty", r"export restrictions?", r"sanctions?",
                        r"embargo", r"output cuts?", r"production cuts?", r"supply cuts?", r"supply disruption", r"shortage",
                        r"quota"],
}

COMMODITY_ALERT_COOLDOWN_HOURS = 12
COMMODITY_NEWS_MAX_AGE_HOURS = 48

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
    state["seen_news"] = state.get("seen_news", [])[-2000:]   # commodity watch adds its own fingerprints
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

    log.info("Polling global and Indian leadership RSS feeds for breaking macro news...")
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
                
                # Check if it's targeted PM/FM economic/semiconductor news from Google News feed
                if "news.google.com" in feed_url:
                    if check_keyword_match(title, LEADER_STATEMENT_KEYWORDS) and ("modi" in title.lower() or "sitharaman" in title.lower() or "finance minister" in title.lower() or "prime minister" in title.lower()):
                        category = "leadership_statement"
                        icon = "🇮🇳"
                        header = "PM / FINANCE MINISTER STATEMENT (ECONOMY / SEMICONDUCTOR)"
                else:
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
# COMMODITY SUPPLY-DISRUPTION WATCH
# ----------------------------------------------------------------------
def _rx(words: list) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)

_US_UPPER = re.compile(r"\bUS\b")
_COUNTRY_RX = {c: _rx(a) for c, a in COUNTRY_ALIASES.items()}
_EVENT_RX = {e: _rx(w) for e, w in DISRUPTION_EVENTS.items()}
_COMMODITY_RX = {c: _rx(kw) for c, (kw, _, _) in COMMODITY_WATCH.items()}

def _country_hit(country: str, text: str) -> bool:
    if country == "United States" and _US_UPPER.search(text):
        return True
    return bool(_COUNTRY_RX[country].search(text))

def classify_commodity_disruption(title: str):
    """(commodity, country, event_type, event_word) when a headline names a watched
    commodity, one of ITS top producers, and a disruption event; else None."""
    for commodity, (_, producers, _) in COMMODITY_WATCH.items():
        if not _COMMODITY_RX[commodity].search(title):
            continue
        country = next((c for c in producers if _country_hit(c, title)), None)
        if not country:
            continue
        for event, rx in _EVENT_RX.items():
            m = rx.search(title)
            if m:
                return commodity, country, event, m.group(0).lower()
    return None

COMMODITY_SEARCH_TERMS = {
    "Crude Oil": 'crude OR "oil production" OR refinery OR oilfield OR OPEC',
    "Natural Gas": '"natural gas" OR LNG OR "gas field"',
    "Coal": "coal", "Iron Ore": '"iron ore"', "Gold": '"gold mine" OR "gold mining"',
    "Copper": "copper", "Nickel": "nickel", "Wheat": "wheat", "Rice": "rice",
    "Corn": "corn OR maize", "Soybeans": "soybean OR soybeans OR soy", "Sugar": "sugar OR sugarcane",
    "Coffee": "coffee OR arabica OR robusta", "Tea": '"tea crop" OR "tea production" OR "tea output" OR "tea estate" OR "tea prices"',
}

def _google_news_url(commodity: str) -> str:
    producers = COMMODITY_WATCH[commodity][1]
    country_terms = " OR ".join(f'"{c}"' if " " in c else c for c in producers)
    event_terms = ('drought OR flood OR frost OR hurricane OR cyclone OR heatwave OR monsoon OR strike OR outage '
                   'OR halt OR shutdown OR "export ban" OR shortage OR "force majeure" OR sanctions')
    q = f"({COMMODITY_SEARCH_TERMS[commodity]}) ({country_terms}) ({event_terms}) when:2d"
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"

def poll_commodity_disruptions(state: dict) -> dict:
    if not feedparser:
        return state
    seen = state.setdefault("seen_news", [])
    cooldown = state.setdefault("commodity_alert_cooldown", {})
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    alerts = []

    for commodity in COMMODITY_WATCH:
        try:
            feed = feedparser.parse(_google_news_url(commodity))
        except Exception as e:
            log.warning("Commodity feed failed for %s: %s", commodity, e)
            continue
        for entry in feed.entries[:40]:
            raw_title = entry.get("title", "")
            title = re.sub(r"\s+-\s+[^-]+$", "", raw_title).strip()   # drop " - Publisher"
            fp = "C:" + re.sub(r"[^A-Za-z0-9]", "", title.upper())[:40]
            if not title or fp in seen:
                continue
            pub = entry.get("published_parsed")
            if pub:
                pub_dt = datetime.datetime(*pub[:6], tzinfo=datetime.timezone.utc)
                if (now_utc - pub_dt).total_seconds() > COMMODITY_NEWS_MAX_AGE_HOURS * 3600:
                    continue
            hit = classify_commodity_disruption(title)
            if not hit:
                continue
            seen.append(fp)
            c_name, country, event, word = hit
            key = f"{c_name}|{country}"   # one alert per commodity+country per cooldown, whatever the wording
            last = cooldown.get(key)
            if last and (now_utc - datetime.datetime.fromisoformat(last)).total_seconds() < COMMODITY_ALERT_COOLDOWN_HOURS * 3600:
                continue   # same story from another outlet
            cooldown[key] = now_utc.isoformat()
            source = entry.get("source", {}).get("title", "") if isinstance(entry.get("source"), dict) else ""
            alerts.append((c_name, country, event, word, title, entry.get("link", ""), source))

    # keep the cooldown map small
    cutoff = now_utc - datetime.timedelta(hours=COMMODITY_ALERT_COOLDOWN_HOURS * 2)
    state["commodity_alert_cooldown"] = {k: v for k, v in cooldown.items() if datetime.datetime.fromisoformat(v) >= cutoff}

    icons = {"Weather": "🌦️", "Strike / Labour": "✊", "Outage / Accident": "🛑", "Supply / Policy": "🚫"}
    blocks = []
    for c_name, country, event, word, title, link, source in alerts:
        blocks.append(
            f"{icons.get(event, '⚠️')} <b>{html.escape(c_name)} | {html.escape(country)} | {event}</b> ({html.escape(word)})\n"
            f"{html.escape(title)}{f' — <i>{html.escape(source)}</i>' if source else ''}\n"
            f"🇮🇳 <i>Watch: {html.escape(COMMODITY_WATCH[c_name][2])}</i>\n"
            f"🔗 <a href='{html.escape(link, quote=True)}'>Read</a>"
        )
    if len(blocks) <= 3:
        for b in blocks:
            send_telegram_message(f"🌐 <b>COMMODITY SUPPLY ALERT</b>\n\n{b}")
            time.sleep(1)
    else:
        header = f"🌐 <b>COMMODITY SUPPLY WATCH — {len(blocks)} new events</b>"
        chunk = [header]
        for b in blocks:
            if sum(len(x) + 2 for x in chunk) + len(b) > 3800:
                send_telegram_message("\n\n".join(chunk)); time.sleep(1)
                chunk = [header + " (contd.)"]
            chunk.append(b)
        send_telegram_message("\n\n".join(chunk))
    log.info("Commodity watch: %d new disruption alerts.", len(alerts))
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
            try:
                state = poll_commodity_disruptions(state)
            except Exception as e:
                log.exception("Commodity watch failed: %s", e)
            state = check_and_send_morning_pulse(state)
            save_state(state)
        except Exception as e:
            log.exception("Error during single poll: %s", e)
        return

    # Continuous execution
    while True:
        try:
            state = poll_news(state)
            try:
                state = poll_commodity_disruptions(state)
            except Exception as e:
                log.exception("Commodity watch failed: %s", e)
            state = check_and_send_morning_pulse(state)
            save_state(state)
        except Exception as e:
            log.exception("Error during cycle: %s", e)
            
        log.info(f"Cycle complete. Sleeping for {POLL_INTERVAL_MINUTES} minutes...")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
