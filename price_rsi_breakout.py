"""
Nifty Total Market Breakout Notifier -> Telegram
=========================================================
Dual-Engine Fyers & Yahoo Finance Edition:
- Fully Automated Headless TOTP Login via GitHub Secrets.
- Time-based routing: Fyers (9:15-3:30) -> Yahoo Finance (After Hours).
- Real-Time LTP override using Fyers Quotes API for zero-delay SME alerts.
- 45-minute hibernation + >1% price change requirement for repeats.
- Special Price Discovery / Call Auction Circular & News Scraper from Google Sheet.
- 17% to 22% 52W High / ATH Scanner.
"""

import os
import re
import io
import sys
import json
import time
import base64
import logging
import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import numpy as np
import pandas as pd

try:
    import pyotp
except ImportError:
    pyotp = None
    print("pyotp is recommended for headless login: pip install pyotp")

try:
    import yfinance as yf
except ImportError:
    print("yfinance is required: pip install yfinance pandas")
    raise

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None
    print("WARNING: fyers_apiv3 not installed. Will fallback to Yahoo Finance natively.")

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

# ----------------------------------------------------------------------
# CONFIG -- edit these
# ----------------------------------------------------------------------

SCRIPT_TAG = "🤖 [price_rsi_breakout.py]"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8974222959:AAG7S_dPYmDXBOX_ZnDWXMEenwqrmygkC-4")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1689560854")

GOOGLE_SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTicCnhvOb2njwMTaCp4oEnOv3LONbfE796ZVTtvPPA_uRN9C2lNeWXL813jxiW_n7zxf1-4HBG_c1G/pub?output=csv"

# --- 🔑 FYERS API CREDENTIALS ---
LOGIN_ID = "KX26944"
APP_ID = "0WC8A1Z15T-100"
SECRET_KEY = "AANC8ZMTAX"
REDIRECT_URI = "http://127.0.0.1:5000"

CLIENT_ID = APP_ID if APP_ID.endswith("-100") else f"{APP_ID}-100"
CACHE_DIR = os.path.expanduser("~/fyers_trading")
TOKEN_FILE = os.path.join(CACHE_DIR, ".fyers_token.txt")

# Strict 45-minute freeze and minimum 1% price change
POLL_INTERVAL_MINUTES = 15
ALERT_COOLDOWN_MINUTES = 45
MIN_PRICE_CHANGE_FOR_REPEAT_PCT = 1.0

RSI_PERIOD = 14
TRACK_WINDOW_DAYS = 15

MIN_MARKET_CAP_CR = 300.0
MAX_MARKET_CAP_CR = 31000.0
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
MOMENTUM_STATE_FILE = Path(__file__).parent / "momentum_state.json"

NSE_CSV_URL = "https://archives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"

FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

HISTORY_PERIOD = "18mo"

NEAR_52W_HIGH_PCT = 25.0
ABOVE_52W_LOW_PCT = 30.0
SMA200_TREND_LOOKBACK_DAYS = 22
VCP_LOOKBACK_DAYS = 60
RS_LOOKBACK_DAYS = 126

MOMENTUM_LEADER_RS_RANK_MIN = 90.0
MOMENTUM_LEADER_NEAR_HIGH_PCT = 15.0

ZANGER_BASE_LOOKBACK_DAYS = 30
ZANGER_VOLUME_MULT = 2.0
ZANGER_EARLY_MOVE_PCT = 1.5

BONDE_MIN_MOVE_PCT = 2.0
BONDE_MIN_VOLUME = 700000

TOP_N_MOMENTUM = 15

# ----------------------------------------------------------------------
# LOGGING & TELEGRAM
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("breakout_notifier")


def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram bot token / chat id not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{SCRIPT_TAG}\n{text}",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, data=payload, timeout=15)
        return True
    except Exception as e:
        log.error("Telegram send exception: %s", e)
        return False

# ----------------------------------------------------------------------
# FYERS AUTHENTICATION (Automated TOTP)
# ----------------------------------------------------------------------

def get_fyers_access_token():
    if not fyersModel:
        return None
        
    os.makedirs(CACHE_DIR, exist_ok=True)

    if os.path.exists(TOKEN_FILE):
        mtime = os.path.getmtime(TOKEN_FILE)
        if time.strftime("%Y-%m-%d", time.localtime(mtime)) == time.strftime("%Y-%m-%d"):
            with open(TOKEN_FILE, "r") as f:
                token = f.read().strip()
                if token:
                    log.info("🔑 Loaded valid daily Fyers token from %s", TOKEN_FILE)
                    return token

    totp_key = os.environ.get("FYERS_TOTP_KEY")
    pin = os.environ.get("FYERS_PIN")
    
    if totp_key and pin and pyotp:
        log.info("🤖 FYERS_TOTP_KEY found! Initiating fully automated headless login...")
        try:
            b64_fyers_id = base64.b64encode(LOGIN_ID.encode()).decode()
            b64_pin = base64.b64encode(str(pin).encode()).decode()
            
            s = requests.Session()
            res1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", json={"fy_id": b64_fyers_id, "app_id": "2"})
            req_key = res1.json()["request_key"]
            
            otp = pyotp.TOTP(totp_key).now()
            res2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json={"request_key": req_key, "otp": otp})
            req_key2 = res2.json()["request_key"]
            
            res3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", json={"request_key": req_key2, "identity_type": "pin", "identifier": b64_pin})
            access_token = res3.json()["data"]["access_token"]
            
            headers = {"authorization": f"Bearer {access_token}", "content-type": "application/json"}
            payload = {
                "fyers_id": LOGIN_ID, "app_id": CLIENT_ID[:-4], "redirect_uri": REDIRECT_URI,
                "appType": "100", "code_challenge": "", "state": "abcdefg", "scope": "", 
                "nonce": "", "response_type": "code", "create_cookie": True
            }
            res4 = s.post("https://api.fyers.in/api/v2/generate-authcode", json=payload, headers=headers)
            auth_code = res4.json()["data"]["code"]
            
            session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
            session.set_token(auth_code)
            response = session.generate_token()
            
            if response.get("s") == "ok":
                final_token = response["access_token"]
                with open(TOKEN_FILE, "w") as f:
                    f.write(final_token)
                log.info("✅ Headless Fyers token generated successfully!")
                return final_token
            else:
                log.error("Headless token gen failed: %s", response)
        except Exception as e:
            log.error("Exception during headless login: %s", e)

    if not sys.stdin.isatty():
        log.info("🌐 Running headless without FYERS_TOTP_KEY. Activating Yahoo fallback.")
        return None

    print(f"\n--- 🔐 FYERS AUTHENTICATION REQUIRED (User: {LOGIN_ID}) ---")
    session = fyersModel.SessionModel(
        client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI,
        response_type="code", grant_type="authorization_code",
    )

    auth_url = session.generate_authcode()
    print("\n1. Open this link in your browser to log in:")
    print(f"\n👉  {auth_url}\n")
    user_input = input("2. Paste the full redirected URL (or auth_code) here: ").strip()

    auth_code = user_input
    if "auth_code=" in user_input:
        try:
            parsed_url = urlparse(user_input)
            query_params = parse_qs(parsed_url.query)
            if "auth_code" in query_params:
                auth_code = query_params["auth_code"][0]
        except Exception:
            match = re.search(r"auth_code=([^&]+)", user_input)
            if match:
                auth_code = match.group(1)

    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") == "ok" and "access_token" in response:
        access_token = response["access_token"]
        with open(TOKEN_FILE, "w") as f:
            f.write(access_token)
        print("✅ Access token generated and saved successfully!\n")
        return access_token
    else:
        log.error("Failed to generate Fyers token: %s. Falling back to Yahoo.", response)
        return None

# ----------------------------------------------------------------------
# TECHNICAL MATH & LIVE ENGINE
# ----------------------------------------------------------------------

def is_market_hours_now() -> bool:
    now = datetime.datetime.now(IST) if IST else datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end

def rsi_fyers_tradingview(close_prices, period=RSI_PERIOD):
    if len(close_prices) < period + 1:
        return None

    df = pd.DataFrame({"close": close_prices})
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(com=period - 1, min_periods=1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=1, adjust=False).mean()

    rs = avg_gain / avg_loss
    rs = rs.replace([np.inf], 999999.0)
    rs = rs.replace([-np.inf], 0.0)
    rs = rs.fillna(0.0)

    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.clip(0.0, 100.0)
    rsi.iloc[:period] = np.nan

    latest_rsi = rsi.iloc[-1]
    return float(latest_rsi) if not pd.isna(latest_rsi) else None


def get_live_metrics(fyers, symbol: str, exchange: str = "NSE") -> dict:
    if fyers and is_market_hours_now():
        time.sleep(0.15) 
        fyers_sym = f"{exchange}:{symbol}-EQ" if not symbol.isdigit() else f"BSE:{symbol}-EQ"
        data = {
            "symbol": fyers_sym, "resolution": "D", "date_format": "1",
            "range_from": (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
            "range_to": datetime.date.today().strftime("%Y-%m-%d"), "cont_flag": "1"
        }
        
        resp = fyers.history(data=data)
        
        if resp.get("s") != "ok" or "candles" not in resp:
            fyers_sym = f"{exchange}:{symbol}-SM"
            data["symbol"] = fyers_sym
            resp = fyers.history(data=data)
            if resp.get("s") != "ok" or "candles" not in resp:
                fyers_sym = f"{exchange}:{symbol}-BE"
                data["symbol"] = fyers_sym
                resp = fyers.history(data=data)
                
        if resp.get("s") == "ok" and "candles" in resp:
            candles = resp["candles"]
            if len(candles) >= 5:
                df = pd.DataFrame(candles, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
                df["date"] = pd.to_datetime(df["timestamp"], unit='s', utc=True).dt.tz_convert("Asia/Kolkata")
                df.set_index("date", inplace=True)
                
                try:
                    q_resp = fyers.quotes(data={"symbols": fyers_sym})
                    if q_resp.get("s") == "ok" and q_resp.get("d"):
                        v = q_resp["d"][0]["v"]
                        live_price = float(v["lp"])
                        live_open = float(v["open_price"])
                        live_vol = float(v["volume"])
                        live_high = float(v["high_price"])
                        live_low = float(v["low_price"])
                        
                        last_idx = df.index[-1]
                        
                        if last_idx.date() == datetime.date.today():
                            df.loc[last_idx, "Close"] = live_price
                            df.loc[last_idx, "Open"] = live_open
                            df.loc[last_idx, "Volume"] = live_vol
                            df.loc[last_idx, "High"] = live_high
                            df.loc[last_idx, "Low"] = live_low
                        else:
                            new_idx = pd.Timestamp.now(tz="Asia/Kolkata")
                            df.loc[new_idx] = {
                                "Open": live_open, "High": live_high, "Low": live_low,
                                "Close": live_price, "Volume": live_vol, "timestamp": int(time.time())
                            }
                except Exception:
                    pass

                closes = df["Close"]
                today = df.iloc[-1]
                yesterday = df.iloc[-2]
                day3_ago = df.iloc[-4] if len(df) >= 4 else yesterday
                
                weekly_closes = closes.resample('W-FRI').last().dropna()
                weekly_rsi = rsi_fyers_tradingview(weekly_closes.tolist(), RSI_PERIOD) if len(weekly_closes) > 14 else 50.0

                price = float(today["Close"])
                open_price = float(today["Open"])
                prev_close = float(yesterday["Close"])

                metrics_dict = {
                    "price": price,
                    "volume": float(today["Volume"]),
                    "prev_close": prev_close,
                    "prev_volume": float(yesterday["Volume"]),
                    "close_3d_ago": float(day3_ago["Close"]),
                    "rsi": rsi_fyers_tradingview(closes.tolist(), RSI_PERIOD),
                    "weekly_rsi": weekly_rsi,
                    "open_change_rs": price - open_price,
                    "open_change_pct": ((price - open_price) / open_price * 100) if open_price else 0.0,
                    "day_change_rs": price - prev_close,
                    "day_change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0.0,
                    "ema_10": float(closes.ewm(span=10, adjust=False).mean().iloc[-1]),
                    "ema_20": float(closes.ewm(span=20, adjust=False).mean().iloc[-1]),
                    "ema_21": float(closes.ewm(span=21, adjust=False).mean().iloc[-1]),
                    "ema_50": float(closes.ewm(span=50, adjust=False).mean().iloc[-1]),
                    "ema_100": float(closes.ewm(span=100, adjust=False).mean().iloc[-1]),
                    "ema_200": float(closes.ewm(span=200, adjust=False).mean().iloc[-1]),
                    "sma_50": float(closes.rolling(50).mean().iloc[-1] if len(closes) >= 50 else 0),
                    "sma_200": float(closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else 0),
                    "sma_vol_5": float(df["Volume"].rolling(5).mean().iloc[-1]) if len(df) >= 5 else 0.0,
                    "sma_vol_20": float(df["Volume"].rolling(20).mean().iloc[-1]) if len(df) >= 20 else 0.0,
                }
                return metrics_dict

    yahoo_ticker = f"{symbol}.NS" if exchange == "NSE" else f"{symbol}.BO"
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="1y", interval="1d")
        if hist.empty or len(hist) < 5: 
            return None
        
        closes = hist["Close"]
        today = hist.iloc[-1]
        yesterday = hist.iloc[-2]
        day3_ago = hist.iloc[-4] if len(hist) >= 4 else yesterday
        
        weekly_closes = closes.resample('W-FRI').last().dropna()
        weekly_rsi = rsi_fyers_tradingview(weekly_closes.tolist(), RSI_PERIOD) if len(weekly_closes) > 14 else 50.0

        price = float(today["Close"])
        open_price = float(today["Open"])
        prev_close = float(yesterday["Close"])

        metrics_dict = {
            "price": price,
            "volume": float(today["Volume"]),
            "prev_close": prev_close,
            "prev_volume": float(yesterday["Volume"]),
            "close_3d_ago": float(day3_ago["Close"]),
            "rsi": rsi_fyers_tradingview(closes.tolist(), RSI_PERIOD),
            "weekly_rsi": weekly_rsi,
            "open_change_rs": price - open_price,
            "open_change_pct": ((price - open_price) / open_price * 100) if open_price else 0.0,
            "day_change_rs": price - prev_close,
            "day_change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0.0,
            "ema_10": float(closes.ewm(span=10, adjust=False).mean().iloc[-1]),
            "ema_20": float(closes.ewm(span=20, adjust=False).mean().iloc[-1]),
            "ema_21": float(closes.ewm(span=21, adjust=False).mean().iloc[-1]),
            "ema_50": float(closes.ewm(span=50, adjust=False).mean().iloc[-1]),
            "ema_100": float(closes.ewm(span=100, adjust=False).mean().iloc[-1]),
            "ema_200": float(closes.ewm(span=200, adjust=False).mean().iloc[-1]),
            "sma_50": float(closes.rolling(50).mean().iloc[-1] if len(closes) >= 50 else 0),
            "sma_200": float(closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else 0),
            "sma_vol_5": float(hist["Volume"].rolling(5).mean().iloc[-1]) if len(hist) >= 5 else 0.0,
            "sma_vol_20": float(hist["Volume"].rolling(20).mean().iloc[-1]) if len(hist) >= 20 else 0.0,
        }
        return metrics_dict
    except Exception as e:
        log.warning("Could not fetch Yahoo fallback metrics for %s: %s", yahoo_ticker, e)
        return None

def get_baseline_metrics(fyers, symbol: str, date: datetime.date, exchange="NSE"):
    if fyers and is_market_hours_now():
        time.sleep(0.15)
        fyers_sym = f"{exchange}:{symbol}-EQ" if not symbol.isdigit() else f"BSE:{symbol}-EQ"
        data = {
            "symbol": fyers_sym, "resolution": "D", "date_format": "1",
            "range_from": (date - datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
            "range_to": (date + datetime.timedelta(days=7)).strftime("%Y-%m-%d"), "cont_flag": "1"
        }
        resp = fyers.history(data=data)
        if resp.get("s") != "ok" or "candles" not in resp:
            data["symbol"] = f"{exchange}:{symbol}-SM"
            resp = fyers.history(data=data)
            
        if resp.get("s") == "ok" and "candles" in resp:
            candles = resp["candles"]
            if len(candles) >= 2:
                df = pd.DataFrame(candles, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
                df["date"] = pd.to_datetime(df["timestamp"], unit='s', utc=True).dt.tz_convert("Asia/Kolkata").dt.date
                df.set_index("date", inplace=True)
                
                idx = next((d for d in df.index if d >= date), None)
                if idx:
                    closes_up_to = df.loc[:idx, "Close"]
                    baseline_dict = {
                        "day_high": float(df.loc[idx, "High"]),
                        "day_low": float(df.loc[idx, "Low"]),
                        "baseline_rsi": rsi_fyers_tradingview(closes_up_to.tolist(), RSI_PERIOD),
                        "actual_date": idx,
                    }
                    return baseline_dict

    yahoo_ticker = f"{symbol}.NS" if exchange == "NSE" else f"{symbol}.BO"
    try:
        hist = yf.Ticker(yahoo_ticker).history(start=date - datetime.timedelta(days=365), end=date + datetime.timedelta(days=7))
        if hist.empty: 
            return None

        hist_dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        idx_dt = next((d for d in hist_dates if d >= date), None)
        if idx_dt:
            idx = hist_dates.index(idx_dt)
            closes_up_to = hist["Close"].iloc[: idx + 1]
            baseline_dict = {
                "day_high": float(hist["High"].iloc[idx]),
                "day_low": float(hist["Low"].iloc[idx]),
                "baseline_rsi": rsi_fyers_tradingview(closes_up_to.tolist(), RSI_PERIOD),
                "actual_date": hist_dates[idx],
            }
            return baseline_dict
    except Exception:
        pass
    return None

# ----------------------------------------------------------------------
# MOMENTUM SCREENING & TECHNICAL CRITERIA (Batch End-of-Day)
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
    fifty2w_high, fifty2w_low = float(window.max()), float(window.min())

    near_high = price >= (1 - NEAR_52W_HIGH_PCT / 100) * fifty2w_high
    above_low = price >= (1 + ABOVE_52W_LOW_PCT / 100) * fifty2w_low
    stage2 = bool(price > s50 > s150 > s200 and sma200_rising and near_high and above_low)

    status_dict = {
        "sma50": s50, 
        "sma150": s150, 
        "sma200": s200, 
        "sma200_rising": sma200_rising,
        "stage2": stage2, 
        "fifty2w_high": fifty2w_high, 
        "fifty2w_low": fifty2w_low, 
        "near_high": near_high,
    }
    return status_dict

def detect_vcp(highs: pd.Series, lows: pd.Series, lookback: int = VCP_LOOKBACK_DAYS) -> bool:
    if len(highs) < lookback or len(lows) < lookback: 
        return False
    h, l = highs[-lookback:], lows[-lookback:]
    third = lookback // 3
    ranges = []
    for i in range(3):
        seg_h, seg_l = h[i * third:(i + 1) * third], l[i * third:(i + 1) * third]
        if seg_l.empty or float(seg_l.min()) <= 0: 
            return False
        ranges.append((float(seg_h.max()) - float(seg_l.min())) / float(seg_l.min()) * 100)
    return ranges[0] > ranges[1] > ranges[2]

def get_market_cap_cr(symbol: str) -> float:
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

        mcap_cr = get_market_cap_cr(symbol)
        if mcap_cr is not None and (mcap_cr < MIN_MARKET_CAP_CR or mcap_cr > MAX_MARKET_CAP_CR): 
            continue

        trend = get_trend_template_status(closes)
        if trend is None: 
            continue
        
        rs_return = (float(closes.iloc[-1]) / float(closes.iloc[-RS_LOOKBACK_DAYS]) - 1) * 100 if len(closes) > RS_LOOKBACK_DAYS and float(closes.iloc[-RS_LOOKBACK_DAYS]) > 0 else None
        
        ath_high = float(highs.max()) if len(highs) > 0 else float(closes.max())
        
        symbol_data_dict = {
            **trend,
            "ath_high": ath_high,
            "vcp_contracting": detect_vcp(highs, lows),
            "rs_return_6m": rs_return,
            "avg_volume_50d": float(volumes[-50:].mean()),
            "base_high": float(highs.iloc[-ZANGER_BASE_LOOKBACK_DAYS - 1:-1].max()) if len(highs) > ZANGER_BASE_LOOKBACK_DAYS else float(highs.max()),
            "base_low": float(lows.iloc[-ZANGER_BASE_LOOKBACK_DAYS - 1:-1].min()) if len(lows) > ZANGER_BASE_LOOKBACK_DAYS else float(lows.min()),
            "prev_close": float(closes.iloc[-1]),
            "monthly_rsi": rsi_fyers_tradingview(closes.resample('ME').last().dropna().tolist(), RSI_PERIOD) if len(closes.resample('ME').last().dropna()) > 14 else 50.0,
            "yesterday_rsi": rsi_fyers_tradingview(closes.tolist(), RSI_PERIOD),
            "ema_10_prev": float(closes.ewm(span=10, adjust=False).mean().iloc[-1]),
            "ema_21_prev": float(closes.ewm(span=21, adjust=False).mean().iloc[-1]),
            "ema_50_prev": float(closes.ewm(span=50, adjust=False).mean().iloc[-1]),
            "ema_100_prev": float(closes.ewm(span=100, adjust=False).mean().iloc[-1]),
            "ema_200_prev": float(closes.ewm(span=200, adjust=False).mean().iloc[-1]),
            "mcap_cr": mcap_cr,
        }
        per_symbol_data[symbol] = symbol_data_dict
        if rs_return is not None: 
            returns_6m[symbol] = rs_return

    sorted_syms = sorted(returns_6m.keys(), key=lambda s: returns_6m[s], reverse=True)
    rs_rank_pct = {s: round(100 * (1 - i / (len(sorted_syms) or 1)), 1) for i, s in enumerate(sorted_syms)}

    watchlist = {}
    stage2_vcp_list, momentum_leader_list = [], []

    for symbol, d in per_symbol_data.items():
        rs_rank = rs_rank_pct.get(symbol)
        watchlist[symbol] = {**d, "rs_rank": rs_rank}
        
        if d["stage2"] and d["vcp_contracting"]: 
            stage2_vcp_list.append((symbol, rs_rank))
        if rs_rank is not None and rs_rank >= MOMENTUM_LEADER_RS_RANK_MIN and d["near_high"]: 
            momentum_leader_list.append((symbol, rs_rank))

    stage2_vcp_list.sort(key=lambda x: (x[1] or 0), reverse=True)
    momentum_leader_list.sort(key=lambda x: (x[1] or 0), reverse=True)

    momentum_state = {"last_scan_date": today_str, "watchlist": watchlist}

    lines = [
        f"\U0001F4CA <b>Daily Momentum Scan \u2014 {today_str}</b>",
        f"<i>Universe: {len(per_symbol_data)} stocks within ₹{MIN_MARKET_CAP_CR:,.0f} Cr \u2013 ₹{MAX_MARKET_CAP_CR:,.0f} Cr.</i>", "",
        f"\U0001F7E2 <b>Minervini Stage 2 + VCP</b> ({len(stage2_vcp_list)} stocks)",
    ]
    for s, r in stage2_vcp_list[:TOP_N_MOMENTUM]: 
        lines.append(f"  {s} (RS rank {r})")
    lines += ["", f"\U0001F31F <b>Momentum Leaders</b> ({len(momentum_leader_list)} stocks)"]
    for s, r in momentum_leader_list[:TOP_N_MOMENTUM]: 
        lines.append(f"  {s} (RS rank {r})")
    
    digest = "\n".join(lines)
    send_telegram_message(digest)
    return momentum_state

# ----------------------------------------------------------------------
# COOLDOWN & 1% PRICE CHANGE HIBERNATION ENGINE
# ----------------------------------------------------------------------

def alert_allowed(entry: dict, alert_key_prefix: str, current_price: float = None) -> bool:
    last_time_str = entry.get(f"{alert_key_prefix}_last_alert")
    if not last_time_str:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last_time_str)
    except Exception:
        return True
    
    # 1. Check 45-minute hibernation
    if (datetime.datetime.now() - last_dt) < datetime.timedelta(minutes=ALERT_COOLDOWN_MINUTES):
        return False
    
    # 2. Check > 1% price change condition
    last_price = entry.get(f"{alert_key_prefix}_last_price")
    if current_price is not None and last_price is not None:
        try:
            pct_diff = abs(current_price - float(last_price)) / float(last_price) * 100.0
            if pct_diff < MIN_PRICE_CHANGE_FOR_REPEAT_PCT:
                return False
        except ZeroDivisionError:
            pass
            
    return True

def record_alert(entry: dict, alert_key_prefix: str, current_price: float = None):
    entry[f"{alert_key_prefix}_last_alert"] = datetime.datetime.now().isoformat()
    if current_price is not None:
        entry[f"{alert_key_prefix}_last_price"] = float(current_price)

# ----------------------------------------------------------------------
# INTRADAY MOMENTUM SCANNER HEADS
# ----------------------------------------------------------------------

def check_intraday_momentum_triggers(momentum_state: dict, fyers) -> dict:
    watchlist = momentum_state.get("watchlist", {})
    if not watchlist: 
        return momentum_state

    # --- TIME LOCK: Completely stop technical scanners after 3:40 PM ---
    now = datetime.datetime.now(IST) if IST else datetime.datetime.now()
    if now.weekday() >= 5:  
        return momentum_state  
    
    current_time = now.time()
    start_time = datetime.time(9, 0)
    end_time = datetime.time(15, 40)
    
    if not (start_time <= current_time <= end_time):
        return momentum_state
    # -------------------------------------------------------------------

    for symbol, entry in watchlist.items():
        metrics = get_live_metrics(fyers, symbol, "NSE")
        if not metrics or metrics.get("weekly_rsi", 100) >= MAX_WEEKLY_RSI:
            continue

        price = metrics["price"]
        volume = metrics["volume"]
        prev_close = metrics["prev_close"]
        prev_volume = metrics["prev_volume"]
        avg_vol50 = entry.get("avg_volume_50d") or 0
        base_high = entry.get("base_high")
        pct_change = (price - prev_close) / prev_close * 100 if prev_close else None

        if alert_allowed(entry, "zanger", price) and base_high and avg_vol50 and pct_change is not None:
            if pct_change >= ZANGER_EARLY_MOVE_PCT and volume >= ZANGER_VOLUME_MULT * avg_vol50 and price >= (base_high * 0.95):
                record_alert(entry, "zanger", price)
                send_telegram_message(f"\U0001F4A5 <b>{symbol}</b> Zanger Early Entry Setup!\nPrice \u20b9{price:.2f} (up {pct_change:+.1f}%) near {ZANGER_BASE_LOOKBACK_DAYS}-day base high \u20b9{base_high:.2f} on {volume / avg_vol50:.1f}x avg volume.")

        if alert_allowed(entry, "bonde", price) and pct_change is not None and prev_volume is not None and metrics.get("close_3d_ago"):
            return_3d = (prev_close - metrics["close_3d_ago"]) / metrics["close_3d_ago"] * 100
            if entry.get("stage2") and return_3d < 1.0 and volume > prev_volume and volume >= BONDE_MIN_VOLUME and pct_change >= BONDE_MIN_MOVE_PCT:
                record_alert(entry, "bonde", price)
                send_telegram_message(f"\u26A1 <b>{symbol}</b> Bonde Early Entry Model Trigger!\nConsolidated ({return_3d:+.1f}%), moved {pct_change:+.1f}% today. Vol ({volume:,.0f}) > Yesterday, in Stage 2. Price \u20b9{price:.2f}")

        if alert_allowed(entry, "resistance", price) and base_high:
            if 0 < ((base_high - price) / base_high * 100) <= 4.0:
                record_alert(entry, "resistance", price)
                send_telegram_message(f"\U0001F3AF <b>{symbol}</b> Horizontal Resistance Scanner!\nPrice \u20b9{price:.2f} is within 4% below the recent base high (\u20b9{base_high:.2f}).")

        if alert_allowed(entry, "mtf", price) and metrics.get("rsi") is not None:
            m_rsi, y_rsi = entry.get("monthly_rsi", 50.0), entry.get("yesterday_rsi", 50.0)
            ema_condition = price >= metrics["ema_200"] * 1.03 or price >= metrics["ema_50"] * 1.03 or price >= metrics["ema_21"] * 1.03
            rsi_condition = metrics["rsi"] > y_rsi and metrics["rsi"] > 30 and m_rsi <= 56 and metrics["weekly_rsi"] <= metrics["rsi"]
            if rsi_condition and ema_condition:
                record_alert(entry, "mtf", price)
                send_telegram_message(f"\U0001F52E <b>{symbol}</b> MTF RSI + EMA Trigger!\nPrice \u20b9{price:.2f} (Spiked \u22653% above key EMA).\nLive Daily RSI: {metrics['rsi']:.1f} | Weekly: {metrics['weekly_rsi']:.1f} | Monthly: {m_rsi:.1f}")
        
        # ----------------------------------------------------------------------
        # Strict 50 EMA Pullback Scanner (Rules 1-8)
        # ----------------------------------------------------------------------
        if alert_allowed(entry, "ema50_pullback", price):
            ema_50 = metrics.get("ema_50")
            rsi = metrics.get("rsi")
            y_rsi = entry.get("yesterday_rsi")
            w_rsi = metrics.get("weekly_rsi")
            mcap = entry.get("mcap_cr", 0)
            sma_vol_5 = metrics.get("sma_vol_5")
            sma_vol_20 = metrics.get("sma_vol_20")

            if ema_50 and rsi and y_rsi and w_rsi:
                cond_price = (ema_50 * 0.975) <= price <= (ema_50 * 1.06)
                cond_rsi_bounds = 30 <= rsi < 58
                cond_mcap = mcap > 300
                cond_vol = (sma_vol_20 and sma_vol_20 > 0 and (sma_vol_5 / sma_vol_20) > 1.5)
                cond_close = price >= prev_close
                cond_rsi_daily = rsi > y_rsi
                cond_rsi_weekly = rsi > w_rsi

                if (cond_price and cond_rsi_bounds and cond_mcap and cond_vol and 
                    cond_close and cond_rsi_daily and cond_rsi_weekly):
                    
                    record_alert(entry, "ema50_pullback", price)
                    send_telegram_message(
                        f"🧲 <b>{symbol}</b> Strict 50 EMA Pullback Alert!\n"
                        f"Price ₹{price:.2f} is hovering near 50 EMA (₹{ema_50:.2f}) with expanding volume.\n"
                        f"RSI: {rsi:.1f} (Up from {y_rsi:.1f}) | Mcap: ₹{mcap:.0f} Cr"
                    )

        # ----------------------------------------------------------------------
        # Within 17% to 22% of Yearly High / ATH Scanner
        # ----------------------------------------------------------------------
        if alert_allowed(entry, "near_high_17_22", price):
            ref_high = max(entry.get("fifty2w_high") or 0.0, entry.get("ath_high") or 0.0)
            if ref_high > 0:
                pct_from_high = ((ref_high - price) / ref_high) * 100
                if 17.0 <= pct_from_high <= 22.0:
                    record_alert(entry, "near_high_17_22", price)
                    send_telegram_message(
                        f"🏔️ <b>{symbol}</b> Near Yearly High / ATH Scanner!\n"
                        f"Price ₹{price:.2f} is within {pct_from_high:.1f}% of its 52W/ATH High (₹{ref_high:.2f})."
                    )

    return momentum_state

# ----------------------------------------------------------------------
# NEWS & SPECIAL PRICE DISCOVERY CIRCULARS SCRAPER
# ----------------------------------------------------------------------

def fetch_recent_news_for_alerts() -> list:
    results = []
    session = requests.Session()
    
    # 1. NSE Announcements (Equities + SME)
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
        time.sleep(1)
        
        for idx in ["equities", "sme"]:
            resp = session.get(f"https://www.nseindia.com/api/corporate-announcements?index={idx}", headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                for item in resp.json():
                    results.append({
                        "symbol": (item.get("symbol") or "").upper(),
                        "subject": f"{item.get('desc', '')} {item.get('attchmntText', '')}",
                        "link": item.get("attchmntFile", "")
                    })
    except Exception:
        pass

    # 2. NSE Exchange Circulars (For Special Call Auction / Price Discovery Sessions)
    try:
        time.sleep(1)
        circ_resp = session.get("https://www.nseindia.com/api/circulars", headers=HEADERS, timeout=15)
        if circ_resp.status_code == 200:
            data = circ_resp.json()
            items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in items:
                subject = f"{item.get('circNo', '')} {item.get('sub', '')}"
                circ_file = item.get("circFile", "")
                link = f"https://archives.nseindia.com/content/circulars/{circ_file}" if circ_file else ""
                
                # Tagged under CIRCULAR to match Google Sheet entries
                results.append({
                    "symbol": "CIRCULAR",
                    "subject": subject,
                    "link": link
                })
    except Exception:
        pass

    # 3. BSE Announcements & Notices
    try:
        today = datetime.datetime.now().strftime("%Y%m%d")
        from_date = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w?pageno=1&strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P&strToDate={today}&strType=C&subcategory=-1"
        resp = requests.get(url, headers={**HEADERS, "Referer": "https://www.bseindia.com/corporates/ann.html"}, timeout=15)
        if resp.status_code == 200 and "Table" in resp.json():
            for item in resp.json()["Table"]:
                results.append({
                    "symbol": str(item.get("SCRIP_CD", "")),
                    "subject": f"{item.get('NEWSSUB', '')} {item.get('HEADLINE', '')}",
                    "link": item.get("ATTACHMENTNAME", "")
                })
    except Exception:
        pass
        
    return results

# ----------------------------------------------------------------------
# GOOGLE SHEETS DYNAMIC CUSTOM ALERTS
# ----------------------------------------------------------------------

def parse_target_options(target_raw: str, metric: str) -> list:
    parts = re.split(r",|\s+(?:or|OR)\s+", str(target_raw).strip())
    options = [p.strip() for p in parts if p.strip()][:3]
    parsed = []
    for opt in options:
        if metric == "news":
            parsed.append(opt.lower())
        else:
            try:
                parsed.append(float(opt))
            except ValueError:
                parsed.append(opt.lower())
    return parsed

def fetch_custom_alerts_from_sheet(sheet_url: str) -> dict:
    if not sheet_url or "docs.google.com" not in sheet_url: 
        return {}
    try:
        df = pd.read_csv(sheet_url)
        df.columns = [str(c).strip().lower() for c in df.columns]
        if not {"exchange", "symbol", "metric", "condition", "target"}.issubset(set(df.columns)):
            return {}

        sheet_alerts = {}
        for _, row in df.dropna(subset=["symbol", "metric", "condition", "target"]).iterrows():
            sym = str(row["symbol"]).strip().upper()
            exchange = str(row["exchange"]).strip().upper()
            metric = str(row["metric"]).strip().lower()
            condition = str(row["condition"]).strip().lower()
            targets = parse_target_options(row["target"], metric)
            if not targets or condition not in ("above", "below", "contains"): 
                continue

            sheet_alerts[f"{'BSE:' if exchange == 'BSE' else ''}{sym}"] = {
                "metric": metric, 
                "condition": condition, 
                "targets": targets, 
                "target_raw": str(row["target"]).strip()
            }
        return sheet_alerts
    except Exception:
        return {}

# ----------------------------------------------------------------------
# MAIN POLLING LOOP
# ----------------------------------------------------------------------

def normalise_company(name: str) -> str:
    name = name.upper()
    name = re.sub(r"\b(LIMITED|LTD|LTD\.|THE)\b", "", name)
    return re.sub(r"[^A-Z0-9]", "", name).strip()

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
    except Exception:
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
    except Exception:
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
        return symbols
    except Exception:
        return FALLBACK_SYMBOLS

def prune_expired(state: dict) -> dict:
    cutoff = datetime.datetime.now() - datetime.timedelta(days=TRACK_WINDOW_DAYS)
    kept = {}
    for symbol, entry in state.items():
        if symbol.startswith("custom_alert_"):
            kept[symbol] = entry
            continue
        try:
            result_date = datetime.datetime.fromisoformat(entry["result_date"])
            if result_date >= cutoff:
                kept[symbol] = entry
        except Exception:
            continue
    return kept

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def load_momentum_state() -> dict:
    if MOMENTUM_STATE_FILE.exists():
        try:
            return json.loads(MOMENTUM_STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_scan_date": None, "watchlist": {}}

def poll_once(state: dict, fyers) -> dict:
    universe = set(get_universe_symbols())
    universe_normalised = {normalise_company(s): s for s in universe}

    nse_hits = fetch_nse_result_symbols() & universe
    bse_hits = {universe_normalised[n] for n in fetch_bse_result_companies() if n in universe_normalised}
    new_result_symbols = (nse_hits | bse_hits) - set(state.keys())

    today = datetime.date.today()
    for symbol in new_result_symbols:
        metrics = get_baseline_metrics(fyers, symbol, today, "NSE")
        if metrics is None: 
            continue
        state_dict = {
            "result_date": datetime.datetime.combine(metrics["actual_date"], datetime.time()).isoformat(),
            "day_high": metrics["day_high"], 
            "day_low": metrics["day_low"], 
            "baseline_rsi": metrics["baseline_rsi"],
        }
        state[symbol] = state_dict
        
        rsi_line = f"Result-day RSI({RSI_PERIOD}): {metrics['baseline_rsi']:.1f}\n" if metrics["baseline_rsi"] else ""
        send_telegram_message(f"\U0001F4CC <b>{symbol}</b> result filed today.\nResult-day High: \u20b9{metrics['day_high']:.2f} | Low: \u20b9{metrics['day_low']:.2f}\n{rsi_line}Will alert if price breaks this High/Low or RSI crosses.")

    state = prune_expired(state)
    
    for symbol, entry in state.items():
        if symbol.startswith("custom_alert_"): 
            continue 
        if "day_low" not in entry or "baseline_rsi" not in entry: 
            continue

        metrics = get_live_metrics(fyers, symbol, "NSE")
        if not metrics or metrics.get("weekly_rsi", 100) >= MAX_WEEKLY_RSI: 
            continue

        price, rsi = metrics["price"], metrics["rsi"]

        if price > entry["day_high"] and alert_allowed(entry, "price_high", price):
            record_alert(entry, "price_high", price)
            send_telegram_message(f"\U0001F680 <b>{symbol}</b> price broke ABOVE result-day High!\nCurrent: \u20b9{price:.2f} | Result-day High: \u20b9{entry['day_high']:.2f}")

        if price < entry["day_low"] and alert_allowed(entry, "price_low", price):
            record_alert(entry, "price_low", price)
            send_telegram_message(f"\U0001F53B <b>{symbol}</b> price broke BELOW result-day Low!\nCurrent: \u20b9{price:.2f} | Result-day Low: \u20b9{entry['day_low']:.2f}")

        if rsi is not None and entry.get("baseline_rsi") is not None:
            if rsi > entry["baseline_rsi"] and alert_allowed(entry, "rsi_up"):
                record_alert(entry, "rsi_up")
                send_telegram_message(f"\U0001F4C8 <b>{symbol}</b> RSI crossed ABOVE result-day RSI!\nCurrent RSI: {rsi:.1f} | Base RSI: {entry['baseline_rsi']:.1f} | Price: \u20b9{price:.2f}")

            if rsi < entry["baseline_rsi"] and alert_allowed(entry, "rsi_down"):
                record_alert(entry, "rsi_down")
                send_telegram_message(f"\U0001F4C9 <b>{symbol}</b> RSI crossed BELOW result-day RSI!\nCurrent RSI: {rsi:.1f} | Base RSI: {entry['baseline_rsi']:.1f} | Price: \u20b9{price:.2f}")

    custom_alerts = {**fetch_custom_alerts_from_sheet(GOOGLE_SHEET_CSV_URL)}
    recent_news = fetch_recent_news_for_alerts()

    for symbol, rules in custom_alerts.items():
        state_key = f"custom_alert_{symbol}_{rules['metric']}"
        if state_key not in state: 
            state[state_key] = {"alerted": False, "last_alert": None}
        c_entry = state[state_key]
        clean_symbol, exchange = symbol.split(":")[-1].upper(), "BSE" if symbol.startswith("BSE:") else "NSE"

        # Special News & Price Discovery / Call Auction Circular Matcher
        if rules["metric"] == "news":
            for news_item in recent_news:
                match_symbol = (news_item["symbol"] == clean_symbol) or (clean_symbol in ("CIRCULAR", "ALL", "*"))
                if match_symbol:
                    subj_lower = news_item["subject"].lower()
                    matched_kw = next((t for t in rules["targets"] if t in subj_lower), None)
                    if matched_kw and rules["condition"] == "contains":
                        news_fp = str(news_item["subject"])[:60]
                        if c_entry.get("last_news_fingerprint") != news_fp:
                            c_entry["alerted"] = True
                            c_entry["last_alert"] = datetime.datetime.now().isoformat()
                            c_entry["last_news_fingerprint"] = news_fp
                            
                            header_title = "🏛️ Exchange Circular / Price Discovery" if news_item["symbol"] == "CIRCULAR" else f"📰 {clean_symbol} Catalyst Alert"
                            link_str = f"\n🔗 {news_item['link']}" if news_item.get("link") else ""
                            
                            send_telegram_message(
                                f"<b>{header_title}</b>!\n"
                                f"Matched: <b>'{matched_kw}'</b> (Target: <i>{rules['target_raw']}</i>)\n\n"
                                f"<i>{news_item['subject']}</i>{link_str}"
                            )
            continue

        # Numeric / Price Alerts with 45-Min + 1% Price Change Rule
        c_metrics = get_live_metrics(fyers, clean_symbol, exchange)
        if not c_metrics: 
            continue

        metric_type, cond, current_val = rules["metric"], rules["condition"], c_metrics.get(rules["metric"])
        if current_val is None: 
            continue

        current_price = c_metrics.get("price")
        if alert_allowed(c_entry, "custom", current_price):
            for target_rule in rules["targets"]:
                target_val = c_metrics.get(target_rule) if isinstance(target_rule, str) else target_rule
                if target_val is None: 
                    continue
                
                if (cond == "below" and current_val < target_val) or (cond == "above" and current_val > target_val):
                    record_alert(c_entry, "custom", current_price)
                    t_str = f"{target_rule.upper()} (₹{target_val:.2f})" if isinstance(target_rule, str) else (f"{target_val:+.2f}%" if "pct" in metric_type else f"{target_val:.1f}" if "rsi" in metric_type else f"₹{target_val:+.2f}" if "change" in metric_type else f"₹{target_val:.2f}")
                    v_str = f"{current_val:+.2f}%" if "pct" in metric_type else f"{current_val:.1f}" if "rsi" in metric_type else f"₹{current_val:+.2f}" if "change" in metric_type else f"₹{current_val:.2f}"
                    send_telegram_message(f"🎯 <b>{clean_symbol}</b> Custom Alert!\n{metric_type.replace('_', ' ').title()} ({v_str}) has {'dropped BELOW' if cond == 'below' else 'crossed ABOVE'} {t_str}.\nCurrent Price: ₹{current_price:.2f}")
                    break

    return state

# ----------------------------------------------------------------------
# MAIN ENTRY POINT
# ----------------------------------------------------------------------

def main():
    one_shot = "--once" in sys.argv
    log.info("Starting Nifty Breakout Notifier.%s", " (single-shot mode)" if one_shot else "")
    state = load_state()
    momentum_state = load_momentum_state()

    fyers_token = get_fyers_access_token()
    fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=fyers_token, log_path="/tmp") if fyers_token else None

    if one_shot:
        try:
            state = poll_once(state, fyers)
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e: 
            log.exception("Error during poll: %s", e)
        try:
            momentum_state = run_daily_momentum_scan(momentum_state)
            momentum_state = check_intraday_momentum_triggers(momentum_state, fyers)
            MOMENTUM_STATE_FILE.write_text(json.dumps(momentum_state, indent=2))
        except Exception as e: 
            log.exception("Error during momentum scan: %s", e)
        return

    while True:
        try:
            state = poll_once(state, fyers)
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e: 
            log.exception("Error during poll: %s", e)
        try:
            momentum_state = run_daily_momentum_scan(momentum_state)
            momentum_state = check_intraday_momentum_triggers(momentum_state, fyers)
            MOMENTUM_STATE_FILE.write_text(json.dumps(momentum_state, indent=2))
        except Exception as e: 
            log.exception("Error during momentum scan: %s", e)
        
        time.sleep(POLL_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
