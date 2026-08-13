"""
Nifty 500 Result-Day-High / RSI Breakout Notifier -> Telegram + Email
========================================================================

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

ADDITIONALLY -- momentum/trend screening (new)
------------------------------------------------
Once per calendar day (self-throttled: skipped on every poll except
the first one after the date rolls over -- no separate workflow
trigger needed), scans the full Nifty 500 universe for:

  - MINERVINI STAGE 2 + VCP: price above rising 50/150/200-day
    moving averages (stacked in that order), near its 52-week high,
    well above its 52-week low, with a simplified volatility-
    contraction check (each of the last 3 sub-windows of the recent
    base has a smaller price range than the one before it).
  - MOMENTUM LEADERS (thechartist26-style approximation): top-decile
    relative strength vs the universe + near 52-week high. This is a
    general India-momentum approximation, NOT that account's actual
    (undocumented) methodology.

  Relative strength is computed against an EQUAL-WEIGHT MEDIAN RETURN
  across the fetched Nifty 500 universe, used as a benchmark proxy --
  this avoids depending on an unverified Yahoo Finance ticker for the
  Nifty 500 index itself.

  Results are sent as one daily digest (Telegram + email).

Then, on EVERY poll (15-min cadence) for stocks already on that day's
watchlist, checks two intraday triggers (alerts once per stock per
day):

  - DAN ZANGER breakout: live price breaks above the recent ~30-day
    base high on volume >= ZANGER_VOLUME_MULT x the 50-day average.
  - PRADEEP BONDE 4% MODEL: live single-day move >= BONDE_MIN_MOVE_PCT
    on volume >= BONDE_VOLUME_MULT x the 50-day average, restricted to
    stocks already in a Stage 2 uptrend.

Data sources
------------
- Nifty 500 constituent list: fetched live from NSE's official
  archives CSV, cached locally and refreshed weekly. Falls back to a
  small bundled list of large, liquid names if NSE's endpoint is
  unreachable (rare, but NSE does occasionally block/rate-limit).
- Price + RSI + daily history: Yahoo Finance via the yfinance library
  (NSE symbols use a ".NS" suffix, e.g. "RELIANCE.NS"). Free, no API
  key or NSE session cookies required.
- Result filings: NSE + BSE public announcement APIs (same as
  result_notifier.py).

One-time setup
---------------
1. pip install -r requirements.txt
2. Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMAIL_ADDRESS,
   GMAIL_APP_PASSWORD, NOTIFY_EMAIL_TO (env vars, or edit CONFIG
   directly). email_notifier.py must exist alongside this file.
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

from email_notifier import send_email

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

# Minimum minutes between repeat alerts of the SAME type for the SAME
# stock (e.g. price re-crossing above/below the same level repeatedly
# through the day). Prevents spam if a price/RSI level is hovering
# right at the threshold and flapping across it poll to poll.
ALERT_COOLDOWN_MINUTES = 30

# Keywords that identify a "financial result" announcement (same idea
# as result_notifier.py).
RESULT_KEYWORDS = [
    "financial result", "financial results", "quarterly result",
    "quarterly results", "board meeting outcome", "un-audited",
    "unaudited", "audited financial", "results for the quarter",
    "results for the year", "regulation 33", "reg. 33", "reg 33",
    "standalone and consolidated financial", "submitted to the exchange",
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

# --- Momentum/trend screening config (new) ---

MOMENTUM_STATE_FILE = Path(__file__).parent / "momentum_state.json"

# How many trading days of history to pull per stock for the daily scan.
# ~15 months, comfortably covers the 200-day MA plus a 52-week high/low window.
HISTORY_PERIOD = "15mo"

# Minervini trend-template thresholds.
NEAR_52W_HIGH_PCT = 25.0    # price must be within this % of the 52-week high
ABOVE_52W_LOW_PCT = 30.0    # price must be at least this % above the 52-week low
SMA200_TREND_LOOKBACK_DAYS = 22  # ~1 month; 200-SMA must be higher now than this many days ago

# VCP (volatility contraction) -- simplified: split the recent window
# into 3 equal sub-windows and require each successive range to be
# tighter than the one before it.
VCP_LOOKBACK_DAYS = 60

# Relative strength lookback for the daily scan (~6 months of trading days).
RS_LOOKBACK_DAYS = 126

# thechartist26-style momentum-leader approximation: top RS percentile + near high.
MOMENTUM_LEADER_RS_RANK_MIN = 90.0
MOMENTUM_LEADER_NEAR_HIGH_PCT = 15.0

# Dan Zanger: base lookback for the breakout level, and volume multiple required.
ZANGER_BASE_LOOKBACK_DAYS = 30
ZANGER_VOLUME_MULT = 2.0

# Pradeep Bonde 4% model: minimum single-day move % and volume multiple,
# restricted to stocks already in a Stage 2 uptrend.
BONDE_MIN_MOVE_PCT = 4.0
BONDE_VOLUME_MULT = 1.5

# How many names to list per category in the daily digest.
TOP_N_MOMENTUM = 15

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
# TELEGRAM + EMAIL
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


def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


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
    if any(kw in subj_lower for kw in RESULT_KEYWORDS):
        return True
    return "board meeting" in subj_lower and "result" in subj_lower


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
# STATE (result-day baseline tracking)
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


def cooldown_elapsed(entry: dict, last_alert_key: str) -> bool:
    """Returns True if enough time has passed since the last alert of
    this type for this stock (or if it has never fired), i.e. it's OK
    to alert again now."""
    last = entry.get(last_alert_key)
    if not last:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last)
    except Exception:
        return True
    return (datetime.datetime.now() - last_dt) >= datetime.timedelta(minutes=ALERT_COOLDOWN_MINUTES)


# ----------------------------------------------------------------------
# MOMENTUM STATE (new -- separate file, one daily watchlist)
# ----------------------------------------------------------------------

def load_momentum_state() -> dict:
    if MOMENTUM_STATE_FILE.exists():
        try:
            return json.loads(MOMENTUM_STATE_FILE.read_text())
        except Exception:
            log.warning("Could not parse momentum state file, starting fresh.")
    return {"last_scan_date": None, "watchlist": {}}


def save_momentum_state(momentum_state: dict):
    MOMENTUM_STATE_FILE.write_text(json.dumps(momentum_state, indent=2))


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


def get_live_price_volume_rsi(yahoo_ticker: str) -> tuple:
    """Returns (latest_price, latest_volume, rsi) or (None, None, None) on failure."""
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="2mo", interval="1d")
        if hist.empty or len(hist) < RSI_PERIOD + 1:
            return None, None, None
        latest_price = float(hist["Close"].iloc[-1])
        latest_volume = float(hist["Volume"].iloc[-1])
        rsi = compute_rsi(hist["Close"])
        return latest_price, latest_volume, rsi
    except Exception as e:
        log.warning("Could not fetch price/volume/RSI for %s: %s", yahoo_ticker, e)
        return None, None, None


def get_live_price_and_rsi(yahoo_ticker: str) -> tuple:
    """Back-compat wrapper: returns (latest_price, rsi) or (None, None)."""
    price, _volume, rsi = get_live_price_volume_rsi(yahoo_ticker)
    return price, rsi


# ----------------------------------------------------------------------
# MOMENTUM SCREENING (new): Minervini / VCP / RS / Zanger / Bonde
# ----------------------------------------------------------------------

def fetch_batch_history(tickers: list) -> dict:
    """Bulk-downloads daily OHLCV for many tickers in one call (much
    faster and lighter than looping yf.Ticker(...).history() 500
    times). Returns dict of ticker -> DataFrame with Open/High/Low/
    Close/Volume columns. Skips tickers that failed or came back empty."""
    result = {}
    try:
        data = yf.download(
            tickers=" ".join(tickers), period=HISTORY_PERIOD, interval="1d",
            group_by="ticker", threads=True, progress=False, auto_adjust=False,
        )
    except Exception as e:
        log.error("Batch history download failed: %s", e)
        return result

    if len(tickers) == 1:
        # yf.download returns a flat (non-multi-index) frame for a single ticker
        t = tickers[0]
        if not data.empty:
            result[t] = data.dropna(how="all")
        return result

    for t in tickers:
        try:
            df = data[t].dropna(how="all")
            if not df.empty:
                result[t] = df
        except (KeyError, Exception):
            continue
    return result


def get_trend_template_status(closes: pd.Series) -> dict:
    """Simplified Minervini trend template. Returns None if there isn't
    enough history yet (need 200+ trading days)."""
    if len(closes) < 210:
        return None
    sma50 = closes.rolling(50).mean()
    sma150 = closes.rolling(150).mean()
    sma200 = closes.rolling(200).mean()
    price = float(closes.iloc[-1])
    s50, s150, s200 = float(sma50.iloc[-1]), float(sma150.iloc[-1]), float(sma200.iloc[-1])

    sma200_prior = sma200.iloc[-1 - SMA200_TREND_LOOKBACK_DAYS] if len(sma200) > SMA200_TREND_LOOKBACK_DAYS else None
    sma200_rising = bool(sma200_prior is not None and s200 > float(sma200_prior))

    window = closes[-252:] if len(closes) >= 252 else closes
    fifty2w_high = float(window.max())
    fifty2w_low = float(window.min())

    near_high = price >= (1 - NEAR_52W_HIGH_PCT / 100) * fifty2w_high
    above_low = price >= (1 + ABOVE_52W_LOW_PCT / 100) * fifty2w_low

    stage2 = bool(price > s50 > s150 > s200 and sma200_rising and near_high and above_low)

    return {
        "sma50": s50, "sma150": s150, "sma200": s200,
        "sma200_rising": sma200_rising, "stage2": stage2,
        "fifty2w_high": fifty2w_high, "fifty2w_low": fifty2w_low,
        "near_high": near_high,
    }


def detect_vcp(highs: pd.Series, lows: pd.Series, lookback: int = VCP_LOOKBACK_DAYS) -> bool:
    """Simplified volatility-contraction check: splits the recent
    `lookback`-day window into 3 equal sub-windows and requires each
    successive sub-window's price range (as % of its low) to be
    tighter than the one before it. Not a full pattern-recognition
    VCP detector -- a practical approximation."""
    if len(highs) < lookback or len(lows) < lookback:
        return False
    h = highs[-lookback:]
    l = lows[-lookback:]
    third = lookback // 3
    ranges = []
    for i in range(3):
        seg_h = h[i * third:(i + 1) * third]
        seg_l = l[i * third:(i + 1) * third]
        if seg_l.empty or float(seg_l.min()) <= 0:
            return False
        rng_pct = (float(seg_h.max()) - float(seg_l.min())) / float(seg_l.min()) * 100
        ranges.append(rng_pct)
    return ranges[0] > ranges[1] > ranges[2]


def compute_rs_return(closes: pd.Series, lookback: int = RS_LOOKBACK_DAYS):
    if len(closes) <= lookback:
        return None
    start_price = float(closes.iloc[-lookback])
    end_price = float(closes.iloc[-1])
    if start_price <= 0:
        return None
    return (end_price / start_price - 1) * 100


def run_daily_momentum_scan(momentum_state: dict) -> dict:
    """Runs once per calendar day (self-throttled via last_scan_date).
    Screens the full Nifty 500 universe for Minervini Stage 2 + VCP,
    and a momentum-leader (thechartist26-style) approximation. Sends
    one Telegram + email digest. Also stores per-symbol data (trend
    levels, base high/low, avg volume) used by the intraday triggers."""
    today_str = datetime.date.today().isoformat()
    if momentum_state.get("last_scan_date") == today_str:
        return momentum_state  # already scanned today, nothing to do

    symbols = get_nifty500_symbols()
    tickers = [f"{s}.NS" for s in symbols]
    log.info("Running daily momentum universe scan for %d symbols (this is slow, runs once/day)...", len(tickers))

    hist_map = fetch_batch_history(tickers)
    log.info("Batch history fetched for %d/%d tickers.", len(hist_map), len(tickers))

    per_symbol_data = {}
    returns_6m = {}

    for symbol in symbols:
        df = hist_map.get(f"{symbol}.NS")
        if df is None or df.empty or "Close" not in df.columns:
            continue
        closes = df["Close"].dropna()
        highs = df["High"].dropna()
        lows = df["Low"].dropna()
        volumes = df["Volume"].dropna()
        if len(closes) < 210 or len(volumes) < 50:
            continue

        trend = get_trend_template_status(closes)
        if trend is None:
            continue
        vcp_contracting = detect_vcp(highs, lows)
        rs_return = compute_rs_return(closes)
        avg_vol50 = float(volumes[-50:].mean())
        base_high = float(highs.iloc[-ZANGER_BASE_LOOKBACK_DAYS - 1:-1].max()) if len(highs) > ZANGER_BASE_LOOKBACK_DAYS else float(highs.max())
        base_low = float(lows.iloc[-ZANGER_BASE_LOOKBACK_DAYS - 1:-1].min()) if len(lows) > ZANGER_BASE_LOOKBACK_DAYS else float(lows.min())
        prev_close = float(closes.iloc[-1])  # most recent completed session's close, as of scan time

        per_symbol_data[symbol] = {
            **trend,
            "vcp_contracting": vcp_contracting,
            "rs_return_6m": rs_return,
            "avg_volume_50d": avg_vol50,
            "base_high": base_high,
            "base_low": base_low,
            "prev_close": prev_close,
        }
        if rs_return is not None:
            returns_6m[symbol] = rs_return

    # Benchmark proxy = equal-weight median 6-month return across the
    # scanned universe (avoids depending on an unverified Nifty500 index ticker).
    median_return = float(pd.Series(list(returns_6m.values())).median()) if returns_6m else 0.0

    sorted_syms = sorted(returns_6m.keys(), key=lambda s: returns_6m[s], reverse=True)
    total = len(sorted_syms) or 1
    rs_rank_pct = {s: round(100 * (1 - i / total), 1) for i, s in enumerate(sorted_syms)}

    watchlist = {}
    stage2_vcp_list = []
    momentum_leader_list = []

    for symbol, d in per_symbol_data.items():
        rs_rank = rs_rank_pct.get(symbol)
        entry = {**d, "rs_rank": rs_rank, "zanger_alerted": False, "bonde_alerted": False}
        watchlist[symbol] = entry

        if d["stage2"] and d["vcp_contracting"]:
            stage2_vcp_list.append((symbol, rs_rank))

        near_high_leader = d["near_high"]  # already 25%-of-high by default trend check
        if rs_rank is not None and rs_rank >= MOMENTUM_LEADER_RS_RANK_MIN and near_high_leader:
            momentum_leader_list.append((symbol, rs_rank))

    stage2_vcp_list.sort(key=lambda x: (x[1] or 0), reverse=True)
    momentum_leader_list.sort(key=lambda x: (x[1] or 0), reverse=True)

    momentum_state = {"last_scan_date": today_str, "watchlist": watchlist}

    lines = [
        f"\U0001F4CA <b>Daily Momentum Scan \u2014 {today_str}</b>",
        f"<i>Universe: {len(per_symbol_data)}/{len(symbols)} Nifty500 stocks with sufficient history. "
        f"Benchmark = equal-weight median 6-month return across universe ({median_return:+.1f}%), "
        f"used as an index-ticker-free relative-strength proxy.</i>",
        "",
        f"\U0001F7E2 <b>Minervini Stage 2 + VCP</b> ({len(stage2_vcp_list)} stocks)",
        "<i>Price above rising 50/150/200-day MAs (stacked), near 52-week high, "
        "volatility contracting over the last ~3 months (simplified check)</i>",
    ]
    if stage2_vcp_list:
        for s, r in stage2_vcp_list[:TOP_N_MOMENTUM]:
            lines.append(f"  {s} (RS rank {r})")
    else:
        lines.append("  (none)")

    lines += [
        "",
        f"\U0001F31F <b>Momentum Leaders</b> \u2014 thechartist26-style approximation, NOT their actual "
        f"methodology ({len(momentum_leader_list)} stocks)",
        f"<i>Top-decile relative strength (RS rank \u2265{MOMENTUM_LEADER_RS_RANK_MIN:.0f}) + near 52-week high</i>",
    ]
    if momentum_leader_list:
        for s, r in momentum_leader_list[:TOP_N_MOMENTUM]:
            lines.append(f"  {s} (RS rank {r})")
    else:
        lines.append("  (none)")

    lines += [
        "",
        "<i>Dan Zanger volume-breakout and Pradeep Bonde 4% triggers will alert intraday, "
        "as they fire, for stocks on this watchlist.</i>",
    ]

    digest = "\n".join(lines)
    send_telegram_message(digest)
    send_email(subject=f"Daily Momentum Scan \u2014 {today_str}", body=strip_html_tags(digest))

    log.info("Daily momentum scan done: %d in universe, %d Stage2+VCP, %d momentum-leader.",
              len(per_symbol_data), len(stage2_vcp_list), len(momentum_leader_list))
    return momentum_state


def check_intraday_momentum_triggers(momentum_state: dict) -> dict:
    """Every poll: for stocks on today's watchlist not yet alerted,
    checks Zanger volume-breakout and Bonde 4% triggers against live
    price/volume. Alerts once per stock per trigger per day."""
    watchlist = momentum_state.get("watchlist", {})
    if not watchlist:
        return momentum_state

    for symbol, entry in watchlist.items():
        if entry.get("zanger_alerted") and entry.get("bonde_alerted"):
            continue

        yahoo_ticker = f"{symbol}.NS"
        price, volume, _rsi = get_live_price_volume_rsi(yahoo_ticker)
        if price is None or volume is None:
            continue

        avg_vol50 = entry.get("avg_volume_50d") or 0

        # --- Dan Zanger: breakout above the recent base high on volume ---
        base_high = entry.get("base_high")
        if (not entry.get("zanger_alerted") and base_high and avg_vol50
                and price > base_high and volume >= ZANGER_VOLUME_MULT * avg_vol50):
            entry["zanger_alerted"] = True
            send_telegram_message(
                f"\U0001F4A5 <b>{symbol}</b> Zanger-style volume breakout!\n"
                f"Price \u20b9{price:.2f} broke above the {ZANGER_BASE_LOOKBACK_DAYS}-day base high "
                f"\u20b9{base_high:.2f} on {volume / avg_vol50:.1f}x the 50-day avg volume."
            )
            log.info("ZANGER breakout: %s @ %.2f (base high %.2f, vol %.1fx avg)",
                      symbol, price, base_high, volume / avg_vol50)

        # --- Pradeep Bonde 4% model: sharp move on volume, in a Stage 2 uptrend ---
        prev_close = entry.get("prev_close")
        if prev_close and prev_close > 0:
            pct_change = (price - prev_close) / prev_close * 100
        else:
            pct_change = None

        if (not entry.get("bonde_alerted") and pct_change is not None and avg_vol50
                and entry.get("stage2") and pct_change >= BONDE_MIN_MOVE_PCT
                and volume >= BONDE_VOLUME_MULT * avg_vol50):
            entry["bonde_alerted"] = True
            send_telegram_message(
                f"\u26A1 <b>{symbol}</b> Bonde 4% model trigger!\n"
                f"Move {pct_change:+.1f}% today on {volume / avg_vol50:.1f}x avg volume, "
                f"in a confirmed Stage 2 uptrend. Price \u20b9{price:.2f}"
            )
            log.info("BONDE 4%% trigger: %s move=%.1f%% vol=%.1fx avg", symbol, pct_change, volume / avg_vol50)

    return momentum_state


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
# MAIN POLL CYCLE (result-day baseline tracking -- unchanged logic)
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

        if not entry["price_high_alerted"] and price > entry["day_high"] and cooldown_elapsed(entry, "price_high_last_alert"):
            entry["price_high_alerted"] = True
            entry["price_high_last_alert"] = datetime.datetime.now().isoformat()
            send_telegram_message(
                f"\U0001F680 <b>{symbol}</b> price broke ABOVE result-day High!\n"
                f"Current: \u20b9{price:.2f} | Result-day High: \u20b9{entry['day_high']:.2f}"
            )
            log.info("PRICE HIGH breakout: %s @ %.2f (baseline high %.2f)", symbol, price, entry["day_high"])

        if not entry["price_low_alerted"] and price < entry["day_low"] and cooldown_elapsed(entry, "price_low_last_alert"):
            entry["price_low_alerted"] = True
            entry["price_low_last_alert"] = datetime.datetime.now().isoformat()
            send_telegram_message(
                f"\U0001F53B <b>{symbol}</b> price broke BELOW result-day Low!\n"
                f"Current: \u20b9{price:.2f} | Result-day Low: \u20b9{entry['day_low']:.2f}"
            )
            log.info("PRICE LOW breakdown: %s @ %.2f (baseline low %.2f)", symbol, price, entry["day_low"])

        baseline_rsi = entry.get("baseline_rsi")
        if rsi is not None and baseline_rsi is not None:
            if not entry["rsi_up_alerted"] and rsi > baseline_rsi and cooldown_elapsed(entry, "rsi_up_last_alert"):
                entry["rsi_up_alerted"] = True
                entry["rsi_up_last_alert"] = datetime.datetime.now().isoformat()
                send_telegram_message(
                    f"\U0001F4C8 <b>{symbol}</b> RSI crossed ABOVE result-day RSI!\n"
                    f"Current RSI: {rsi:.1f} | Result-day RSI: {baseline_rsi:.1f} | Price: \u20b9{price:.2f}"
                )
                log.info("RSI UP break: %s RSI=%.1f (baseline %.1f)", symbol, rsi, baseline_rsi)

            if not entry["rsi_down_alerted"] and rsi < baseline_rsi and cooldown_elapsed(entry, "rsi_down_last_alert"):
                entry["rsi_down_alerted"] = True
                entry["rsi_down_last_alert"] = datetime.datetime.now().isoformat()
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
    momentum_state = load_momentum_state()

    if one_shot:
        if is_market_hours_now():
            try:
                state = poll_once(state)
                save_state(state)
            except Exception as e:
                log.exception("Unexpected error during poll: %s", e)
            try:
                momentum_state = run_daily_momentum_scan(momentum_state)
                momentum_state = check_intraday_momentum_triggers(momentum_state)
                save_momentum_state(momentum_state)
            except Exception as e:
                log.exception("Unexpected error during momentum scan/check: %s", e)
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
            try:
                momentum_state = run_daily_momentum_scan(momentum_state)
                momentum_state = check_intraday_momentum_triggers(momentum_state)
                save_momentum_state(momentum_state)
            except Exception as e:
                log.exception("Unexpected error during momentum scan/check: %s", e)
        else:
            log.info("Outside market hours -- skipping this cycle.")
        time.sleep(POLL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
