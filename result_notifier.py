"""
NSE + BSE Live Result / Corporate Announcement Notifier -> Telegram
=====================================================================

What this does
---------------
Every POLL_INTERVAL_MINUTES, this script:
  1. Fetches the latest corporate announcements from NSE and BSE.
  2. Filters them to financial-result type filings (configurable) --
     AND, separately, order/contract-win filings (new -- see below).
  3. De-duplicates announcements that appear on BOTH exchanges for the
     same company (many companies are dual-listed), using a
     (company_symbol_normalised + subject + date) fingerprint.
  4. Sends a Telegram message for every NEW announcement it hasn't seen
     before (tracked in seen_announcements.json so restarts don't
     re-send old alerts).
  5. Only polls during NSE/BSE trading hours on trading days
     (Mon-Fri, 9:00 AM - 4:00 PM IST) to avoid hammering the APIs
     when the market is shut. Set POLL_ONLY_MARKET_HOURS = False to
     override (e.g. for testing).

ORDER / CONTRACT WIN DETECTION (new)
--------------------------------------
Alongside financial results, this now also detects "Award of Order",
"Receipt of Order/Contract", "Bagging of Contract" type filings --
the same general announcement category NSE/BSE use for things like
".../SUNGARNER_..._NSE_NTPC.pdf" style order-win disclosures.

When such a filing is found, the script also attempts to extract a
rupee VALUE from the available announcement text (subject + NSE's
attchmntText field, when present) using pattern matching for common
phrasings like "Rs. 150 crore", "₹45.2 Cr", "INR 12 lakh", etc.

Caveats, so the numbers aren't over-trusted:
  - BSE's API only exposes a short headline/subject, not the full
    attachment text -- so order VALUE extraction will often come up
    empty for BSE-sourced announcements even when the underlying PDF
    states one. NSE's attchmntText field is more likely to have it,
    but is not guaranteed either (some filings only state the value
    inside the PDF itself, which this script does not download/OCR).
  - When no amount can be confidently parsed, the alert says so
    explicitly rather than omitting it silently -- check the linked
    filing yourself for the exact figure when it matters.
  - This is a best-effort text scan, not a financial-data extraction
    engine. Always verify against the source PDF before acting.

One-time setup
---------------
1. Create a Telegram bot:
     - Open Telegram, search for "BotFather", send /newbot
     - Follow prompts -> you'll get a BOT TOKEN like
       123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
2. Get your chat ID:
     - Search for "userinfobot" on Telegram, send /start
       -> it replies with your numeric chat ID.
     - (Or message your new bot anything, then visit
        https://api.telegram.org/bot<TOKEN>/getUpdates in a browser
        and read the "chat":{"id": ...} field.)
3. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below (or set them
   as environment variables of the same name -- recommended so you
   never commit secrets to version control).
4. pip install requests pytz
5. python result_notifier.py

Notes on the data sources
--------------------------
- NSE blocks requests without browser-like headers and a valid
  session cookie. This script first hits nseindia.com's homepage to
  pick up cookies, then calls the corporate-announcements API.
- NSE occasionally rate-limits / changes its API. If NSE calls start
  failing, the script logs the error and continues with BSE only
  (and vice versa) rather than crashing.
- BSE's announcement API is generally more stable and needs no
  cookie dance.
"""

import os
import re
import sys
import json
import time
import logging
import datetime
from pathlib import Path

import requests

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

# ----------------------------------------------------------------------
# CONFIG -- edit these
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

# How often to poll, in minutes.
POLL_INTERVAL_MINUTES = 15

# Only poll Mon-Fri 9:00-16:00 IST. Set False to poll around the clock
# (useful for testing outside market hours).
POLL_ONLY_MARKET_HOURS = True

# Announcement-type filter. The script checks each announcement's
# "subject" text (case-insensitive) against these keywords; if ANY
# keyword matches, it's treated as a "result" filing and alerted.
# To track ALL corporate announcements (not just results), set this
# to an empty list: RESULT_KEYWORDS = []
RESULT_KEYWORDS = [
    "financial result",
    "financial results",
    "quarterly result",
    "quarterly results",
    "board meeting outcome",
    "un-audited",
    "unaudited",
    "audited financial",
    "results for the quarter",
    "results for the year",
    "regulation 33",
    "reg. 33",
    "reg 33",
    "standalone and consolidated financial",
    "submitted to the exchange",
]

# Order / contract-win filing keywords (new). Matches NSE/BSE's
# "Award of Order / Receipt of Order / Bagging of Contract" style
# announcement category.
ORDER_KEYWORDS = [
    "award of order",
    "awarded order",
    "awarded contract",
    "award of contract",
    "receipt of order",
    "received order",
    "receipt of contract",
    "bagging of order",
    "bagging of contract",
    "bags order",
    "bags contract",
    "secures order",
    "secured order",
    "secures contract",
    "wins order",
    "wins contract",
    "letter of intent",
    "l.o.i.",
    " loi ",
    "letter of award",
    "l.o.a.",
    " loa ",
    "purchase order",
    "work order",
    "order/contract",
    "order / contract",
]

# Regex patterns to pull a rupee amount out of announcement text.
# Matches things like "Rs. 150 crore", "₹45.2 Cr", "INR 12 lakh",
# "Rs 1,250 Crores", "USD 3 million" (kept, since some order filings
# quote value in USD for export contracts).
_AMOUNT_UNIT_PATTERN = re.compile(
    r"(?:rs\.?|inr|₹|usd|\$)\s*([\d,]+(?:\.\d+)?)\s*"
    r"(crore|cr\.?|lakh|lac|million|mn|billion|bn)\b",
    re.IGNORECASE,
)


def extract_order_value(text: str):
    """Best-effort scan for a rupee/dollar value mentioned in the
    announcement text. Returns a formatted string like "₹150 crore"
    (using whatever currency word and unit were found in the source
    text, not necessarily normalised) or None if nothing matched.
    See module docstring for caveats -- this is best-effort, not
    guaranteed, especially for BSE announcements (short text only)."""
    if not text:
        return None
    match = _AMOUNT_UNIT_PATTERN.search(text)
    if not match:
        return None
    amount, unit = match.group(1), match.group(2)
    return f"{amount} {unit}"


# Empty = track every company. To restrict to specific tickers, add
# NSE symbols / BSE scrip names here, e.g. ["RELIANCE", "TCS", "INFY"]
WATCHLIST = []

SEEN_FILE = Path(__file__).parent / "seen_announcements.json"
LOG_FILE = Path(__file__).parent / "notifier.log"

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("result_notifier")

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram bot token / chat id not configured. Edit the CONFIG "
                   "section or set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars.")
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
        if resp.status_code != 200:
            log.error("Telegram send failed [%s]: %s", resp.status_code, resp.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram send exception: %s", e)
        return False


# ----------------------------------------------------------------------
# SEEN-ANNOUNCEMENTS STORE (dedup across NSE + BSE + restarts)
# ----------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            log.warning("Could not parse %s, starting fresh.", SEEN_FILE)
    return set()


def save_seen(seen: set):
    # Keep the file from growing forever -- only keep last 5000 fingerprints
    trimmed = list(seen)[-5000:]
    SEEN_FILE.write_text(json.dumps(trimmed))


def normalise_company(name: str) -> str:
    """Strip suffixes like 'Ltd', 'Limited', punctuation, extra spaces,
    so the same company filed under slightly different names on NSE vs
    BSE still produces the same fingerprint."""
    name = name.upper()
    name = re.sub(r"\b(LIMITED|LTD|LTD\.|THE)\b", "", name)
    name = re.sub(r"[^A-Z0-9]", "", name)
    return name.strip()


def fingerprint(company: str, subject: str, date_str: str) -> str:
    # date_str truncated to just the date (not time) so the same
    # announcement re-fetched with a slightly different timestamp
    # still dedupes correctly.
    date_only = date_str[:10]
    subj_key = re.sub(r"[^A-Za-z0-9]", "", subject.upper())[:40]
    return f"{normalise_company(company)}|{subj_key}|{date_only}"


# ----------------------------------------------------------------------
# NSE FETCH
# ----------------------------------------------------------------------

def fetch_nse_announcements() -> list:
    """Returns list of dicts: {company, subject, date, source, link}"""
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        # Prime cookies by visiting the homepage first -- NSE rejects
        # direct API calls without a valid session cookie.
        session.get("https://www.nseindia.com", headers=headers, timeout=15)
        time.sleep(1)
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("NSE fetch failed: %s", e)
        return []

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            log.error(
                "NSE returned a non-JSON string response (likely blocked "
                "or rate-limited). First 300 chars: %r", data[:300],
            )
            return []

    if not isinstance(data, list):
        log.error(
            "NSE response was not the expected list format. Got type=%s. "
            "Raw (first 300 chars): %r", type(data).__name__, str(data)[:300],
        )
        return []

    results = []
    for item in data:
        company = item.get("sm_name") or item.get("symbol", "")
        subject = f"{item.get('desc') or ''} {item.get('attchmntText') or ''}"
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


# ----------------------------------------------------------------------
# BSE FETCH
# ----------------------------------------------------------------------

def fetch_bse_announcements() -> list:
    """Returns list of dicts: {company, subject, date, source, link}"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://www.bseindia.com/corporates/ann.html",
    }
    today = datetime.datetime.now().strftime("%Y%m%d")
    from_date = (datetime.datetime.now() - datetime.timedelta(days=3)).strftime("%Y%m%d")
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
        log.error("BSE fetch failed: %s", e)
        return []

    # BSE's API sometimes returns a JSON-encoded STRING instead of an
    # object (e.g. double-encoded JSON, or a plain error message like
    # "Please provide valid Request"). Handle both cases gracefully
    # instead of crashing.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            log.error(
                "BSE returned a non-JSON string response (likely an API "
                "error or blocked request). First 300 chars: %r",
                data[:300],
            )
            return []

    if not isinstance(data, dict) or "Table" not in data:
        log.error(
            "BSE response was not in the expected format. Got type=%s, "
            "keys=%s. Raw (first 300 chars): %r",
            type(data).__name__,
            list(data.keys()) if isinstance(data, dict) else "n/a",
            str(data)[:300],
        )
        return []

    results = []
    for item in data.get("Table", []):
        company = item.get("SLONGNAME") or item.get("SCRIP_CD", "")
        subject = f"{item.get('NEWSSUB') or ''} {item.get('HEADLINE') or ''}"
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


# ----------------------------------------------------------------------
# FILTERING
# ----------------------------------------------------------------------

def is_result_announcement(subject: str) -> bool:
    if not RESULT_KEYWORDS:
        return True  # no filter -> everything counts
    subj_lower = subject.lower()
    if any(kw in subj_lower for kw in RESULT_KEYWORDS):
        return True
    return "board meeting" in subj_lower and "result" in subj_lower


def is_order_announcement(subject: str) -> bool:
    """True if the subject text matches an order/contract-win filing
    (Award of Order, Receipt of Contract, Bagging of Order, etc.)."""
    subj_lower = f" {subject.lower()} "
    return any(kw in subj_lower for kw in ORDER_KEYWORDS)


def matches_watchlist(company: str, symbol: str) -> bool:
    if not WATCHLIST:
        return True  # no watchlist -> everything counts
    norm_watch = {normalise_company(w) for w in WATCHLIST}
    return (
        normalise_company(company) in norm_watch
        or symbol.upper() in {w.upper() for w in WATCHLIST}
    )


# ----------------------------------------------------------------------
# MARKET HOURS CHECK
# ----------------------------------------------------------------------

def is_market_hours_now() -> bool:
    if not POLL_ONLY_MARKET_HOURS:
        return True
    now = datetime.datetime.now(IST) if IST else datetime.datetime.now()
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

def poll_once(seen: set) -> set:
    all_items = fetch_nse_announcements() + fetch_bse_announcements()
    log.info("Fetched %d raw announcements (NSE+BSE combined).", len(all_items))

    for item in all_items:
        log.debug("RAW: [%s] %s -- %s", item["source"], item["company"], item["subject"][:80])

    new_alerts = []
    for item in all_items:
        if not item["company"] or not item["subject"]:
            continue
        if not matches_watchlist(item["company"], item["symbol"]):
            continue

        is_result = is_result_announcement(item["subject"])
        is_order = is_order_announcement(item["subject"])
        if not is_result and not is_order:
            continue

        fp = fingerprint(item["company"], item["subject"], item["date"])
        if fp in seen:
            continue  # already alerted (possibly via the other exchange)

        seen.add(fp)
        item["category"] = "result" if is_result else "order"
        new_alerts.append(item)

    for item in new_alerts:
        if item["category"] == "order":
            order_value = extract_order_value(item["subject"])
            value_line = (f"\U0001F4B0 Value: \u20b9{order_value}\n" if order_value
                          else "\U0001F4B0 Value: not stated in available text -- check filing\n")
            msg = (
                f"\U0001F4E6 <b>{item['company']}</b> ({item['source']}) -- Order/Contract Win\n"
                f"{item['subject']}\n"
                f"{value_line}"
                f"\U0001F550 {item['date']}"
            )
        else:
            msg = (
                f"\U0001F4E2 <b>{item['company']}</b> ({item['source']})\n"
                f"{item['subject']}\n"
                f"\U0001F550 {item['date']}"
            )
        if item.get("link"):
            msg += f"\n\U0001F517 {item['link']}"
        sent = send_telegram_message(msg)
        log.info("%s %s alert for %s: %s", "Sent" if sent else "FAILED",
                  item["category"], item["company"], item["subject"][:60])
        time.sleep(1)  # be gentle on Telegram's rate limit

    if not new_alerts:
        log.info("No new result/order filings this cycle.")

    return seen


def main():
    one_shot = "--once" in sys.argv

    log.info("Starting NSE+BSE result/order notifier. Polling every %d minutes.%s",
              POLL_INTERVAL_MINUTES, " (single-shot mode)" if one_shot else "")
    seen = load_seen()

    if one_shot:
        # Used by scheduled runners (e.g. GitHub Actions) where the
        # scheduler itself controls timing -- run exactly one poll and exit.
        if is_market_hours_now():
            try:
                seen = poll_once(seen)
                save_seen(seen)
            except Exception as e:
                log.exception("Unexpected error during poll: %s", e)
        else:
            log.info("Outside market hours -- skipping.")
        return

    while True:
        if is_market_hours_now():
            try:
                seen = poll_once(seen)
                save_seen(seen)
            except Exception as e:
                log.exception("Unexpected error during poll: %s", e)
        else:
            log.info("Outside market hours -- skipping this cycle.")

        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
