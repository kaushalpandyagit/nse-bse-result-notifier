"""
NSE + BSE Live Result, Order Win, Insider Trade, Circular, Meeting & Business Update Notifier
==========================================================================================
File: result_notifier.py

Coverage:
  1. Financial Results (Regulation 33 / Board outcomes)
  2. Order Wins + Order-to-Market-Cap Asymmetry Triggers (>= 20% of MCap)
  3. Asset Commissioning & Capacity Expansion (Commercial Production / CWIP)
  4. Multi-Modal Logistics & Terminals (Gati Shakti, Railway Sidings)
  5. Pre-Earnings Quarterly Business / Operational Updates (Provisional Numbers,
     Advances & Deposits, AUM updates, Standalone Revenue)
  6. Insider Trading & Promoter Actions (with Buy / Sell classification)
  7. NSE Exchange Circulars
  8. Automated Catalyst History Logger (company_catalyst_history.json)
  9. Operating Schedule: Weekdays 08:00-22:30 IST, Weekends 09:00-21:00 IST
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
import html
from pathlib import Path

import requests

try:
    import yfinance as yf
except ImportError:
    yf = None
    print("WARNING: yfinance not installed. Market cap filtering will safely default to passing all SMEs.")

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
    print("WARNING: PyPDF2 is not installed. PDF text extraction will be disabled.")

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

SCRIPT_TAG = "🤖 [result_notifier.py]"

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

# Catches DMart, HDFC Bank, Bank of Baroda, Bajaj Finance, Trent, GCPL phrasing
BUSINESS_UPDATE_KEYWORDS = [
    "business update", "operational update", "quarterly update",
    "provisional data", "provisional figures", "provisional numbers",
    "provisional update", "performance update", "key operational",
    "business highlights", "updates on operational", "update on operations",
    "standalone revenue", "revenue from operations", "key business parameters",
    "provisional business", "business parameters", "business performance",
    "advances and deposits", "deposits and advances", "update on advances",
    "aum update", "update on aum", "provisional key", "sales volume",
    "quarterly sales", "gross advances", "operating performance"
]

ORDER_KEYWORDS = [
    "award of order", "awarded order", "awarded contract", "award of contract",
    "receipt of order", "received order", "receipt of contract", "bagging of order",
    "bagging of contract", "bags order", "bags contract", "secures order",
    "secured order", "secures contract", "wins order", "wins contract",
    "letter of intent", "l.o.i.", " loi ", "letter of award",
    "l.o.a.", " loa ", "purchase order", "work order",
    "order/contract", "order / contract",
    "award_of_order", "receipt_of_order", "orders/contracts", "bagging/receiving"
]

COMMISSIONING_KEYWORDS = [
    "commercial production", "commissioning", "commencement of commercial",
    "commences commercial", "cwip", "capital work-in-progress", "brownfield",
    "commercial operations", "commencement of operation", "capacity expansion"
]

LOGISTICS_KEYWORDS = [
    "railway siding", "gati shakti", "cargo terminal", "logistics park", 
    "multi-modal", "multimodal"
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
    "conference call", "schedule of analyst", "investor presentation",
    "institutional investor"
]

NOISE_KEYWORDS = [
    "agm", "e-voting", "evoting", "newspaper", "corrigendum", "proceedings", 
    "annual general meeting", "postal ballot", "notice of", "book closure"
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
CATALYST_FILE = Path(__file__).parent / "company_catalyst_history.json"
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
# SCHEDULE & UTILITIES
# ----------------------------------------------------------------------

def get_ist_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)

def is_polling_allowed_now() -> bool:
    now = get_ist_now()
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
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

# ----------------------------------------------------------------------
# ANNOUNCEMENT STORAGE & PARSERS
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

def load_catalyst_history() -> dict:
    if CATALYST_FILE.exists():
        try:
            return json.loads(CATALYST_FILE.read_text())
        except Exception:
            pass
    return {}

def save_catalyst_history(data: dict):
    CATALYST_FILE.write_text(json.dumps(data, indent=2))

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

def parse_to_crores(amount_str: str) -> float:
    if not amount_str:
        return 0.0
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*(crore|cr\.?|lakh|lac|million|mn|billion|bn)\b", amount_str, re.IGNORECASE)
    if not match:
        return 0.0
    try:
        val = float(match.group(1).replace(",", ""))
        unit = match.group(2).lower().replace(".", "")
        if unit in ["crore", "cr"]: return val
        if unit in ["lakh", "lac"]: return val / 100.0
        if unit in ["million", "mn"]: return val / 10.0
        if unit in ["billion", "bn"]: return val * 100.0
    except ValueError:
        pass
    return 0.0

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

def extract_insider_summary(text: str) -> str:
    if not text:
        return ""
    text_lower = text.lower()
    text_stripped = text_lower.replace("substantial acquisition of shares", "")
    text_stripped = text_stripped.replace("prohibition of insider trading", "")
    text_stripped = text_stripped.replace("details of acquisition/sale", "")
    text_stripped = text_stripped.replace("acquired/disposed", "")
    
    summaries = []
    if "pledge" in text_stripped or "encumbrance" in text_stripped or "31(1)" in text_stripped or "31(2)" in text_stripped:
        if "creation of" in text_stripped or "created" in text_stripped:
            summaries.append("Creation of Pledge 🔒")
        if "release of" in text_stripped or "released" in text_stripped or "revocation" in text_stripped:
            summaries.append("Release of Pledge 🔓")
        if "invocation" in text_stripped or "invoked" in text_stripped:
            summaries.append("Invocation of Pledge ⚠️")
            
    if "form c" in text_stripped or "29(2)" in text_stripped or "29(1)" in text_stripped or "7(2)" in text_stripped:
        if "market purchase" in text_stripped or "open market purchase" in text_stripped:
            summaries.append("Market Purchase (Buy) 🟢")
        elif "market sale" in text_stripped or "open market sale" in text_stripped:
            summaries.append("Market Sale (Sell) 🔴")
        elif "esop" in text_stripped:
            summaries.append("ESOP Allotment 🟢")
        elif "gift" in text_stripped:
            summaries.append("Gift / Transfer 🎁")
        else:
            acq_c = text_stripped.count("acquired") + text_stripped.count("acquisition") + text_stripped.count("purchase")
            disp_c = text_stripped.count("disposed") + text_stripped.count("sale") + text_stripped.count("sold")
            if acq_c > disp_c * 2:
                summaries.append("Acquisition of Shares 🟢")
            elif disp_c > acq_c * 2:
                summaries.append("Disposal of Shares 🔴")

    if not summaries:
        return
