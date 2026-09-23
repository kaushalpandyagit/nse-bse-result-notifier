"""
Daily F&O, Delivery Data, and Ace Investor Analysis -> Telegram
================================================================

Runs ONCE per trading day, after market close (when NSE's daily
Bhavcopy and Bulk/Block deal files are finalized, typically by ~6:30 PM IST).

What this covers
------------------
1. ACE INVESTOR / SMART MONEY DEALS
   Scans the daily NSE Bulk and Block deal feeds for a custom watchlist
   of renowned individuals, institutions, and mutual funds.

2. DELIVERY % ANALYSIS (from NSE's equity Bhavcopy)
   Flags stocks with unusually high delivery percentage combined with
   a meaningful price move.

3. UNUSUAL VOLUME
   Flags stocks trading >=2.5x their average volume with minimal price movement,
   excluding ETFs and stocks that had a Bulk/Block deal that day.

4. LONG/SHORT BUILDUP (from NSE's F&O Bhavcopy, near-month futures)
   Classifies F&O stocks into Long Buildup, Short Buildup, Short Covering,
   and Long Unwinding based on Price and Open Interest changes.

5. PCR (Put-Call Ratio) -- computed per stock and overall market.

6. FII AGGREGATE POSITIONING (best-effort) -- long/short ratio in
   index futures & options.
"""

import os
import io
import re
import sys
import csv
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

# ----------------------------------------------------------------------
# ACE INVESTOR & INSTITUTIONAL WATCHLIST
# ----------------------------------------------------------------------
ACE_INVESTORS = [
    # Individuals
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
    
    # Institutions, Funds & Investment Firms
    "MALABAR INDIA", "ZERODHA BROKING", "AMANSA HOLDINGS", "INDIA EMERGING GIANTS",
    "ENAM INVESTMENT", "MOTILAL OSWAL NIFTY MIDCAP", "MOTILAL OSWAL MIDCAP",
    "SBI MUTUAL FUND", "TATA MUTUAL FUND", "CANARA ROBECO MUTUAL FUND",
    "QUANT MUTUAL FUND", "SUNDARAM MUTUAL FUND", "BANK OF INDIA",
    "AEQUITAS EQUITY", "SIXTH SENSE INDIA", "AUTHUM INVESTMENT",
    "GIRIRAJ STOCK BROKING", "3P INDIA EQUITY", "HEM FINLEASE",
    "ARROW EMERGING OPPORTUNITIES", "MINDPOOL TECHNOLOGIES", "OPALFORCE SOFTWARE",
    "SAGEONE FLAGSHIP", "BANDHAN SMALL CAP", "360 ONE FLEXICAP",
    "360 ONE ASSET", "AIRAN LIMITED", "MINARVA VENTURES", "VINEY EQUITY MARKET"
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
                log.warning("Could not find standard columns in %s deals CSV", report)
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
# 1. DELIVERY % ANALYSIS
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
                volume = float(row["TTL_TRD_QNTY"])
                if prev_close == 0:
                    continue
                price_change_pct = ((close - prev_close) / prev_close) * 100
                if deliv_pct >= DELIVERY_PCT_THRESHOLD and abs(price_change_pct) >= EARLY_MOVE_MIN:
                    all_signals.append((symbol, price_change_pct, deliv_pct, volume))
            except (ValueError, KeyError):
                continue
    except Exception as e:
        log.error("Delivery analysis failed: %s", e)

    early_movers = [s for s in all_signals if EARLY_MOVE_MIN <= abs(s[1]) <= EARLY_MOVE_MAX]
    early_movers.sort(key=lambda x: abs(x[1]), reverse=True)

    return {"early_movers": early_movers[:TOP_N]}


# ----------------------------------------------------------------------
# UNUSUAL VOLUME 
# ----------------------------------------------------------------------

VOLUME_HISTORY_FILE = Path(__file__).parent / "volume_history.json"
VOLUME_HISTORY_DAYS = 20  
UNUSUAL_VOLUME_RATIO = 2.5  
UNUSUAL_VOLUME_PRICE_CAP = 1.5  
MIN_HISTORY_DAYS = 5  

def load_volume_history() -> dict:
    if VOLUME_HISTORY_FILE.exists():
        try:
            return json.loads(VOLUME_HISTORY_FILE.read_text())
        except Exception:
            log.warning("Could not parse volume history, starting fresh.")
    return {}

def save_volume_history(history: dict):
    VOLUME_HISTORY_FILE.write_text(json.dumps(history))

ETF_LIST_CACHE_FILE = Path(__file__).parent / "etf_symbols.json"
ETF_LIST_URL = "https://archives.nseindia.com/content/equities/eq_etfseclist.csv"

def get_etf_symbols(session) -> set:
    if ETF_LIST_CACHE_FILE.exists():
        try:
            cached = json.loads(ETF_LIST_CACHE_FILE.read_text())
            fetched_at = datetime.datetime.fromisoformat(cached["fetched_at"])
            if (datetime.datetime.now() - fetched_at).days < 7:
                return set(cached["symbols"])
        except Exception:
            pass 

    try:
        resp = session.get(ETF_LIST_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]
        symbol_col = next((c for c in df.columns if "Symbol" in c), df.columns[0])
        symbols = {str(s).strip().upper() for s in df[symbol_col] if str(s).strip()}
        ETF_LIST_CACHE_FILE.write_text(json.dumps({
            "fetched_at": datetime.datetime.now().isoformat(),
            "symbols": sorted(symbols),
        }))
        log.info("Refreshed ETF list: %d symbols.", len(symbols))
        return symbols
    except Exception as e:
        log.warning("Could not fetch ETF list (%s). Unusual-volume signal will not exclude ETFs this run.", e)
        return set()

def analyze_unusual_volume(df: pd.DataFrame, session, date: datetime.date, bulk_block_symbols: set) -> list:
    history = load_volume_history()
    etfs = get_etf_symbols(session)
    results = []

    try:
        df = df[df["SERIES"].str.strip() == "EQ"]
        today_str = date.isoformat()
        for _, row in df.iterrows():
            try:
                symbol = str(row["SYMBOL"]).strip()
                close = float(row["CLOSE_PRICE"])
                prev_close = float(row["PREV_CLOSE"])
                volume = float(row["TTL_TRD_QNTY"])
                if prev_close == 0:
                    continue
                price_change_pct = ((close - prev_close) / prev_close) * 100

                sym_hist = history.get(symbol, {})
                past_volumes = [v for d, v in sym_hist.items() if d != today_str]

                sym_hist[today_str] = volume
                if len(sym_hist) > VOLUME_HISTORY_DAYS:
                    oldest = sorted(sym_hist.keys())[0]
                    del sym_hist[oldest]
                history[symbol] = sym_hist

                if len(past_volumes) < MIN_HISTORY_DAYS:
                    continue
                if symbol in bulk_block_symbols:
                    continue
                if symbol in etfs:
                    continue

                avg_volume = sum(past_volumes) / len(past_volumes)
                if avg_volume <= 0:
                    continue
                volume_ratio = volume / avg_volume

                if volume_ratio >= UNUSUAL_VOLUME_RATIO and abs(price_change_pct) <= UNUSUAL_VOLUME_PRICE_CAP:
                    results.append((symbol, price_change_pct, volume_ratio))
            except (ValueError, KeyError):
                continue
    except Exception as e:
        log.error("Unusual volume analysis failed: %s", e)

    save_volume_history(history)
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:TOP_N]


# ----------------------------------------------------------------------
# F&O BHAVCOPY -- LONG/SHORT BUILDUP + PCR
# ----------------------------------------------------------------------

def fetch_fo_bhavcopy(session, date: datetime.date) -> pd.DataFrame | None:
    date_str = date.strftime("%Y%m%d")
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip"
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            csv_name = z.namelist()[0]
            with z.open(csv_name) as f:
                df = pd.read_csv(f)
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception as e:
        log.error("F&O Bhavcopy fetch failed for %s: %s", date, e)
        return None

def analyze_long_short_buildup(df: pd.DataFrame) -> dict:
    categories = {"Long Buildup": [], "Short Buildup": [], "Short Covering": [], "Long Unwinding": []}
    try:
        fut = df[df["FinInstrmTp"].str.strip() == "STF"].copy()
        fut["XpryDt"] = pd.to_datetime(fut["XpryDt"], errors="coerce")
        fut = fut.sort_values("XpryDt").groupby("TckrSymb").first().reset_index()

        for _, row in fut.iterrows():
            try:
                symbol = str(row["TckrSymb"]).strip()
                close = float(row["ClsPric"])
                prev_close = float(row["PrvsClsgPric"])
                oi = float(row["OpnIntrst"])
                chg_oi = float(row["ChngInOpnIntrst"])
                if prev_close == 0 or (oi - chg_oi) == 0:
                    continue
                price_chg_pct = ((close - prev_close) / prev_close) * 100
                oi_chg_pct = (chg_oi / (oi - chg_oi)) * 100

                if abs(oi_chg_pct) < OI_CHANGE_THRESHOLD:
                    continue

                if price_chg_pct > 0 and oi_chg_pct > 0:
                    categories["Long Buildup"].append((symbol, price_chg_pct, oi_chg_pct))
                elif price_chg_pct < 0 and oi_chg_pct > 0:
                    categories["Short Buildup"].append((symbol, price_chg_pct, oi_chg_pct))
                elif price_chg_pct > 0 and oi_chg_pct < 0:
                    categories["Short Covering"].append((symbol, price_chg_pct, oi_chg_pct))
                elif price_chg_pct < 0 and oi_chg_pct < 0:
                    categories["Long Unwinding"].append((symbol, price_chg_pct, oi_chg_pct))
            except (ValueError, KeyError, TypeError):
                continue
    except Exception as e:
        log.error("Long/Short buildup analysis failed: %s", e)

    for cat in categories:
        categories[cat].sort(key=lambda x: abs(x[2]), reverse=True)
        categories[cat] = categories[cat][:TOP_N]

    return categories

def analyze_pcr(df: pd.DataFrame) -> tuple:
    per_stock_pcr = []
    try:
        opts = df[df["FinInstrmTp"].str.strip() == "STO"].copy()
        grouped = opts.groupby(["TckrSymb", "OptnTp"])["OpnIntrst"].sum().unstack(fill_value=0)
        total_ce = grouped["CE"].sum() if "CE" in grouped else 0
        total_pe = grouped["PE"].sum() if "PE" in grouped else 0
        overall_pcr = round(total_pe / total_ce, 2) if total_ce else None

        for symbol, row in grouped.iterrows():
            ce = row.get("CE", 0)
            pe = row.get("PE", 0)
            if ce > 0:
                per_stock_pcr.append((symbol, round(pe / ce, 2)))
    except Exception as e:
        log.error("PCR analysis failed: %s", e)
        return None, []

    return overall_pcr, per_stock_pcr


# ----------------------------------------------------------------------
# FII AGGREGATE POSITIONING
# ----------------------------------------------------------------------

def fetch_fii_stats(session, date: datetime.date) -> pd.DataFrame | None:
    date_str = date.strftime("%d-%b-%Y")
    urls = [
        f"https://nsearchives.nseindia.com/content/fo/fii_stats_{date_str}.xls",
        f"https://archives.nseindia.com/content/fo/fii_stats_{date_str}.xls",
    ]
    for url in urls:
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            df = pd.read_excel(io.BytesIO(resp.content), header=None)
            return df
        except Exception as e:
            log.warning("FII stats fetch failed for %s: %s", url, e)
            continue
    return None

def parse_fii_stats(df: pd.DataFrame) -> list:
    results = []
    for _, row in df.iterrows():
        cells = [str(c).strip() for c in row if pd.notna(c) and str(c).strip()]
        if len(cells) < 7:
            continue
        nums = cells[-6:]
        name_parts = cells[:-6]
        if not name_parts:
            continue
        try:
            nums_clean = [float(n.replace(",", "")) for n in nums]
        except ValueError:
            continue 
        category = " ".join(name_parts)
        buy_amt, sell_amt = nums_clean[1], nums_clean[3]
        results.append({
            "category": category,
            "buy_amt": buy_amt,
            "sell_amt": sell_amt,
            "net_amt": round(buy_amt - sell_amt, 2),
        })
    return results


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def format_stock_list(items, comment=None, third_label="OI"):
    if not items:
        return "  (none)"
    lines = []
    for entry in items:
        if len(entry) == 3:
            symbol, price_chg, extra = entry
            line = f"  {symbol}: price {price_chg:+.1f}%, {third_label} {extra:+.1f}%"
            if comment:
                direction = "buying interest" if price_chg > 0 else "selling pressure"
                line += f" (high delivery {direction})"
            lines.append(line)
        else:
            symbol, val = entry
            lines.append(f"  {symbol}: {val}x avg volume")
    return "\n".join(lines)


def main():
    date = datetime.date.today()
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        if idx + 1 < len(sys.argv):
            date = datetime.datetime.strptime(sys.argv[idx + 1], "%d-%m-%Y").date()

    log.info("Running F&O + Delivery analysis for %s", date)
    session = get_session()

    sections = []
    
    # --- Bulk & Block Deal Analysis ---
    bulk_block_symbols, ace_deals = fetch_bulk_block_deals(session)
    if ace_deals:
        lines = [
            "💎 <b>Smart Money / Ace Investor Deals</b>", 
            "<i>Bulk & Block deals flagged today</i>"
        ]
        for deal in ace_deals:
            action_color = "🟢 BUY" if deal['type'] == 'BUY' else "🔴 SELL"
            qty_fmt = f"{int(deal['qty']):,}"
            val_cr = (deal['qty'] * deal['price']) / 10000000
            
            lines.append(f"  {action_color} <b>{deal['symbol']}</b>: {deal['client']} "
                         f"({qty_fmt} shrs @ ₹{deal['price']:.2f}) \u2014 <b>₹{val_cr:.2f} Cr</b> [{deal['deal_type']}]")
        
        sections.append("\n".join(lines))
        log.info("Found %d smart money deals.", len(ace_deals))

    # --- Delivery analysis (early movers only) ---
    deliv_df = fetch_delivery_data(session, date)
    if deliv_df is not None:
        tiers = analyze_delivery(deliv_df)
        sections.append(
            f"\U0001F331 <b>High Delivery % \u2014 Early Movers</b> "
            f"(>{DELIVERY_PCT_THRESHOLD:.0f}% delivery, {EARLY_MOVE_MIN:.1f}-{EARLY_MOVE_MAX:.1f}% move)\n"
            f"<i>Kept tight to moves still early \u2014 beyond {EARLY_MOVE_MAX:.1f}% the move is "
            f"largely played out and risk/reward for a fresh entry is no longer favorable</i>\n" +
            format_stock_list([(s, p, d) for s, p, d, v in tiers["early_movers"]], comment=True, third_label="Delivery")
        )
        log.info("Delivery analysis: %d early movers.", len(tiers["early_movers"]))

        # --- Unusual volume ---
        unusual = analyze_unusual_volume(deliv_df, session, date, bulk_block_symbols)
        sections.append(
            f"\U0001F50D <b>Unusual Volume, Minimal Price Move</b> (\u2265{UNUSUAL_VOLUME_RATIO:.1f}x avg volume, "
            f"\u2264{UNUSUAL_VOLUME_PRICE_CAP:.1f}% move, bulk/block deals + ETFs excluded)\n"
            f"<i>High volume without a price move can signal quiet accumulation or distribution "
            f"before the move shows up in price</i>\n" +
            format_stock_list([(s, round(v, 1)) for s, p, v in unusual])
        )
        log.info("Unusual volume signals: %d found.", len(unusual))
    else:
        sections.append("\U0001F4E6 <b>Delivery analysis unavailable</b> (data fetch failed -- see logs)")

    # --- F&O buildup + PCR ---
    fo_df = fetch_fo_bhavcopy(session, date)
    if fo_df is not None:
        buildup = analyze_long_short_buildup(fo_df)
        buildup_notes = {
            "Long Buildup": "Price + OI both rising \u2014 commonly read as fresh long positioning (bullish)",
            "Short Buildup": "Price falling + OI rising \u2014 commonly read as fresh short positioning (bearish)",
            "Short Covering": "Price rising + OI falling \u2014 shorts being closed out (bullish reversal)",
            "Long Unwinding": "Price falling + OI falling \u2014 longs being closed out (bearish reversal)",
        }
        for cat, emoji in [("Long Buildup", "\U0001F7E2"), ("Short Buildup", "\U0001F534"),
                             ("Short Covering", "\U0001F7E1"), ("Long Unwinding", "\U0001F7E0")]:
            sections.append(
                f"{emoji} <b>{cat}</b>\n<i>{buildup_notes[cat]}</i>\n" + format_stock_list(buildup[cat])
            )
        log.info("Long/Short buildup: %s", {k: len(v) for k, v in buildup.items()})

        overall_pcr, stock_pcr = analyze_pcr(fo_df)
        stock_pcr.sort(key=lambda x: x[1], reverse=True)
        pcr_line = f"Overall Market PCR: {overall_pcr}\n" if overall_pcr else ""
        sections.append(
            f"\U0001F4CA <b>Put-Call Ratio</b>\n{pcr_line}"
            f"<i>PCR &gt;1 = more Put OI than Call OI (heavier downside hedging/bets); "
            f"PCR &lt;1 = more Call OI (heavier upside bets). Many traders read extremes as "
            f"contrarian -- very high PCR is sometimes viewed as oversold, very low as overbought.</i>\n"
            f"Highest PCR:\n" + format_stock_list(stock_pcr[:5]) +
            f"\nLowest PCR:\n" + format_stock_list(stock_pcr[-5:])
        )
        log.info("PCR analysis: overall=%s, %d stocks", overall_pcr, len(stock_pcr))
    else:
        sections.append("\U0001F4CA <b>F&O buildup/PCR unavailable</b> (data fetch failed -- see logs)")

    # --- FII stats ---
    fii_df = fetch_fii_stats(session, date)
    if fii_df is not None:
        fii_rows = parse_fii_stats(fii_df)
        if fii_rows:
            lines = [
                "\U0001F3E6 <b>FII Derivatives Stats</b> (Net = Buy \u2212 Sell, \u20b9 Cr)",
                "<i>Net positive = FII net buyers in that category (bullish tilt); "
                "net negative = FII net sellers (bearish tilt)</i>",
            ]
            for row in fii_rows:
                tilt = "bullish tilt" if row["net_amt"] > 0 else "bearish tilt" if row["net_amt"] < 0 else "neutral"
                lines.append(f"  {row['category']}: Net {row['net_amt']:+.1f} Cr ({tilt})")
            sections.append("\n".join(lines))
            log.info("FII stats parsed: %d categories.", len(fii_rows))
        else:
            sections.append("\U0001F3E6 <b>FII stats fetched but could not be parsed</b> -- see logs")
            log.warning("FII stats dataframe fetched but parse_fii_stats found no valid rows.")
    else:
        sections.append("\U0001F3E6 <b>FII stats unavailable</b> (best-effort source -- see logs)")

    message = f"\U0001F4C8 <b>F&O + Delivery Analysis \u2014 {date.strftime('%d %b %Y')}</b>\n\n" + "\n\n".join(sections)

    send_email(
        subject=f"F&O + Delivery Analysis \u2014 {date.strftime('%d %b %Y')}",
        body=strip_html_tags(message),
    )

    if len(message) <= 4000:
        send_telegram_message(message)
    else:
        for i in range(0, len(message), 3800):
            send_telegram_message(message[i:i + 3800])

    log.info("Done.")


if __name__ == "__main__":
    now = datetime.datetime.now(IST) if IST else datetime.datetime.now()
    if "--date" in sys.argv or now.weekday() < 5: 
        main()
    else:
        logging.info("Weekend detected. Skipping execution.")
