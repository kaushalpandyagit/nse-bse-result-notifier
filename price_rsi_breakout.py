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
- Uncapped Result-Day RSI recording for ALL companies > 50 Cr Market Cap.
- SETUP 1: 60-Day Major Structural Sweep & RSI Retest (Wyckoff Spring).
- SETUP 2: Momentum Ignition (1% to 2.5% below Macro High).
- SETUP 3: Wyckoff SOS + Range Shift Pullback Curl (Electrosteel Type).
- EoD Sector RSI Calculation & Persistent Local Sector Cache.
- NSE-BSE Delivery Arbitrage & Spread Scanner (Expanded Holding Co. List).
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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

GOOGLE_SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTicCnhvOb2njwMTaCp4oEnOv3LONbfE796ZVTtvPPA_uRN9C2lNeWXL813jxiW_n7zxf1-4HBG_c1G/pub?output=csv"

LOGIN_ID = "KX26944"
APP_ID = "0WC8A1Z15T-100"
SECRET_KEY = "AANC8ZMTAX"
REDIRECT_URI = "http://127.0.0.1:5000"

CLIENT_ID = APP_ID if APP_ID.endswith("-100") else f"{APP_ID}-100"
CACHE_DIR = os.path.expanduser("~/fyers_trading")
TOKEN_FILE = os.path.join(CACHE_DIR, ".fyers_token.txt")

POLL_INTERVAL_MINUTES = 15
ALERT_COOLDOWN_MINUTES = 45
MIN_PRICE_CHANGE_FOR_REPEAT_PCT = 1.0

RSI_PERIOD = 14
TRACK_WINDOW_DAYS = 15

# Momentum Universe Filters
MIN_MARKET_CAP_CR = 300.0
MAX_MARKET_CAP_CR = 31000.0
MAX_WEEKLY_RSI = 57.0

MIN_RSI_RECORDING_MCAP_CR = 50.0
MACRO_PIVOT_LOOKBACK_DAYS = 60

# --- ARBITRAGE SCANNER CONFIG ---
ARBITRAGE_MIN_SPREAD_PCT = 2.5
DEFAULT_ARBITRAGE_CANDIDATES = [
    "BENGALASM", "KAMAHOLD", "JSWHL", "PILANIINVS", "STEL", "BAJAJHLDNG", 
    "BBTC", "DHUNINV", "GANGESSECU", "GFLLIMITED", "JINDALPHOT", "JPOLYINVST", 
    "KICL", "MAHSCOOTER", "NSIL", "RANEHOLDIN", "RPSGVENT", "SILINV", 
    "SUMMITSEC", "TATACAP", "TATAINVEST", "TSFINV", "TVSHLTD", "VHL", 
    "ADANIENT", "WELINV"
]

RESULT_KEYWORDS = [
    "financial result", "financial results", "quarterly result",
    "quarterly results", "un-audited", "unaudited", "audited financial", 
    "results for the quarter", "results for the year", 
    "regulation 33", "reg. 33", "reg 33",
    "standalone and consolidated financial"
]

STATE_FILE = Path(__file__).parent / "breakout_state.json"
NIFTY500_CACHE_FILE = Path(__file__).parent / "nifty500_symbols.json"
LOG_FILE = Path(__file__).parent / "breakout_notifier.log"
MOMENTUM_STATE_FILE = Path(__file__).parent / "momentum_state.json"
SECTOR_CACHE_FILE = Path(__file__).parent / "stock_sectors.json"

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

SECTOR_BENCHMARKS = {
    "^NSEI": "Nifty 50", "^CRSLDX": "Nifty 500", "^NSEBANK": "Bank", 
    "^CNXIT": "IT", "^CNXAUTO": "Auto", "^CNXPHARMA": "Pharma", 
    "^CNXMETAL": "Metal", "^CNXFMCG": "FMCG", "^CNXENERGY": "Energy", 
    "^CNXREALTY": "Realty", "^CNXINFRA": "Infra", "^CNXMEDIA": "Media"
}

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
# LOCAL SECTOR CACHE
# ----------------------------------------------------------------------
def get_cached_sector(symbol: str) -> str:
    cache = {}
    if SECTOR_CACHE_FILE.exists():
        try:
            cache = json.loads(SECTOR_CACHE_FILE.read_text())
        except Exception:
            pass
            
    if symbol in cache:
        return cache[symbol]
        
    try:
        sector = yf.Ticker(f"{symbol}.NS").info.get("sector", "Unknown")
    except Exception:
        sector = "Unknown"
        
    cache[symbol] = sector
    try:
        SECTOR_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.error("Could not write to sector cache: %s", e)
        
    return sector

# ----------------------------------------------------------------------
# FYERS AUTHENTICATION
# ----------------------------------------------------------------------

def get_fyers_access_token():
    if not fyersModel: return None
        
    os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(TOKEN_FILE):
        mtime = os.path.getmtime(TOKEN_FILE)
        if time.strftime("%Y-%m-%d", time.localtime(mtime)) == time.strftime("%Y-%m-%d"):
            with open(TOKEN_FILE, "r") as f:
                token = f.read().strip()
                if token: return token

    totp_key = os.environ.get("FYERS_TOTP_KEY")
    pin = os.environ.get("FYERS_PIN")
    
    if totp_key and pin and pyotp:
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
                return final_token
        except Exception:
            pass

    if not sys.stdin.isatty(): return None
    session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
    auth_url = session.generate_authcode()
    print(f"\n👉  {auth_url}\n")
    user_input = input("Paste redirected URL / auth_code: ").strip()

    auth_code = user_input
    if "auth_code=" in user_input:
        try:
            parsed_url = urlparse(user_input)
            query_params = parse_qs(parsed_url.query)
            if "auth_code" in query_params:
                auth_code = query_params["auth_code"][0]
        except Exception:
            match = re.search(r"auth_code=([^&]+)", user_input)
            if match: auth_code = match.group(1)

    session.set_token(auth_code)
    response = session.generate_token()
    if response.get("s") == "ok" and "access_token" in response:
        access_token = response["access_token"]
        with open(TOKEN_FILE, "w") as f: f.write(access_token)
        return access_token
    return None

# ----------------------------------------------------------------------
# TECHNICAL MATH & LIVE ENGINE
# ----------------------------------------------------------------------

def get_ist_now():
    if IST: return datetime.datetime.now(IST)
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)

def is_market_hours_now() -> bool:
    now = get_ist_now()
    if now.weekday() >= 5: return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end

def get_rsi_series(close_prices_series, period=14):
    delta = close_prices_series.diff()
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
    return rsi

def rsi_fyers_tradingview(close_prices, period=RSI_PERIOD):
    if len(close_prices) < period + 1: return None
    df = pd.DataFrame({"close": close_prices})
    rsi_series = get_rsi_series(df["close"], period)
    rsi_series.iloc[:period] = np.nan
    latest_rsi = rsi_series.iloc[-1]
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
                        live_price, live_open, live_vol, live_high, live_low = float(v["lp"]), float(v["open_price"]), float(v["volume"]), float(v["high_price"]), float(v["low_price"])
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
                today, yesterday = df.iloc[-1], df.iloc[-2]
                day3_ago = df.iloc[-4] if len(df) >= 4 else yesterday
                weekly_closes = closes.resample('W-FRI').last().dropna()
                weekly_rsi = rsi_fyers_tradingview(weekly_closes.tolist(), RSI_PERIOD) if len(weekly_closes) > 14 else 50.0

                price, open_price, prev_close = float(today["Close"]), float(today["Open"]), float(yesterday["Close"])

                return {
                    "price": price, "volume": float(today["Volume"]), "prev_close": prev_close,
                    "prev_volume": float(yesterday["Volume"]), "close_3d_ago": float(day3_ago["Close"]),
                    "rsi": rsi_fyers_tradingview(closes.tolist(), RSI_PERIOD), "weekly_rsi": weekly_rsi,
                    "open_price": open_price, "high_price": float(today["High"]), "low_price": float(today["Low"]),
                    "open_change_rs": price - open_price, "open_change_pct": ((price - open_price) / open_price * 100) if open_price else 0.0,
                    "day_change_rs": price - prev_close, "day_change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0.0,
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

    yahoo_ticker = f"{symbol}.NS" if exchange.upper() == "NSE" else f"{symbol}.BO"
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="1y", interval="1d")
        if hist.empty or len(hist) < 5: return None
        closes = hist["Close"]
        today, yesterday = hist.iloc[-1], hist.iloc[-2]
        day3_ago = hist.iloc[-4] if len(hist) >= 4 else yesterday
        weekly_closes = closes.resample('W-FRI').last().dropna()
        weekly_rsi = rsi_fyers_tradingview(weekly_closes.tolist(), RSI_PERIOD) if len(weekly_closes) > 14 else 50.0

        price, open_price, prev_close = float(today["Close"]), float(today["Open"]), float(yesterday["Close"])

        return {
            "price": price, "volume": float(today["Volume"]), "prev_close": prev_close,
            "prev_volume": float(yesterday["Volume"]), "close_3d_ago": float(day3_ago["Close"]),
            "rsi": rsi_fyers_tradingview(closes.tolist(), RSI_PERIOD), "weekly_rsi": weekly_rsi,
            "open_price": open_price, "high_price": float(today["High"]), "low_price": float(today["Low"]),
            "open_change_rs": price - open_price, "open_change_pct": ((price - open_price) / open_price * 100) if open_price else 0.0,
            "day_change_rs": price - prev_close, "day_change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0.0,
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
    except Exception:
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
                    return {
                        "day_high": float(df.loc[idx, "High"]), "day_low": float(df.loc[idx, "Low"]),
                        "baseline_rsi": rsi_fyers_tradingview(closes_up_to.tolist(), RSI_PERIOD), "actual_date": idx,
                    }

    yahoo_ticker = f"{symbol}.NS" if exchange.upper() == "NSE" else f"{symbol}.BO"
    try:
        hist = yf.Ticker(yahoo_ticker).history(start=date - datetime.timedelta(days=365), end=date + datetime.timedelta(days=7))
        if hist.empty: return None

        hist_dates = [d.date() if hasattr(d, "date") else d for d in hist.index]
        idx_dt = next((d for d in hist_dates if d >= date), None)
        if idx_dt:
            idx = hist_dates.index(idx_dt)
            closes_up_to = hist["Close"].iloc[: idx + 1]
            return {
                "day_high": float(hist["High"].iloc[idx]), "day_low": float(hist["Low"].iloc[idx]),
                "baseline_rsi": rsi_fyers_tradingview(closes_up_to.tolist(), RSI_PERIOD), "actual_date": hist_dates[idx],
            }
    except Exception:
        pass
    return None
