"""
Nifty Total Market Breakout Notifier -> Telegram + Email
=========================================================
Includes:
  1. Result-Day-High / Low / RSI Breakout Tracker (TradingView RSI matched)
  2. Minervini Stage 2 + VCP / Momentum Leaders Daily Scan
  3. Intraday Dan Zanger Early Entry Breakout
  4. Intraday Pradeep Bonde 4% Early Entry Trigger
  5. Intraday Horizontal Resistance Proximity Scanner (within 4%)
  6. Intraday Multi-Timeframe RSI & Key EMA Breakout Scanner
  7. Custom Manual RSI Alerts Tracker (Supports BSE: prefix)
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

RSI_PERIOD = 14
TRACK_WINDOW_DAYS = 15
ALERT_COOLDOWN_MINUTES = 30

# Market Cap boundaries in Crores (INR)
MIN_MARKET_CAP_CR = 300.0
MAX_MARKET_CAP_CR = 31000.0

# GLOBAL RESTRICTION: No auto-scanners will alert if Weekly RSI is >= this value
MAX_WEEKLY_RSI = 57.0

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

# Nifty Total Market Index (750 stocks covering Large, Mid, Small & Micro)
NSE_CSV_URL = "https://archives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"

FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN",
    "ULTRACEMCO", "WIPRO", "NESTLEIND", "TATASTEEL", "TATAMOTORS",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# --- Momentum/trend screening config ---

MOMENTUM_STATE_FILE = Path(__file__).parent / "momentum_state.json"
HISTORY_PERIOD = "18mo"

NEAR_52W_HIGH_PCT = 25.0
ABOVE_52W_LOW_PCT = 30.0
SMA200_TREND_LOOKBACK_DAYS = 22
VCP_LOOKBACK_DAYS = 60
RS_LOOKBACK_DAYS = 126

MOMENTUM_LEADER_RS_RANK_MIN = 90.0
MOMENTUM_LEADER_NEAR_HIGH_PCT = 15.0

# Zanger Early Entry Config
ZANGER_BASE_LOOKBACK_DAYS = 30
ZANGER_VOLUME_MULT = 2.0
ZANGER_EARLY_MOVE_PCT = 1.5

# Bonde Early Entry Config
BONDE_MIN_MOVE_PCT = 2.0
BONDE_MIN_VOLUME = 700000

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

def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

# ----------------------------------------------------------------------
# UNIVERSE SYMBOL LIST (cached weekly)
# ----------------------------------------------------------------------

def get_universe_symbols() -> list:
    if NIFTY500_CACHE_FILE.exists():
        try:
            cached = json.loads(NIFTY500_CACHE_FILE.read_text())
            fetched_at = datetime.datetime.fromisoformat(cached["fetched_at"])
            if (datetime.datetime.now() - fetched_at).days < 7:
                return cached["symbols"]
        except Exception:
            pass

    try:
        resp = requests.get(NSE_CSV_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        symbols = sorted(df["Symbol"].astype(str).str.strip().tolist())
        NIFTY500_CACHE_FILE.write_text(json.dumps({
            "fetched_at": datetime.datetime.now().isoformat(),
            "symbols": symbols,
        }))
        log.info("Refreshed Universe list: %d symbols.", len(symbols))
        return symbols
    except Exception as e:
        log.error("Could not fetch Universe list (%s). Using fallback list.", e)
        return FALLBACK_SYMBOLS

def get_nifty500_symbols() -> list:
    return get_universe_symbols()

# ----------------------------------------------------------------------
# ANNOUNCEMENT FETCH
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
# STATE MANAGEMENT
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
        # Keep custom RSI alerts indefinitely
        if symbol.startswith("custom_rsi_"):
            kept[symbol] = entry
            continue
        try:
            result_date = datetime.datetime.fromisoformat(entry["result_date"])
            if result_date >= cutoff:
                kept[symbol] = entry
        except Exception:
            continue
    return kept

def cooldown_elapsed(entry: dict, last_alert_key: str) -> bool:
    last = entry.get(last_alert_key)
    if not last:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last)
    except Exception:
        return True
    return (datetime.datetime.now() - last_dt) >= datetime.timedelta(minutes=ALERT_COOLDOWN_MINUTES)

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
# PRICE / RSI / METRICS
# ----------------------------------------------------------------------

def compute_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # adjust=False matches TradingView's Wilder Smoothing exactly
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def get_baseline_metrics(yahoo_ticker: str, date: datetime.date):
    try:
        # Extended lookback to 365 days for accurate RSI warm-up
        start = date - datetime.timedelta(days=365)
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

def get_live_metrics_enhanced(yahoo_ticker: str) -> dict:
    try:
        # Extended to 1y to match TradingView live RSI smoothing and to compute live weekly RSI
        hist = yf.Ticker(yahoo_ticker).history(period="1y", interval="1d")
        if hist.empty or len(hist) < 5:
            return None
        
        today = hist.iloc[-1]
        yesterday = hist.iloc[-2]
        day3_ago = hist.iloc[-4]
        
        # Calculate live Weekly RSI
        weekly_closes = hist["Close"].resample('W-FRI').last().dropna()
        weekly_rsi = compute_rsi(weekly_closes) if len(weekly_closes) > 14 else 50.0

        return {
            "price": float(today["Close"]),
            "volume": float(today["Volume"]),
            "prev_close": float(yesterday["Close"]),
            "prev_volume": float(yesterday["Volume"]),
            "close_3d_ago": float(day3_ago["Close"]),
            "rsi": compute_rsi(hist["Close"]),
            "weekly_rsi": weekly_rsi,
        }
    except Exception as e:
        log.warning("Could not fetch extended metrics for %s: %s", yahoo_ticker, e)
        return None

# ----------------------------------------------------------------------
# MOMENTUM SCREENING & TECHNICAL CRITERIA
# ----------------------------------------------------------------------

def fetch_batch_history(tickers: list) -> dict:
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

def get_market_cap_cr(symbol: str) -> float:
    """Helper to fetch Market Cap in Crores."""
    try:
        t = yf.Ticker(f"{symbol}.NS")
        mcap = t.fast_info.get("marketCap") or t.fast_info.get("market_cap")
        if mcap:
            return float(mcap) / 1e7
    except Exception:
        pass
    return None

def run_daily_momentum_scan(momentum_state: dict) -> dict:
    today_str = datetime.date.today().isoformat()
    if momentum_state.get("last_scan_date") == today_str:
        return momentum_state

    symbols = get_universe_symbols()
    tickers = [f"{s}.NS" for s in symbols]
    log.info("Running daily momentum universe scan for %d symbols...", len(tickers))

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

        # --- MARKET CAP FILTER (300 Cr to 31,000 Cr) ---
        mcap_cr = get_market_cap_cr(symbol)
        if mcap_cr is not None:
            if mcap_cr < MIN_MARKET_CAP_CR or mcap_cr > MAX_MARKET_CAP_CR:
                continue

        trend = get_trend_template_status(closes)
        if trend is None:
            continue
        vcp_contracting = detect_vcp(highs, lows)
        rs_return = compute_rs_return(closes)
        avg_vol50 = float(volumes[-50:].mean())
        base_high = float(highs.iloc[-ZANGER_BASE_LOOKBACK_DAYS - 1:-1].max()) if len(highs) > ZANGER_BASE_LOOKBACK_DAYS else float(highs.max())
        base_low = float(lows.iloc[-ZANGER_BASE_LOOKBACK_DAYS - 1:-1].min()) if len(lows) > ZANGER_BASE_LOOKBACK_DAYS else float(lows.min())
        prev_close = float(closes.iloc[-1])

        # Multi-Timeframe RSI baseline metrics
        monthly_closes = closes.resample('ME').last().dropna()
        monthly_rsi = compute_rsi(monthly_closes) if len(monthly_closes) > 14 else 50.0
        yesterday_rsi = compute_rsi(closes)

        # Yesterday's EMAs
        ema_10 = float(closes.ewm(span=10, adjust=False).mean().iloc[-1])
        ema_21 = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        ema_50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
        ema_100 = float(closes.ewm(span=100, adjust=False).mean().iloc[-1])
        ema_200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])

        per_symbol_data[symbol] = {
            **trend,
            "vcp_contracting": vcp_contracting,
            "rs_return_6m": rs_return,
            "avg_volume_50d": avg_vol50,
            "base_high": base_high,
            "base_low": base_low,
            "prev_close": prev_close,
            "monthly_rsi": monthly_rsi,
            "yesterday_rsi": yesterday_rsi,
            "ema_10_prev": ema_10,
            "ema_21_prev": ema_21,
            "ema_50_prev": ema_50,
            "ema_100_prev": ema_100,
            "ema_200_prev": ema_200,
            "mcap_cr": mcap_cr,
        }
        if rs_return is not None:
            returns_6m[symbol] = rs_return

    median_return = float(pd.Series(list(returns_6m.values())).median()) if returns_6m else 0.0

    sorted_syms = sorted(returns_6m.keys(), key=lambda s: returns_6m[s], reverse=True)
    total = len(sorted_syms) or 1
    rs_rank_pct = {s: round(100 * (1 - i / total), 1) for i, s in enumerate(sorted_syms)}

    watchlist = {}
    stage2_vcp_list = []
    momentum_leader_list = []

    for symbol, d in per_symbol_data.items():
        rs_rank = rs_rank_pct.get(symbol)
        entry = {
            **d,
            "rs_rank": rs_rank,
            "zanger_alerted": False,
            "bonde_alerted": False,
            "resistance_alerted": False,
            "mtf_alerted": False,
        }
        watchlist[symbol] = entry

        if d["stage2"] and d["vcp_contracting"]:
            stage2_vcp_list.append((symbol, rs_rank))

        near_high_leader = d["near_high"]
        if rs_rank is not None and rs_rank >= MOMENTUM_LEADER_RS_RANK_MIN and near_high_leader:
            momentum_leader_list.append((symbol, rs_rank))

    stage2_vcp_list.sort(key=lambda x: (x[1] or 0), reverse=True)
    momentum_leader_list.sort(key=lambda x: (x[1] or 0), reverse=True)

    momentum_state = {"last_scan_date": today_str, "watchlist": watchlist}

    lines = [
        f"\U0001F4CA <b>Daily Momentum Scan \u2014 {today_str}</b>",
        f"<i>Universe: {len(per_symbol_data)} stocks within ₹{MIN_MARKET_CAP_CR:,.0f} Cr \u2013 ₹{MAX_MARKET_CAP_CR:,.0f} Cr. "
        f"Benchmark Median Return ({median_return:+.1f}%)</i>",
        "",
        f"\U0001F7E2 <b>Minervini Stage 2 + VCP</b> ({len(stage2_vcp_list)} stocks)",
        "<i>Price above stacked rising MAs, near 52W high, VCP contracting</i>",
    ]
    if stage2_vcp_list:
        for s, r in stage2_vcp_list[:TOP_N_MOMENTUM]:
            lines.append(f"  {s} (RS rank {r})")
    else:
        lines.append("  (none)")

    lines += [
        "",
        f"\U0001F31F <b>Momentum Leaders</b> ({len(momentum_leader_list)} stocks)",
        f"<i>Top-decile RS (rank \u2265{MOMENTUM_LEADER_RS_RANK_MIN:.0f}) + near 52W high</i>",
    ]
    if momentum_leader_list:
        for s, r in momentum_leader_list[:TOP_N_MOMENTUM]:
            lines.append(f"  {s} (RS rank {r})")
    else:
        lines.append("  (none)")

    lines += [
        "",
        f"<i>Intraday alerts active. (Filtered out if Weekly RSI \u2265 {MAX_WEEKLY_RSI})</i>",
    ]

    digest = "\n".join(lines)
    send_telegram_message(digest)
    send_email(subject=f"Daily Momentum Scan \u2014 {today_str}", body=strip_html_tags(digest))

    log.info("Daily scan done: %d in watchlist, %d Stage2+VCP, %d momentum-leader.",
             len(per_symbol_data), len(stage2_vcp_list), len(momentum_leader_list))
    return momentum_state

def check_intraday_momentum_triggers(momentum_state: dict) -> dict:
    watchlist = momentum_state.get("watchlist", {})
    if not watchlist:
        return momentum_state

    for symbol, entry in watchlist.items():
        if (entry.get("zanger_alerted") and entry.get("bonde_alerted")
                and entry.get("resistance_alerted") and entry.get("mtf_alerted")):
            continue

        yahoo_ticker = f"{symbol}.NS"
        metrics = get_live_metrics_enhanced(yahoo_ticker)
        if not metrics:
            continue

        live_weekly_rsi = metrics["weekly_rsi"]
        
        # --- GLOBAL RESTRICTION: SKIP IF WEEKLY RSI >= 57 ---
        if live_weekly_rsi >= MAX_WEEKLY_RSI:
            continue

        price = metrics["price"]
        volume = metrics["volume"]
        prev_close = metrics["prev_close"]
        prev_volume = metrics["prev_volume"]
        close_3d_ago = metrics["close_3d_ago"]
        live_rsi = metrics["rsi"]

        avg_vol50 = entry.get("avg_volume_50d") or 0
        base_high = entry.get("base_high")
        pct_change = (price - prev_close) / prev_close * 100 if prev_close else None

        # --- 1. Dan Zanger Early Entry Breakout ---
        if (not entry.get("zanger_alerted") and base_high and avg_vol50 and pct_change is not None):
            if pct_change >= ZANGER_EARLY_MOVE_PCT and volume >= ZANGER_VOLUME_MULT * avg_vol50 and price >= (base_high * 0.95):
                entry["zanger_alerted"] = True
                send_telegram_message(
                    f"\U0001F4A5 <b>{symbol}</b> Zanger Early Entry Setup!\n"
                    f"Price \u20b9{price:.2f} (up {pct_change:+.1f}%) near {ZANGER_BASE_LOOKBACK_DAYS}-day base high "
                    f"\u20b9{base_high:.2f} on {volume / avg_vol50:.1f}x avg volume."
                )
                log.info("ZANGER EARLY breakout: %s @ %.2f", symbol, price)

        # --- 2. Pradeep Bonde 4% Early Entry Mod ---
        if (not entry.get("bonde_alerted") and pct_change is not None and prev_volume is not None and close_3d_ago):
            return_3d = (prev_close - close_3d_ago) / close_3d_ago * 100
            if (entry.get("stage2") and return_3d < 1.0 and volume > prev_volume
                    and volume >= BONDE_MIN_VOLUME and pct_change >= BONDE_MIN_MOVE_PCT):
                entry["bonde_alerted"] = True
                send_telegram_message(
                    f"\u26A1 <b>{symbol}</b> Bonde Early Entry Model Trigger!\n"
                    f"Consolidated ({return_3d:+.1f}%), moved {pct_change:+.1f}% today. "
                    f"Vol ({volume:,.0f}) > Yesterday, in Stage 2. Price \u20b9{price:.2f}"
                )
                log.info("BONDE EARLY trigger: %s move=%.1f%%", symbol, pct_change)

        # --- 3. Horizontal Resistance Proximity Scanner ---
        if not entry.get("resistance_alerted") and base_high:
            if 0 < ((base_high - price) / base_high * 100) <= 4.0:
                entry["resistance_alerted"] = True
                send_telegram_message(
                    f"\U0001F3AF <b>{symbol}</b> Horizontal Resistance Scanner!\n"
                    f"Price \u20b9{price:.2f} is within 4% below the recent base high (\u20b9{base_high:.2f})."
                )
                log.info("RESISTANCE Scanner: %s @ %.2f", symbol, price)

        # --- 4. Multi-Timeframe RSI & Key EMA Scanner ---
        if not entry.get("mtf_alerted") and live_rsi is not None:
            m_rsi = entry.get("monthly_rsi", 50.0)
            y_rsi = entry.get("yesterday_rsi", 50.0)

            live_ema_10 = (price - entry["ema_10_prev"]) * (2/11) + entry["ema_10_prev"]
            live_ema_21 = (price - entry["ema_21_prev"]) * (2/22) + entry["ema_21_prev"]
            live_ema_50 = (price - entry["ema_50_prev"]) * (2/51) + entry["ema_50_prev"]
            live_ema_100 = (price - entry["ema_100_prev"]) * (2/101) + entry["ema_100_prev"]
            live_ema_200 = (price - entry["ema_200_prev"]) * (2/201) + entry["ema_200_prev"]

            ema_condition = (
                price >= live_ema_200 * 1.03 or
                price >= live_ema_100 * 1.03 or
                price >= live_ema_50 * 1.03 or
                price >= live_ema_21 * 1.03 or
                price >= live_ema_10 * 1.03
            )

            rsi_condition = (
                live_rsi > y_rsi and
                live_rsi > 30 and
                m_rsi <= 56 and
                live_weekly_rsi <= live_rsi
            )

            if rsi_condition and ema_condition:
                entry["mtf_alerted"] = True
                send_telegram_message(
                    f"\U0001F52E <b>{symbol}</b> MTF RSI + EMA Trigger!\n"
                    f"Price \u20b9{price:.2f} (Spiked \u22653% above key EMA).\n"
                    f"Live Daily RSI: {live_rsi:.1f} | Weekly: {live_weekly_rsi:.1f} | Monthly: {m_rsi:.1f}"
                )
                log.info("MTF SCANNER: %s @ %.2f", symbol, price)

    return momentum_state

# ----------------------------------------------------------------------
# MARKET HOURS & POLLING
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

def poll_once(state: dict) -> dict:
    universe = set(get_universe_symbols())
    universe_normalised = {normalise_company(s): s for s in universe}

    # Detect fresh announcements
    nse_hits = fetch_nse_result_symbols() & universe
    bse_hits_normalised = fetch_bse_result_companies()
    bse_hits = {universe_normalised[n] for n in bse_hits_normalised if n in universe_normalised}
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
        rsi_line = (f"Result-day RSI({RSI_PERIOD}): {metrics['baseline_rsi']:.1f}\n" if metrics["baseline_rsi"] else "")
        send_telegram_message(
            f"\U0001F4CC <b>{symbol}</b> result filed today.\n"
            f"Result-day High: \u20b9{metrics['day_high']:.2f} | Low: \u20b9{metrics['day_low']:.2f}\n"
            f"{rsi_line}Will alert if price breaks this High/Low or RSI crosses."
        )

    state = prune_expired(state)
    
    # -------------------------------------------------------------
    # 1. Result Day RSI Breakout Checks
    # -------------------------------------------------------------
    for symbol, entry in state.items():
        if symbol.startswith("custom_rsi_"):
            continue # Skip custom alerts in this loop

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
            else:
                continue

        all_alerted = (entry.get("price_high_alerted") and entry.get("price_low_alerted")
                       and entry.get("rsi_up_alerted") and entry.get("rsi_down_alerted"))
        if all_alerted:
            continue

        yahoo_ticker = entry.get("yahoo_ticker", f"{symbol}.NS")
        metrics = get_live_metrics_enhanced(yahoo_ticker)
        if not metrics:
            continue
        
        # --- GLOBAL RESTRICTION: SKIP IF WEEKLY RSI >= 57 ---
        if metrics["weekly_rsi"] >= MAX_WEEKLY_RSI:
            continue

        price = metrics["price"]
        rsi = metrics["rsi"]

        if not entry["price_high_alerted"] and price > entry["day_high"] and cooldown_elapsed(entry, "price_high_last_alert"):
            entry["price_high_alerted"] = True
            entry["price_high_last_alert"] = datetime.datetime.now().isoformat()
            send_telegram_message(
                f"\U0001F680 <b>{symbol}</b> price broke ABOVE result-day High!\n"
                f"Current: \u20b9{price:.2f} | Result-day High: \u20b9{entry['day_high']:.2f}"
            )

        if not entry["price_low_alerted"] and price < entry["day_low"] and cooldown_elapsed(entry, "price_low_last_alert"):
            entry["price_low_alerted"] = True
            entry["price_low_last_alert"] = datetime.datetime.now().isoformat()
            send_telegram_message(
                f"\U0001F53B <b>{symbol}</b> price broke BELOW result-day Low!\n"
                f"Current: \u20b9{price:.2f} | Result-day Low: \u20b9{entry['day_low']:.2f}"
            )

        baseline_rsi = entry.get("baseline_rsi")
        if rsi is not None and baseline_rsi is not None:
            if not entry["rsi_up_alerted"] and rsi > baseline_rsi and cooldown_elapsed(entry, "rsi_up_last_alert"):
                entry["rsi_up_alerted"] = True
                entry["rsi_up_last_alert"] = datetime.datetime.now().isoformat()
                send_telegram_message(
                    f"\U0001F4C8 <b>{symbol}</b> RSI crossed ABOVE result-day RSI!\n"
                    f"Current RSI: {rsi:.1f} | Base RSI: {baseline_rsi:.1f} | Price: \u20b9{price:.2f}"
                )

            if not entry["rsi_down_alerted"] and rsi < baseline_rsi and cooldown_elapsed(entry, "rsi_down_last_alert"):
                entry["rsi_down_alerted"] = True
                entry["rsi_down_last_alert"] = datetime.datetime.now().isoformat()
                send_telegram_message(
                    f"\U0001F4C9 <b>{symbol}</b> RSI crossed BELOW result-day RSI!\n"
                    f"Current RSI: {rsi:.1f} | Base RSI: {baseline_rsi:.1f} | Price: \u20b9{price:.2f}"
                )

    # -------------------------------------------------------------
    # 2. CUSTOM MANUAL RSI ALERTS 
    # -------------------------------------------------------------
    custom_alerts = {
        "HINDWAREAP": {"condition": "above", "target_rsi": 32.63},
        "KOTHARIPET": {"condition": "below", "target_rsi": 36.63},
        "BSE:PGFOILQ": {"condition": "below", "target_rsi": 34.2},
        "TITAN":       {"condition": "below", "target_rsi": 42},
      
    }

    for symbol, rules in custom_alerts.items():
        state_key = f"custom_rsi_{symbol}"
        if state_key not in state:
            state[state_key] = {"alerted": False, "last_alert": None}
        
        c_entry = state[state_key]
        
        # Check if it's time to alert (respects the 30-min cooldown)
        if not c_entry.get("alerted") or cooldown_elapsed(c_entry, "last_alert"):
            
            # SMART ROUTING: Translate "BSE:SYMBOL" to Yahoo's "SYMBOL.BO"
            clean_symbol = symbol.upper().strip()
            if clean_symbol.startswith("BSE:"):
                display_name = clean_symbol.split(":")[1]
                yahoo_ticker = f"{display_name}.BO"
            elif clean_symbol.startswith("NSE:"):
                display_name = clean_symbol.split(":")[1]
                yahoo_ticker = f"{display_name}.NS"
            elif clean_symbol.endswith(".BO") or clean_symbol.endswith(".NS"):
                display_name = clean_symbol.split(".")[0]
                yahoo_ticker = clean_symbol
            else:
                display_name = clean_symbol
                yahoo_ticker = f"{clean_symbol}.NS" # Default to NSE

            c_metrics = get_live_metrics_enhanced(yahoo_ticker)
            
            if c_metrics:
                live_rsi = c_metrics["rsi"]
                live_price = c_metrics["price"]
                
                if rules["condition"] == "below" and live_rsi < rules["target_rsi"]:
                    c_entry["alerted"] = True
                    c_entry["last_alert"] = datetime.datetime.now().isoformat()
                    send_telegram_message(
                        f"🎯 <b>{display_name}</b> Custom Alert!\n"
                        f"Live RSI ({live_rsi:.1f}) has dropped BELOW {rules['target_rsi']}.\n"
                        f"Price: \u20b9{live_price:.2f}"
                    )
                    log.info("CUSTOM RSI ALERT: %s RSI=%.1f (Target < %s)", display_name, live_rsi, rules["target_rsi"])
                    
                elif rules["condition"] == "above" and live_rsi > rules["target_rsi"]:
                    c_entry["alerted"] = True
                    c_entry["last_alert"] = datetime.datetime.now().isoformat()
                    send_telegram_message(
                        f"🎯 <b>{display_name}</b> Custom Alert!\n"
                        f"Live RSI ({live_rsi:.1f}) has crossed ABOVE {rules['target_rsi']}.\n"
                        f"Price: \u20b9{live_price:.2f}"
                    )
                    log.info("CUSTOM RSI ALERT: %s RSI=%.1f (Target > %s)", display_name, live_rsi, rules["target_rsi"])

    return state

# ----------------------------------------------------------------------
# MAIN ENTRY POINT
# ----------------------------------------------------------------------

def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Nifty Breakout Notifier.%s", " (single-shot mode)" if one_shot else "")
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
