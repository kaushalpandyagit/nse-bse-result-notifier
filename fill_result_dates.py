"""
Fill in result dates for a screener.in-style watchlist CSV, and seed
result-day-high baselines for tracking -- driven by YOUR list of
companies rather than a blind NSE/BSE date-range scan.

Expected input CSV columns (case-insensitive, extra columns ignored):
    Name, NSE Code
(BSE Code / ISIN Code are fine to leave in, they're just ignored.)

For each company:
  1. Queries NSE's per-symbol announcement history for a "financial
     result" type filing within the lookback window.
  2. If found, fetches that day's High from Yahoo Finance.
  3. Seeds a baseline into breakout_state.json (same file
     price_rsi_breakout.py uses for ongoing 15-min price/RSI checks)
     -- skips anything already tracked.
  4. Writes an output CSV with the filled-in result dates so you have
     a record of what was found.

Usage:
    python3 fill_result_dates.py my_watchlist.csv [--days N]

    --days N   How many days back to search for each company's result
               (default 100).

Output:
    result_dates_filled.csv  -- your input rows + ResultDate + DayHigh
    breakout_state.json      -- updated with new baselines (merged)
"""

import os
import re
import sys
import csv
import json
import time
import logging
import datetime
from pathlib import Path

import requests

try:
    import yfinance as yf
except ImportError:
    print("yfinance is required: pip install yfinance")
    raise

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

RESULT_KEYWORDS = [
    "financial result", "financial results", "quarterly result",
    "quarterly results", "board meeting outcome", "un-audited",
    "unaudited", "audited financial", "results for the quarter",
    "results for the year",
]

STATE_FILE = Path(__file__).parent / "breakout_state.json"
OUTPUT_CSV = Path(__file__).parent / "result_dates_filled.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fill_result_dates")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_nse_date(date_str: str):
    date_part = date_str.strip()
    m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", date_part)
    if m:
        day, mon_str, year = m.groups()
        month = _MONTHS.get(mon_str.lower())
        if month:
            try:
                return datetime.datetime(int(year), month, int(day))
            except ValueError:
                return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(date_part, fmt)
        except ValueError:
            continue
    return None


def is_result_announcement(subject: str) -> bool:
    subj_lower = subject.lower()
    return any(kw in subj_lower for kw in RESULT_KEYWORDS)


def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        resp = requests.post(url, data=payload, timeout=15)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def read_watchlist(csv_path: str) -> list:
    """Returns list of dicts: {name, nse_symbol, bse_code}. Either
    nse_symbol or bse_code may be empty, but not both -- rows with
    neither are skipped. Tolerant of column name variations
    (case-insensitive)."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = {fn.strip().lower(): fn for fn in reader.fieldnames or []}
        name_col = fieldnames.get("name")
        nse_col = None
        for candidate in ("nse code", "nse symbol", "symbol", "nsecode"):
            if candidate in fieldnames:
                nse_col = fieldnames[candidate]
                break
        bse_col = None
        for candidate in ("bse code", "bsecode", "scrip code", "scripcode"):
            if candidate in fieldnames:
                bse_col = fieldnames[candidate]
                break
        if nse_col is None and bse_col is None:
            raise ValueError(
                f"Could not find an NSE Code or BSE Code column. Found columns: {list(fieldnames.values())}"
            )
        for row in reader:
            nse_symbol = (row.get(nse_col) or "").strip().upper() if nse_col else ""
            bse_code = (row.get(bse_col) or "").strip() if bse_col else ""
            name = (row.get(name_col) or "").strip() if name_col else (nse_symbol or bse_code)
            if nse_symbol or bse_code:
                rows.append({"name": name, "nse_symbol": nse_symbol, "bse_code": bse_code})
    return rows


def get_session():
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=15)
        time.sleep(1)
    except Exception as e:
        log.warning("Could not prime NSE session: %s", e)
    return session


def fetch_nse_bulk_announcements(session, from_date: datetime.date, to_date: datetime.date) -> dict:
    """Fetches ALL NSE announcements in range in ONE call, and returns
    a dict of symbol -> earliest result-type announcement datetime.
    This is the proven-reliable approach (matches backfill_baselines.py)
    -- NSE's per-symbol endpoint does not reliably honor a wide date
    range, so we fetch broadly once and filter client-side instead."""
    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
    }
    try:
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements",
            headers=HEADERS, params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("NSE bulk fetch failed: %s", e)
        return {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return {}
    if not isinstance(data, list):
        return {}

    log.info("NSE bulk fetch returned %d announcements in range.", len(data))

    matches = {}
    for item in data:
        symbol = (item.get("symbol") or "").strip().upper()
        desc = item.get("desc") or ""
        attchmnt = item.get("attchmntText") or ""
        combined_text = f"{desc} {attchmnt}"
        date_str = item.get("an_dt") or ""
        if not symbol or not is_result_announcement(combined_text):
            continue
        parsed = parse_nse_date(date_str)
        if parsed is None:
            continue
        if symbol not in matches or parsed < matches[symbol]:
            matches[symbol] = parsed
    return matches


def find_result_date_bse(bse_code: str, from_date: datetime.date, to_date: datetime.date):
    """Queries BSE for this specific scrip code's announcements in
    range, returns the earliest result-type announcement's datetime,
    or None."""
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date.strftime('%Y%m%d')}"
        f"&strScrip={bse_code}&strSearch=P&strToDate={to_date.strftime('%Y%m%d')}"
        f"&strType=C&subcategory=-1"
    )
    try:
        resp = requests.get(
            url, headers={**HEADERS, "Referer": "https://www.bseindia.com/corporates/ann.html"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("BSE query failed for scrip %s: %s", bse_code, e)
        return None

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None

    best = None
    for item in data.get("Table", []):
        subject = item.get("NEWSSUB") or ""
        headline = item.get("HEADLINE") or ""
        combined_text = f"{subject} {headline}"
        date_str = item.get("NEWS_DT") or item.get("DissemDT", "") or ""
        if is_result_announcement(combined_text):
            parsed = parse_nse_date(date_str)  # handles ISO fallback too
            if parsed and (best is None or parsed < best):
                best = parsed
    return best


def get_day_high(yahoo_ticker: str, date: datetime.date):
    try:
        hist = yf.Ticker(yahoo_ticker).history(start=date, end=date + datetime.timedelta(days=1))
        if hist.empty:
            return None
        return float(hist["High"].iloc[0])
    except Exception as e:
        log.warning("Could not fetch day-high for %s on %s: %s", yahoo_ticker, date, e)
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 fill_result_dates.py <watchlist.csv> [--days N] [--debug-symbol SYMBOL]")
        sys.exit(1)

    csv_path = sys.argv[1]
    days_back = 100
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days_back = int(sys.argv[idx + 1])

    debug_symbol = None
    if "--debug-symbol" in sys.argv:
        idx = sys.argv.index("--debug-symbol")
        if idx + 1 < len(sys.argv):
            debug_symbol = sys.argv[idx + 1].strip().upper()

    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=days_back)

    if debug_symbol:
        log.info("DEBUG MODE: showing all raw NSE announcements for %s in range (no keyword filter).", debug_symbol)
        session = get_session()
        params = {
            "index": "equities",
            "from_date": from_date.strftime("%d-%m-%Y"),
            "to_date": to_date.strftime("%d-%m-%Y"),
        }
        resp = session.get("https://www.nseindia.com/api/corporate-announcements",
                            headers=HEADERS, params=params, timeout=30)
        data = resp.json()
        if isinstance(data, str):
            data = json.loads(data)
        count = 0
        for item in data:
            symbol = (item.get("symbol") or "").strip().upper()
            if symbol == debug_symbol:
                count += 1
                print(f"  Date: {item.get('an_dt')}")
                print(f"  desc: {item.get('desc')}")
                print(f"  attchmntText: {item.get('attchmntText')}")
                combined = f"{item.get('desc') or ''} {item.get('attchmntText') or ''}"
                print(f"  Matches RESULT_KEYWORDS (combined desc+attchmntText): {is_result_announcement(combined)}")
                print("  ---")
        if count == 0:
            print(f"  No announcements at all found for {debug_symbol} in this date range.")
        return

    watchlist = read_watchlist(csv_path)
    log.info("Loaded %d companies from %s.", len(watchlist), csv_path)

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("Could not parse existing state, starting fresh.")

    session = get_session()
    nse_bulk_matches = fetch_nse_bulk_announcements(session, from_date, to_date)
    log.info("NSE bulk lookup ready: %d unique symbols with result filings in range.", len(nse_bulk_matches))

    output_rows = []
    added = 0
    already_tracked = 0
    not_found = 0
    no_price_data = 0

    for i, row in enumerate(watchlist):
        name = row["name"]
        nse_symbol = row["nse_symbol"]
        bse_code = row["bse_code"]

        state_key = nse_symbol if nse_symbol else f"BSE_{bse_code}"

        if state_key in state:
            already_tracked += 1
            output_rows.append({
                "Name": name, "NSE Code": nse_symbol, "BSE Code": bse_code,
                "ResultDate": state[state_key]["result_date"][:10],
                "DayHigh": state[state_key]["day_high"], "Status": "already tracked",
            })
            continue

        if nse_symbol:
            result_dt = nse_bulk_matches.get(nse_symbol)
            yahoo_ticker = f"{nse_symbol}.NS"
        elif bse_code:
            result_dt = find_result_date_bse(bse_code, from_date, to_date)
            yahoo_ticker = f"{bse_code}.BO"
        else:
            continue  # shouldn't happen, read_watchlist already filters this

        if result_dt is None:
            not_found += 1
            output_rows.append({"Name": name, "NSE Code": nse_symbol, "BSE Code": bse_code,
                                 "ResultDate": "", "DayHigh": "", "Status": "no result found"})
            continue

        result_date = result_dt.date()
        day_high = get_day_high(yahoo_ticker, result_date)
        if day_high is None:
            no_price_data += 1
            output_rows.append({"Name": name, "NSE Code": nse_symbol, "BSE Code": bse_code,
                                 "ResultDate": result_date.isoformat(), "DayHigh": "",
                                 "Status": "no price data"})
            continue

        state[state_key] = {
            "result_date": datetime.datetime.combine(result_date, datetime.time()).isoformat(),
            "day_high": day_high,
            "price_alerted": False,
            "rsi_alerted": False,
            "yahoo_ticker": yahoo_ticker,
        }
        added += 1
        output_rows.append({"Name": name, "NSE Code": nse_symbol, "BSE Code": bse_code,
                             "ResultDate": result_date.isoformat(), "DayHigh": day_high,
                             "Status": "added"})
        log.info("[%d/%d] %s (%s): result %s, day high %.2f",
                  i + 1, len(watchlist), name, state_key, result_date, day_high)

        time.sleep(0.3)  # be gentle on Yahoo Finance / BSE's per-scrip endpoint

    STATE_FILE.write_text(json.dumps(state, indent=2))

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "NSE Code", "BSE Code", "ResultDate", "DayHigh", "Status"])
        writer.writeheader()
        writer.writerows(output_rows)

    log.info(
        "Done. Added %d new baselines. %d already tracked, %d no result found, %d no price data.",
        added, already_tracked, not_found, no_price_data,
    )
    log.info("Full details written to %s", OUTPUT_CSV)

    if added:
        send_telegram_message(
            f"\U0001F4CB <b>Watchlist backfill complete</b>: {added} companies added to tracking "
            f"from your CSV.\n{already_tracked} already tracked, {not_found} had no result found in "
            f"the last {days_back} days, {no_price_data} had no price data."
        )


if __name__ == "__main__":
    main()
