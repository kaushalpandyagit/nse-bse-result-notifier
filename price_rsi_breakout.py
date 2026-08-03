"""
Nifty 500 Result-Day-High / RSI Breakout Notifier -> Telegram
================================================================

What this does
---------------
For every Nifty 500 stock:
  1. Watches NSE + BSE announcements for financial-result filings
     (same detection logic as result_notifier.py).
  2. The FIRST TIME a result is detected for a stock, it records a
     "baseline" from that trading day: the day's High, Low, and the
     14-period RSI AS OF that day (falls forward to the next trading
     day if the result was announced on a weekend/holiday).
  3. On every subsequent poll (same 15-min cadence), for every stock
     with an active baseline, it checks FOUR independent conditions:
       - Has price broken ABOVE the result-day High?
       - Has price broken BELOW the result-day Low?
       - Has RSI crossed ABOVE the result-day RSI?
       - Has RSI crossed BELOW the result-day RSI?
     Each fires independently, exactly once per stock (tracked in
     state so you don't get spammed every 15 min).
  4. Baselines expire automatically after TRACK_WINDOW_DAYS (default
     15 calendar days) to keep the state file small and relevant --
     the result-day levels lose their meaning as a signal long after
     the result.

Data sources
------------
- Nifty 500 constituent list: fetched live from NSE's official
  archives CSV, cached locally and refreshed weekly. Falls back to a
  small bundled list of large, liquid names if NSE's endpoint is
  unreachable (rare, but NSE does occasionally block/rate-limit).
- Price + RSI: Yahoo Finance via the yfinance library (NSE symbols
  use a ".NS" suffix, e.g. "RELIANCE.NS"). This is free and doesn't
  require an API key or NSE session cookies.
- Result filings: NSE + BSE public announcement APIs (same as
  result_notifier.py).

One-time setup
---------------
1. pip install -r requirements.txt
2. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (same bot as your
   other notifier -- env vars, or edit the CONFIG section directly).
3. python3 price_rsi_breakout.py          (runs the loop forever)
   OR
   python3 price_rsi_breakout.py --once   (single poll, for GitHub
   Actions / cron -- see the .github/workflows file)

Config knobs are in the CONFIG section below.
"""

import os
import re
import io
import sys
import json
import time
import logging
import datetime
from pathlib import Path

import requests
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("yfinance is required: pip install yfinance")
    raise

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

POLL_INTERVAL_MINUTES = 15
POLL_ONLY_MARKET_HOURS = True

# RSI comparisons are against each stock's OWN result-day RSI value
# (dynamic per stock), not a fixed threshold -- see get_baseline_metrics().
RSI_PERIOD = 14

# How many calendar days after a result a baseline stays active before
# it's dropped from tracking.
TRACK_WINDOW_DAYS = 15

# Keywords that identify a "financial result" announcement (same idea
# as result_notifier.py).
RESULT_KEYWORDS = [
    "financial result", "financial results", "quarterly result",
    "quarterly results", "board meeting outcome", "un-audited",
    "unaudited", "audited financial", "results for the quarter",
    "results for the year",
]

STATE_FILE = Path(__file__).parent / "breakout_state.json"
NIFTY500_CACHE_FILE = Path(__file__).parent / "nifty500_symbols.json"
LOG_FILE = Path(__file__).parent / "breakout_notifier.log"

NSE_500_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

# Small emergency fallback if NSE's list endpoint is unreachable --
# not the full 500, just enough large/liquid names to keep the script
# functional until NSE is reachable again.
FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN",
    "ULTRACEMCO", "WIPRO", "NESTLEIND", "TATASTEEL", "TATAMOTORS",
    "ADANIENT", "ADANIPORTS", "POWERGRID", "NTPC", "ONGC", "HCLTECH",
    "M&M", "JSWSTEEL",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("breakout_notifier")

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram bot token / chat id not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
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
# NIFTY 500 SYMBOL LIST (cached weekly)
# ----------------------------------------------------------------------

def get_nifty500_symbols() -> list:
    if NIFTY500_CACHE_FILE.exists():
        try:
            cached = json.loads(NIFTY500_CACHE_FILE.read_text())
            fetched_at = datetime.datetime.fromisoformat(cached["fetched_at"])
            if (datetime.datetime.now() - fetched_at).days < 7:
                return cached["symbols"]
        except Exception:
            pass  # fall through to refetch

    try:
        resp = requests.get(NSE_500_CSV_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        symbols = sorted(df["Symbol"].astype(str).str.strip().tolist())
        NIFTY500_CACHE_FILE.write_text(json.dumps({
            "fetched_at": datetime.datetime.now().isoformat(),
            "symbols": symbols,
        }))
        log.info("Refreshed Nifty 500 list: %d symbols.", len(symbols))
        return symbols
    except Exception as e:
        log.error("Could not fetch Nifty 500 list (%s). Using fallback list of %d symbols.",
                   e, len(FALLBACK_SYMBOLS))
        return FALLBACK_SYMBOLS


# ----------------------------------------------------------------------
# ANNOUNCEMENT FETCH (trimmed version of result_notifier.py's logic)
# ----------------------------------------------------------------------

def normalise_company(name: str) -> str:
    name = name.upper()
    name = re.sub(r"\b(LIMITED|LTD|LTD\.|THE)\b", "", name)
    name = re.sub(r"[^A-Z0-9]", "", name)
    return name.strip()


def is_result_announcement(subject: str) -> bool:
    subj_lower = subject.lower()
    return any(kw in subj_lower for kw in RESULT_KEYWORDS)


def fetch_nse_result_symbols() -> set:
    """Returns a set of NSE symbols that have a fresh result announcement today."""
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=15)
        time.sleep(1)
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("NSE announcement fetch failed: %s", e)
        return set()

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return set()
    if not isinstance(data, list):
        return set()

    hits = set()
    for item in data:
        subject = f"{item.get('desc') or ''} {item.get('attchmntText') or ''}"
        symbol = (item.get("symbol") or "").strip().upper()
        if symbol and is_result_announcement(subject):
            hits.add(symbol)
    return hits


def fetch_bse_result_companies() -> set:
    """Returns a set of BSE company names with a fresh result announcement
    in the last 3 days (used to cross-match against Nifty 500 names)."""
    today = datetime.datetime.now().strftime("%Y%m%d")
    from_date = (datetime.datetime.now() - datetime.timedelta(days=3)).strftime("%Y%m%d")
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P"
        f"&strToDate={today}&strType=C&subcategory=-1"
    )
    try:
        resp = requests.get(url, headers={**HEADERS, "Referer": "https://www.bseindia.com/corporates/ann.html"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("BSE announcement fetch failed: %s", e)
        return set()

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return set()
    if not isinstance(data, dict):
        return set()

    hits = set()
    for item in data.get("Table", []):
        subject = f"{item.get('NEWSSUB') or ''} {item.get('HEADLINE') or ''}"
        company = item.get("SLONGNAME") or ""
        if company and is_result_announcement(subject):
            hits.add(normalise_company(company))
    return hits


# ----------------------------------------------------------------------
# STATE
# ----------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("Could not parse state file, starting fresh.")
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def prune_expired(state: dict) -> dict:
    cutoff = datetime.datetime.now() - datetime.timedelta(days=TRACK_WINDOW_DAYS)
    kept = {}
    for symbol, entry in state.items():
        try:
            result_date = datetime.datetime.fromisoformat(entry["result_date"])
            if result_date >= cutoff:
                kept[symbol] = entry
        except Exception:
            continue
    return kept


# ----------------------------------------------------------------------
# PRICE / RSI
# ----------------------------------------------------------------------

def compute_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def get_baseline_metrics(yahoo_ticker: str, date: datetime.date):
    """Returns dict with day_high, day_low, baseline_rsi (RSI computed
    using data up to and including the result day), and actual_date
    (falls forward to the next trading day if `date` was a weekend/
    holiday, e.g. a Saturday board meeting). Returns None on failure."""
    try:
        start = date - datetime.timedelta(days=100)
        end = date + datetime.timedelta(days=7)
        hist = yf.Ticker(yahoo_ticker).history(start=start, end=end)
        if hist.empty:
            return None

        hist_dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        idx = next((i for i, d in enumerate(hist_dates) if d >= date), None)
        if idx is None:
            return None

        day_high = float(hist["High"].iloc[idx])
        day_low = float(hist["Low"].iloc[idx])
        closes_up_to = hist["Close"].iloc[: idx + 1]
        baseline_rsi = compute_rsi(closes_up_to) if len(closes_up_to) >= RSI_PERIOD + 1 else None

        return {
            "day_high": day_high,
            "day_low": day_low,
            "baseline_rsi": baseline_rsi,
            "actual_date": hist_dates[idx],
        }
    except Exception as e:
        log.warning("Could not fetch baseline metrics for %s on/after %s: %s", yahoo_ticker, date, e)
        return None


def get_live_price_and_rsi(yahoo_ticker: str) -> tuple:
    """Returns (latest_price, rsi) or (None, None) on failure."""
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="2mo", interval="1d")
        if hist.empty or len(hist) < RSI_PERIOD + 1:
            return None, None
        latest_price = float(hist["Close"].iloc[-1])
        rsi = compute_rsi(hist["Close"])
        return latest_price, rsi
    except Exception as e:
        log.warning("Could not fetch price/RSI for %s: %s", yahoo_ticker, e)
        return None, None


# ----------------------------------------------------------------------
# MARKET HOURS
# ----------------------------------------------------------------------

def is_market_hours_now() -> bool:
    if not POLL_ONLY_MARKET_HOURS:
        return True
    now = datetime.datetime.now(IST) if IST else datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end


# ----------------------------------------------------------------------
# MAIN POLL CYCLE
# ----------------------------------------------------------------------

def poll_once(state: dict) -> dict:
    nifty500 = set(get_nifty500_symbols())
    nifty500_normalised = {normalise_company(s): s for s in nifty500}

    # --- Step 1: detect NEW result filings for Nifty 500 stocks ---
    nse_hits = fetch_nse_result_symbols() & nifty500
    bse_hits_normalised = fetch_bse_result_companies()
    bse_hits = {nifty500_normalised[n] for n in bse_hits_normalised if n in nifty500_normalised}
    new_result_symbols = (nse_hits | bse_hits) - set(state.keys())

    today = datetime.date.today()
    for symbol in new_result_symbols:
        yahoo_ticker = f"{symbol}.NS"
        metrics = get_baseline_metrics(yahoo_ticker, today)
        if metrics is None:
            continue
        state[symbol] = {
            "result_date": datetime.datetime.combine(metrics["actual_date"], datetime.time()).isoformat(),
            "day_high": metrics["day_high"],
            "day_low": metrics["day_low"],
            "baseline_rsi": metrics["baseline_rsi"],
            "yahoo_ticker": yahoo_ticker,
            "price_high_alerted": False,
            "price_low_alerted": False,
            "rsi_up_alerted": False,
            "rsi_down_alerted": False,
        }
        log.info("New baseline set: %s high=%.2f low=%.2f rsi=%s",
                  symbol, metrics["day_high"], metrics["day_low"],
                  f"{metrics['baseline_rsi']:.1f}" if metrics["baseline_rsi"] else "n/a")
        rsi_line = (f"Result-day RSI({RSI_PERIOD}): {metrics['baseline_rsi']:.1f}\n"
                    if metrics["baseline_rsi"] else "")
        send_telegram_message(
            f"\U0001F4CC <b>{symbol}</b> result filed today.\n"
            f"Result-day High: \u20b9{metrics['day_high']:.2f} | Low: \u20b9{metrics['day_low']:.2f}\n"
            f"{rsi_line}"
            f"Will alert if price breaks this High/Low or RSI crosses the result-day RSI level."
        )

    # --- Step 2: check active baselines for price/RSI breakout ---
    state = prune_expired(state)
    for symbol, entry in state.items():
        # Auto-migrate older entries (pre-High/Low/RSI-baseline schema)
        if "day_low" not in entry or "baseline_rsi" not in entry:
            yahoo_ticker = entry.get("yahoo_ticker", f"{symbol}.NS")
            try:
                result_date = datetime.datetime.fromisoformat(entry["result_date"]).date()
            except Exception:
                result_date = today
            metrics = get_baseline_metrics(yahoo_ticker, result_date)
            if metrics is not None:
                entry["day_high"] = metrics["day_high"]
                entry["day_low"] = metrics["day_low"]
                entry["baseline_rsi"] = metrics["baseline_rsi"]
                entry["price_high_alerted"] = entry.get("price_alerted", False)
                entry["price_low_alerted"] = False
                entry["rsi_up_alerted"] = entry.get("rsi_alerted", False)
                entry["rsi_down_alerted"] = False
                log.info("Migrated old baseline for %s to new High/Low/RSI schema.", symbol)
            else:
                continue  # can't migrate yet, skip this cycle

        all_alerted = (entry.get("price_high_alerted") and entry.get("price_low_alerted")
                       and entry.get("rsi_up_alerted") and entry.get("rsi_down_alerted"))
        if all_alerted:
            continue

        yahoo_ticker = entry.get("yahoo_ticker", f"{symbol}.NS")
        price, rsi = get_live_price_and_rsi(yahoo_ticker)
        if price is None:
            continue

        if not entry["price_high_alerted"] and price > entry["day_high"]:
            entry["price_high_alerted"] = True
            send_telegram_message(
                f"\U0001F680 <b>{symbol}</b> price broke ABOVE result-day High!\n"
                f"Current: \u20b9{price:.2f} | Result-day High: \u20b9{entry['day_high']:.2f}"
            )
            log.info("PRICE HIGH breakout: %s @ %.2f (baseline high %.2f)", symbol, price, entry["day_high"])

        if not entry["price_low_alerted"] and price < entry["day_low"]:
            entry["price_low_alerted"] = True
            send_telegram_message(
                f"\U0001F53B <b>{symbol}</b> price broke BELOW result-day Low!\n"
                f"Current: \u20b9{price:.2f} | Result-day Low: \u20b9{entry['day_low']:.2f}"
            )
            log.info("PRICE LOW breakdown: %s @ %.2f (baseline low %.2f)", symbol, price, entry["day_low"])

        baseline_rsi = entry.get("baseline_rsi")
        if rsi is not None and baseline_rsi is not None:
            if not entry["rsi_up_alerted"] and rsi > baseline_rsi:
                entry["rsi_up_alerted"] = True
                send_telegram_message(
                    f"\U0001F4C8 <b>{symbol}</b> RSI crossed ABOVE result-day RSI!\n"
                    f"Current RSI: {rsi:.1f} | Result-day RSI: {baseline_rsi:.1f} | Price: \u20b9{price:.2f}"
                )
                log.info("RSI UP break: %s RSI=%.1f (baseline %.1f)", symbol, rsi, baseline_rsi)

            if not entry["rsi_down_alerted"] and rsi < baseline_rsi:
                entry["rsi_down_alerted"] = True
                send_telegram_message(
                    f"\U0001F4C9 <b>{symbol}</b> RSI crossed BELOW result-day RSI!\n"
                    f"Current RSI: {rsi:.1f} | Result-day RSI: {baseline_rsi:.1f} | Price: \u20b9{price:.2f}"
                )
                log.info("RSI DOWN break: %s RSI=%.1f (baseline %.1f)", symbol, rsi, baseline_rsi)

    return state


def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Nifty500 result-day breakout notifier.%s",
              " (single-shot mode)" if one_shot else "")
    state = load_state()

    if one_shot:
        if is_market_hours_now():
            try:
                state = poll_once(state)
                save_state(state)
            except Exception as e:
                log.exception("Unexpected error during poll: %s", e)
        else:
            log.info("Outside market hours -- skipping.")
        return

    while True:
        if is_market_hours_now():
            try:
                state = poll_once(state)
                save_state(state)
            except Exception as e:
                log.exception("Unexpected error during poll: %s", e)
        else:
            log.info("Outside market hours -- skipping this cycle.")
        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
