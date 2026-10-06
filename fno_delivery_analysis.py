"""
Daily F&O, Delivery Data, and Ace Investor Analysis -> Telegram
================================================================

Runs ONCE per trading day, after market close (when NSE's daily
Bhavcopy and Bulk/Block deal files are finalized, typically by ~6:30 PM IST).
Includes automated Day-over-Day Delta & Percentage Directional Shift tracking.
"""

import os
import io
import re
import sys
import json
import zipfile
import logging
import datetime
import time
from pathlib import Path

import requests
import pandas as pd

from email_notifier import send_email

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

SCRIPT_TAG = "🤖 [fno_delivery_analysis.py]"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

PARTICIPANT_STATE_FILE = Path(__file__).parent / "fno_participant_state.json"

# ----------------------------------------------------------------------
# ACE INVESTOR & INSTITUTIONAL WATCHLIST
# ----------------------------------------------------------------------
ACE_INVESTORS = [
    # Top Ace Individuals & Family Offices
    "ASHISH KACHOLIA", "RADHAKISHAN SHIVKISHAN DAMANI", "DOLLY KHANNA",
    "MUKUL MAHAVIR AGRAWAL", "ASHA MUKUL AGRAWAL", "SURESH KUMAR AGARWAL",
    "MANOJ AGARWAL", "MADHUSUDAN MURLIDHAR KELA", "MADHURI MADHUSUDAN KELA",
    "AKASH BHANSALI", "MANGAL BHANSHALI", "MEENU MANGAL BHANSHALI",
    "VALLABH ROOPCHAND BHANSHALI", "ASHISH DHAWAN", "AKHIL DHAWAN",
    "AJAY SHIVNARAIN UPADHYAYA", "NIKHIL KISHORCHANDRA VORA", "ARUN KUMAR MUKHERJEE",
    "ZAKI ABBAS NASSER", "DHEERAK KUMAR LOHIA", "AMAL PARIKH", "GOVINDLAL M. PARIKH",
    "SEETHA KUMARI", "MATHURBHAI SHIVARAM PATEL", "VISHWAS AMBALAL PATEL",
    "RAJASHEKAR S. IYER", "SUNIL GUL BIJLANI", "PANKAJ PRASOON",
    "RAHUL JAYANTILAL SHAH", "CHETAN JAYANTILAL SHAH", "RAMESH CHIMANLAL SHAH",
    "SHANKAR SHASHI SHARMA", "GIRISH GULATI", "MANOHAR DEVABHAKTUMI",
    "MUTHUKRISHNAN DHANDAPANI", "ARPANA SAMIRBHAI MACWAN", "MUTHU SUBRAMANIAN JAGADEESH",
    "MUTHU MANICKAM", "ADITYA K. HALWASIYA",
    "REKHA JHUNJHUNWALA", "RARE ENTERPRISES", "VIJAY KEDIA", "KEDIA SECURITIES",
    "NEMISH SHAH", "ANIL KUMAR GOEL", "RAMESH DAMANI", "PORINJU VELIYATH",
    
    # Top Indian Institutions (DIIs), MFs, & PMS
    "LIFE INSURANCE CORPORATION OF INDIA", "LIC OF INDIA", "SBI MUTUAL FUND", 
    "HDFC MUTUAL FUND", "ICICI PRUDENTIAL", "NIPPON INDIA", "KOTAK MUTUAL FUND", 
    "AXIS MUTUAL FUND", "ADITYA BIRLA SUN LIFE", "DSP MUTUAL FUND", 
    "TATA MUTUAL FUND", "CANARA ROBECO", "QUANT MUTUAL FUND", "SUNDARAM MUTUAL FUND", 
    "BANK OF INDIA", "ABAKKUS", "MALABAR INDIA", "AMANSA HOLDINGS", "INDIA EMERGING GIANTS",
    "ENAM INVESTMENT", "MOTILAL OSWAL", "AEQUITAS EQUITY", "SIXTH SENSE INDIA", 
    "AUTHUM INVESTMENT", "GIRIRAJ STOCK BROKING", "3P INDIA EQUITY", "HEM FINLEASE",
    "ARROW EMERGING OPPORTUNITIES", "SAGEONE", "BANDHAN SMALL CAP", "360 ONE",
    "ZERODHA BROKING", "AIRAN LIMITED", "MINARVA VENTURES", "VINEY EQUITY MARKET",
    "MINDPOOL TECHNOLOGIES", "OPALFORCE SOFTWARE",
    
    # Top Foreign Institutional Investors (FIIs) & Sovereign Funds
    "GOVERNMENT OF SINGAPORE", "MONETARY AUTHORITY OF SINGAPORE",
    "ABU DHABI INVESTMENT AUTHORITY", "ADIA", "NORGES BANK", "CARMIGNAC",
    "VANGUARD", "BLACKROCK", "ISHARES", "DIMENSIONAL FUND",
    "GOLDMAN SACHS", "MORGAN STANLEY", "NOMURA", "SOCIETE GENERALE", 
    "CITIGROUP", "BNP PARIBAS", "BOFA SECURITIES", "MERRILL LYNCH",
    "COPTHALL MAURITIUS", "ELARA INDIA"
]

DELIVERY_PCT_THRESHOLD = 60.0
DELIVERY_PRICE_MOVE_THRESHOLD = 2.0
OI_CHANGE_THRESHOLD = 5.0
TOP_N = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fno_analysis")


def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram not configured.")
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
            log.error("Telegram send failed [%s]: %s", resp.status_code, resp.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram send exception: %s", e)
        return False

def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

def get_session():
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=15)
        time.sleep(2)
    except Exception as e:
        log.warning("Could not prime NSE session: %s", e)
    return session


# ----------------------------------------------------------------------
# SMART MONEY: BULK & BLOCK DEALS
# ----------------------------------------------------------------------
def fetch_bulk_block_deals(session) -> tuple:
    symbols = set()
    ace_deals = []
    
    for report in ("bulk", "block"):
        url = f"https://archives.nseindia.com/content/equities/{report}.csv"
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text))
            df.columns = [str(c).strip().upper() for c in df.columns]
            
            sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
            client_col = next((c for c in df.columns if "CLIENT" in c), None)
            type_col = next((c for c in df.columns if "BUY" in c or "SELL" in c or "BUY/SELL" in c), None)
            qty_col = next((c for c in df.columns if "QUANTITY" in c or "QTY" in c), None)
            price_col = next((c for c in df.columns if "PRICE" in c), None)
            
            if not (sym_col and client_col and type_col and qty_col and price_col):
                continue

            for _, row in df.iterrows():
                symbol = str(row[sym_col]).strip().upper()
                client_name = str(row[client_col]).strip().upper()
                deal_type = str(row[type_col]).strip().upper()
                
                try:
                    qty = float(row[qty_col])
                    price = float(row[price_col])
                except (ValueError, TypeError):
                    continue
                
                symbols.add(symbol)
                
                for ace in ACE_INVESTORS:
                    if ace in client_name:
                        ace_deals.append({
                            "symbol": symbol,
                            "client": client_name,
                            "type": "BUY" if "BUY" in deal_type else "SELL",
                            "qty": qty,
                            "price": price,
                            "deal_type": report.title()
                        })
                        break
                        
        except Exception as e:
            log.warning("Could not fetch %s deals: %s", report, e)
            
    return symbols, ace_deals


# ----------------------------------------------------------------------
# DELIVERY % ANALYSIS
# ----------------------------------------------------------------------
def fetch_delivery_data(session, date: datetime.date) -> pd.DataFrame | None:
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date.strftime('%d%m%Y')}.csv"
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception as e:
        log.error("Delivery data fetch failed for %s: %s", date, e)
        return None

EARLY_MOVE_MIN = 2.0
EARLY_MOVE_MAX = 2.5

def analyze_delivery(df: pd.DataFrame) -> dict:
    all_signals = []
    try:
        df = df[df["SERIES"].str.strip() == "EQ"]
        for _, row in df.iterrows():
            try:
                symbol = str(row["SYMBOL"]).strip()
                close = float(row["CLOSE_PRICE"])
                prev_close = float(row["PREV_CLOSE"])
                deliv_pct = float(row["DELIV_PER"])
