"""
NSE + BSE Live Result, Order Win, Insider Trade, Circular & Meeting Notifier -> Telegram
==========================================================================================
Covers:
  1. Financial Results (Regulation 33 / Board outcomes)
  2. Order & Contract Wins (with Rupee value extraction)
  3. Insider Trading & Promoter Actions (Filters OUT generic Trading Window closures)
  4. NSE Exchange Circulars
  5. AGMs, E-Voting, and Investor / Analyst Meets (with Auto-PDF Date & Purpose Extraction)
  6. Extended Timings: Weekdays 08:00-22:30 IST, Weekends 09:00-21:00 IST
"""

import os
import re
import sys
import io
import json
import time
import random
import logging
import datetime
from pathlib import Path

import requests

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
    print("WARNING: PyPDF2 is not installed. PDF date extraction will be disabled.")

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

POLL_INTERVAL_MINUTES = 15

RESULT_KEYWORDS = [
    "financial result", "financial results", "quarterly result",
    "quarterly results", "board meeting outcome", "un-audited",
    "unaudited", "audited financial", "results for the quarter",
    "results for the year", "regulation 33", "reg. 33", "reg 33",
    "standalone and consolidated financial", "submitted to the exchange",
]

ORDER_KEYWORDS = [
    "award of order", "awarded order", "awarded contract", "award of contract",
    "receipt of order", "received order", "receipt of contract", "bagging of order",
    "bagging of contract", "bags order", "bags contract", "secures order",
    "secured order", "secures contract", "wins order", "wins contract",
    "letter of intent", "l.o.i.", " loi ", "letter of award",
    "l.o.a.", " loa ", "purchase order", "work order",
    "order/contract", "order / contract",
]

INSIDER_PROMOTER_KEYWORDS = [
    "regulation 7(2)", "reg 7(2)", "reg. 7(2)", "form c", "insider trading",
    "prohibition of insider trading", "pit regulations",
    "regulation 29(1)", "reg 29(1)", "reg. 29(1)",
    "regulation 29(2)", "reg 29(2)", "reg. 29(2)",
    "regulation 29", "reg 29", "reg. 29", "sast",
    "regulation 31(1)", "reg 31(1)", "reg. 31(1)",
    "regulation 31(2)", "reg 31(2)", "reg. 31(2)",
    "regulation 31", "reg 31", "reg. 31",
    "pledge", "encumbrance", "creation of pledge", "release of pledge",
    "revocation of pledge", "invocation of pledge",
    "promoter group", "acquisition of shares", "disposal of shares",
    "promoter acquisition", "market purchase by promoter"
]

CIRCULAR_KEYWORDS = [
    "special call auction",
    "periodic call auction",
    "illiquid securities",
    "special pre-open session",
    "price discovery"
]

MEETING_KEYWORDS = [
    "annual general meeting", " agm ", "e-voting", "evoting",
    "investor meet", "analyst meet", "earnings call",
    "conference call", "schedule of analyst", "investor presentation"
]

_AMOUNT_UNIT_PATTERN = re.compile(
    r"(?:rs\.?|inr|₹|usd|\$)\s*([\d,]+(?:\.\d+)?)\s*"
    r"(crore|cr\.?|lakh|lac|million|mn|billion|bn)\b",
    re.IGNORECASE,
)

_DATE_PATTERN = re.compile(
    r"\b("
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*,?\s*(?:20\d{2})|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*(?:20\d{2})|"
    r"\d{1,2}[-./]\d{1,2}[-./](?:20\d{2})"
    r")\b",
    re.IGNORECASE
)

# ----------------------------------------------------------------------
# E-VOTING / POSTAL BALLOT RESOLUTION PURPOSES
# ----------------------------------------------------------------------
RESOLUTION_PURPOSES = [
    ("Bonus Issue", re.compile(r"\b(?:bonus shares|bonus issue|issue of bonus)\b", re.IGNORECASE)),
    ("Stock Split", re.compile(r"\b(?:sub-division|subdivision|stock split|split of equity shares)\b", re.IGNORECASE)),
    ("Dividend", re.compile(r"\b(?:final dividend|interim dividend|special dividend|declaration of dividend)\b", re.IGNORECASE)),
    ("Preferential Issue / QIP", re.compile(r"\b(?:preferential allotment|preferential issue|private placement|qip|qualified institutional)\b", re.IGNORECASE)),
    ("Fund Raising / Borrowing", re.compile(r"\b(?:raising of funds|fund raising|borrowing powers|increase in borrowing|issue of debentures|ncds)\b", re.IGNORECASE)),
    ("Name Change", re.compile(r"\b(?:change of name|name change)\b", re.IGNORECASE)),
    ("Capital Increase", re.compile(r"\b(?:increase in authorized|authorised share capital)\b", re.IGNORECASE)),
    ("ESOP / Sweat Equity", re.compile(r"\b(?:esop|employee stock option|sweat equity)\b", re.IGNORECASE)),
    ("Buyback", re.compile(r"\b(?:buyback|buy-back of shares)\b", re.IGNORECASE)),
    ("Director / Auditor Appointment", re.compile(r"\b(?:appointment of|re-appointment of|remuneration of|independent director|statutory auditor)\b", re.IGNORECASE)),
    ("Related Party Transaction", re.compile(r"\b(?:related party transaction|material related party)\b", re.IGNORECASE)),
    ("Slump Sale / Business Sale", re.compile(r"\b(?:sale of undertaking|slump sale|transfer of business)\b", re.IGNORECASE)),
]

WATCHLIST = []  

SEEN_FILE = Path(__file__).parent / "seen_announcements.json"
LOG_FILE = Path(__file__).parent / "notifier.log"

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("result_notifier")

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram credentials not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
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
# TIME & ANTI-BLOCKING UTILITIES
# ----------------------------------------------------------------------

def is_polling_allowed_now() -> bool:
    now = datetime.datetime.now(IST) if IST else datetime.datetime.now()
    weekday = now.weekday()

    if weekday < 5:  
        start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        end = now.replace(hour=22, minute=30, second=0, microsecond=0)
    else:  
        start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        end = now.replace(hour=21, minute=0, second=0, microsecond=0)

    return start <= now <= end

def get_browser_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

# ----------------------------------------------------------------------
# ANNOUNCEMENT STORE & PARSING
# ----------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            log.warning("Could not parse %s, starting fresh.", SEEN_FILE)
    return set()

def save_seen(seen: set):
    trimmed = list(seen)[-6000:]
    SEEN_FILE.write_text(json.dumps(trimmed))

def normalise_company(name: str) -> str:
    name = name.upper()
    name = re.sub(r"\b(LIMITED|LTD|LTD\.|THE)\b", "", name)
    return re.sub(r"[^A-Z0-9]", "", name).strip()

def fingerprint(company: str, subject: str, date_str: str) -> str:
    date_only = date_str[:10]
    subj_key = re.sub(r"[^A-Za-z0-9]", "", subject.upper())[:40]
    return f"{normalise_company(company)}|{subj_key}|{date_only}"

def extract_order_value(text: str):
    if not text:
        return None
    match = _AMOUNT_UNIT_PATTERN.search(text)
    if not match:
        return None
    return f"{match.group(1)} {match.group(2)}"

def extract_meeting_date(text: str):
    if not text:
        return None
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    return match.group(1).strip()

def extract_evoting_purpose(text: str) -> str | None:
    if not text:
        return None
    matched = []
    for label, pattern in RESOLUTION_PURPOSES:
        if pattern.search(text):
            matched.append(label)
    if matched:
        return ", ".join(matched[:3])
    return None

def classify_announcement(subject: str) -> str:
    subj_lower = f" {subject.lower()} "
    
    if "trading window" in subj_lower:
        return None
        
    if any(kw in subj_lower for kw in ORDER_KEYWORDS):
        return "order"
    if any(kw in subj_lower for kw in MEETING_KEYWORDS):
        return "meeting"
    if any(kw in subj_lower for kw in INSIDER_PROMOTER_KEYWORDS):
        return "insider_promoter"
    if any(kw in subj_lower for kw in RESULT_KEYWORDS) or ("board meeting" in subj_lower and "result" in subj_lower):
        return "result"
        
    return None

def matches_watchlist(company: str, symbol: str) -> bool:
    if not WATCHLIST:
        return True
    norm_watch = {normalise_company(w) for w in WATCHLIST}
    return normalise_company(company) in norm_watch or symbol.upper() in {w.upper() for w in WATCHLIST}

# ----------------------------------------------------------------------
# EXCHANGE FETCHERS
# ----------------------------------------------------------------------

def fetch_nse_announcements() -> list:
    session = requests.Session()
    headers = get_browser_headers()
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=12)
        time.sleep(random.uniform(1.2, 2.0))
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers={**headers, "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("NSE announcements unavailable: %s", e)
        return []

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []
    if not isinstance(data, list):
        return []

    results = []
    for item in data:
        company = item.get("sm_name") or item.get("symbol", "")
        subject = f"{item.get('desc') or ''} {item.get('attchmntText') or ''}".strip()
        date_str = item.get("an_dt") or item.get("attchmntFile", "") or ""
        results.append({
            "company": company,
            "symbol": item.get("symbol", ""),
            "subject": subject,
            "date": date_str,
            "source": "NSE",
            "link": item.get("attchmntFile", ""),
        })
    return results

def fetch_bse_announcements() -> list:
    headers = {
        **get_browser_headers(),
        "Accept": "application/json",
        "Referer": "https://www.bseindia.com/corporates/ann.html",
    }
    today = datetime.datetime.now().strftime("%Y%m%d")
    from_date = (datetime.datetime.now() - datetime.timedelta(days=2)).strftime("%Y%m%d")
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P"
        f"&strToDate={today}&strType=C&subcategory=-1"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("BSE announcements unavailable: %s", e)
        return []

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []
    if not isinstance(data, dict) or "Table" not in data:
        return []

    results = []
    for item in data.get("Table", []):
        company = item.get("SLONGNAME") or item.get("SCRIP_CD", "")
        subject = f"{item.get('NEWSSUB') or ''} {item.get('HEADLINE') or ''}".strip()
        date_str = item.get("NEWS_DT") or item.get("DissemDT", "") or ""
        results.append({
            "company": company,
            "symbol": str(item.get("SCRIP_CD", "")),
            "subject": subject,
            "date": date_str,
            "source": "BSE",
            "link": item.get("ATTACHMENTNAME", ""),
        })
    return results

def fetch_nse_circulars() -> list:
    session = requests.Session()
    headers = get_browser_headers()
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=12)
        time.sleep(random.uniform(1.2, 2.0))
        resp = session.get(
            "https://www.nseindia.com/api/circulars",
            headers={**headers, "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("NSE Circulars unavailable: %s", e)
        return []

    items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    
    results = []
    for item in items:
        subject = item.get("sub", "")
        subj_lower = subject.lower()
        
        if any(kw in subj_lower for kw in CIRCULAR_KEYWORDS):
            circ_no = item.get("circNo", "Unknown")
            circ_file = item.get("circFile", "")
            link = f"https://archives.nseindia.com/content/circulars/{circ_file}" if circ_file else ""
            
            results.append({
                "company": "NSE EXCHANGE",
                "symbol": "CIRCULAR",
                "subject": f"[{circ_no}] {subject}",
                "date": item.get("circDt", datetime.date.today().isoformat()),
                "source": "NSE",
                "link": link,
                "category": "circular"
            })
    return results

# ----------------------------------------------------------------------
# MAIN POLLING LOOP
# ----------------------------------------------------------------------

def poll_once(seen: set) -> set:
    nse_items = fetch_nse_announcements()
    time.sleep(random.uniform(1.0, 2.0))
    bse_items = fetch_bse_announcements()
    time.sleep(random.uniform(1.0, 2.0))
    nse_circulars = fetch_nse_circulars()

    all_items = nse_items + bse_items + nse_circulars
    log.info("Fetched %d raw filings (NSE: %d, BSE: %d, Circ: %d).", 
             len(all_items), len(nse_items), len(bse_items), len(nse_circulars))

    new_alerts = []
    for item in all_items:
        if item.get("category") == "circular":
            fp = fingerprint(item["company"], item["subject"], item["date"])
            if fp not in seen:
                seen.add(fp)
                new_alerts.append(item)
            continue
            
        if not item.get("company") or not item.get("subject"):
            continue
        if not matches_watchlist(item["company"], item["symbol"]):
            continue

        category = classify_announcement(item["subject"])
        if not category:
            continue

        fp = fingerprint(item["company"], item["subject"], item["date"])
        if fp in seen:
            continue

        seen.add(fp)
        item["category"] = category
        new_alerts.append(item)

    for item in new_alerts:
        cat = item["category"]
        
        if cat == "circular":
            header = f"🏛️ <b>{item['company']}</b> \u2014 Market Wide Circular"
            body = f"{item['subject']}\n\U0001F550 {item['date']}"
            
        elif cat == "order":
            order_val = extract_order_value(item["subject"])
            val_line = f"\U0001F4B0 Value: \u20b9{order_val}\n" if order_val else "\U0001F4B0 Value: check filing\n"
            header = f"\U0001F4E6 <b>{item['company']}</b> ({item['source']}) \u2014 Order/Contract Win"
            body = f"{item['subject']}\n{val_line}\U0001F550 {item['date']}"
            
        elif cat == "insider_promoter":
            header = f"\U0001F50D <b>{item['company']}</b> ({item['source']}) \u2014 Insider / Promoter Action"
            body = f"{item['subject']}\n\U0001F550 {item['date']}"
            
        elif cat == "meeting":
            meet_date = extract_meeting_date(item["subject"])
            evoting_purpose = extract_evoting_purpose(item["subject"])
            
            if (not meet_date or not evoting_purpose) and item.get("link") and PyPDF2 is not None:
                try:
                    time.sleep(random.uniform(1.0, 2.0))
                    pdf_resp = requests.get(item["link"], headers=get_browser_headers(), timeout=15)
                    if pdf_resp.status_code == 200:
                        with io.BytesIO(pdf_resp.content) as f:
                            reader = PyPDF2.PdfReader(f)
                            if len(reader.pages) > 0:
                                pdf_text = reader.pages[0].extract_text() or ""
                                if len(reader.pages) > 1 and len(pdf_text) < 400:
                                    pdf_text += " " + (reader.pages[1].extract_text() or "")
                                
                                if not meet_date:
                                    meet_date = extract_meeting_date(pdf_text)
                                if not evoting_purpose:
                                    evoting_purpose = extract_evoting_purpose(pdf_text)
                except Exception as e:
                    log.warning("PDF extraction failed for %s: %s", item['company'], e)
            
            date_line = f"🗓️ Date: <b>{meet_date}</b>\n" if meet_date else "🗓️ Date: <i>Check attached PDF</i>\n"
            purpose_line = f"🗳️ Purpose: <b>{evoting_purpose}</b>\n" if evoting_purpose else ""
            
            header = f"📅 <b>{item['company']}</b> ({item['source']}) \u2014 AGM / E-Voting / Meet"
            body = f"{item['subject']}\n{purpose_line}{date_line}\U0001F550 {item['date']}"
            
        else:  # result
            header = f"\U0001F4E2 <b>{item['company']}</b> ({item['source']}) \u2014 Financial Result"
            body = f"{item['subject']}\n\U0001F550 {item['date']}"

        msg = f"{header}\n{body}"
        if item.get("link"):
            msg += f"\n\U0001F517 {item['link']}"

        send_telegram_message(msg)
        log.info("Alert dispatched [%s]: %s", cat.upper(), item["company"])
        time.sleep(1.0)

    return seen

def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Notifier.%s", " (single-shot mode)" if one_shot else "")
    seen = load_seen()

    if one_shot:
        if is_polling_allowed_now():
            try:
                seen = poll_once(seen)
                save_seen(seen)
            except Exception as e:
                log.exception("Error during single poll: %s", e)
        else:
            log.info("Outside allowed operating schedule -- skipping.")
        return

    while True:
        if is_polling_allowed_now():
            try:
                seen = poll_once(seen)
                save_seen(seen)
            except Exception as e:
                log.exception("Error during cycle: %s", e)
        else:
            log.info("Outside allowed operating schedule -- sleeping.")
        time.sleep(POLL_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
