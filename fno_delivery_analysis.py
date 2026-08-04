"""
Daily F&O + Delivery Data Analysis -> Telegram
================================================

Runs ONCE per trading day, after market close (when NSE's daily
Bhavcopy files are finalized, typically available by ~6:30 PM IST).

What this covers
------------------
1. DELIVERY % ANALYSIS (from NSE's equity Bhavcopy)
   Flags stocks with unusually high delivery percentage combined with
   a meaningful price move -- high delivery % suggests genuine
   buying/selling interest rather than pure intraday speculation.

2. LONG/SHORT BUILDUP (from NSE's F&O Bhavcopy, near-month futures)
   Classifies each F&O stock using the standard price-change x
   OI-change matrix:
     - Long Buildup     : price UP   + OI UP    (bullish)
     - Short Buildup     : price DOWN + OI UP    (bearish)
     - Short Covering    : price UP   + OI DOWN  (bullish, closing shorts)
     - Long Unwinding     : price DOWN + OI DOWN  (bearish, closing longs)

3. PCR (Put-Call Ratio) -- computed per stock and for the overall
   market, from the same F&O Bhavcopy's options data (Put OI / Call OI).

4. FII AGGREGATE POSITIONING (best-effort) -- long/short ratio in
   index futures & options from NSE's daily FII derivatives
   statistics. This is the least certain part of this script since
   NSE's exact report format/URL for this specific data point could
   not be verified against live data while building this -- if it
   fails, everything else in the script still runs and sends its
   results; check the logs for "FII fetch failed" if this section
   comes back empty.

Data sources (NSE public archives -- no login required)
----------------------------------------------------------
- Equity Bhavcopy (delivery %):
  https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
- F&O Bhavcopy (OI, price change, PCR):
  https://archives.nseindia.com/content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip
- FII derivatives statistics:
  https://archives.nseindia.com/content/fo/fii_stats_DDMMYYYY.csv (best-effort)

One-time setup
---------------
1. pip install -r requirements_fno.txt
2. Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (same bot as your other
   scripts -- env vars or edit CONFIG below).
3. python3 fno_delivery_analysis.py           (uses today's date)
   python3 fno_delivery_analysis.py --date 04-08-2026   (specific date, for testing)

Config knobs are in the CONFIG section below.
"""

import os
import io
import sys
import csv
import json
import zipfile
import logging
import datetime

import requests
import pandas as pd

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

# Minimum delivery % to flag a stock as a "high delivery interest" signal.
DELIVERY_PCT_THRESHOLD = 60.0
# Minimum absolute price change % (same day) to pair with delivery % above.
DELIVERY_PRICE_MOVE_THRESHOLD = 2.0

# Minimum absolute OI change % to count as a meaningful buildup (filters noise).
OI_CHANGE_THRESHOLD = 5.0

# How many top stocks to show per category in the Telegram summary.
TOP_N = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fno_analysis")


def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram not configured.")
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


def get_session():
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=15)
    except Exception as e:
        log.warning("Could not prime NSE session: %s", e)
    return session


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


def analyze_delivery(df: pd.DataFrame) -> list:
    """Returns list of (symbol, price_change_pct, delivery_pct) for
    stocks with high delivery % AND a meaningful price move."""
    results = []
    try:
        df = df[df["SERIES"].str.strip() == "EQ"]
        for _, row in df.iterrows():
            try:
                symbol = str(row["SYMBOL"]).strip()
                close = float(row["CLOSE_PRICE"])
                prev_close = float(row["PREV_CLOSE"])
                deliv_pct = float(row["DELIV_PER"])
                if prev_close == 0:
                    continue
                price_change_pct = ((close - prev_close) / prev_close) * 100
                if deliv_pct >= DELIVERY_PCT_THRESHOLD and abs(price_change_pct) >= DELIVERY_PRICE_MOVE_THRESHOLD:
                    results.append((symbol, price_change_pct, deliv_pct))
            except (ValueError, KeyError):
                continue
    except Exception as e:
        log.error("Delivery analysis failed: %s", e)
    results.sort(key=lambda x: abs(x[1]), reverse=True)
    return results[:TOP_N]


# ----------------------------------------------------------------------
# 2 & 3. F&O BHAVCOPY -- LONG/SHORT BUILDUP + PCR
# ----------------------------------------------------------------------

def fetch_fo_bhavcopy(session, date: datetime.date) -> pd.DataFrame | None:
    """NSE switched to the UDiFF Bhavcopy format on a new domain in
    July 2024 (NSE Circular 62424) -- old archives.nseindia.com format
    is discontinued. New format uses different column names entirely
    (TckrSymb, ClsPric, OpnIntrst, etc. instead of SYMBOL, CLOSE,
    OPEN_INT)."""
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
    """Returns dict of category -> list of (symbol, price_chg_pct, oi_chg_pct),
    using each stock's near-month stock-futures contract (FinInstrmTp
    == 'STF' in the UDiFF format)."""
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
    """Returns (overall_pcr, list of (symbol, pcr)) using stock-options
    open interest (FinInstrmTp == 'STO', OptnTp == 'CE'/'PE')."""
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
# 4. FII AGGREGATE POSITIONING (best-effort)
# ----------------------------------------------------------------------

def fetch_fii_stats(session, date: datetime.date) -> pd.DataFrame | None:
    """Attempts to fetch FII derivatives long/short stats. This is an
    .xls file (not .csv) at a DD-MMM-YYYY date format. Tries the new
    nsearchives.nseindia.com domain first, falls back to the older
    archives.nseindia.com domain in case this specific report wasn't
    migrated. Returns None on total failure rather than crashing, so
    the rest of the analysis still gets sent."""
    date_str = date.strftime("%d-%b-%Y")
    urls = [
        f"https://nsearchives.nseindia.com/content/fo/fii_stats_{date_str}.xls",
        f"https://archives.nseindia.com/content/fo/fii_stats_{date_str}.xls",
    ]
    for url in urls:
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            # The report has a multi-row header layout (title row, then
            # a two-level column header), which confuses pandas' default
            # single-header-row parsing. Read raw and parse each data
            # row manually instead.
            df = pd.read_excel(io.BytesIO(resp.content), header=None)
            return df
        except Exception as e:
            log.warning("FII stats fetch failed for %s: %s", url, e)
            continue
    return None


def parse_fii_stats(df: pd.DataFrame) -> list:
    """Parses the raw FII stats sheet into a list of dicts, one per
    category row (INDEX FUTURES, STOCK FUTURES, etc.):
    {category, buy_amt, sell_amt, net_amt}. Skips title/header rows
    that don't match the expected 'name + 6 numbers' pattern."""
    import re
    results = []
    for _, row in df.iterrows():
        cells = [str(c).strip() for c in row if pd.notna(c) and str(c).strip()]
        if len(cells) < 7:
            continue
        # Last 6 cells should be numeric: buy_ct, buy_amt, sell_ct, sell_amt, oi_ct, oi_amt
        nums = cells[-6:]
        name_parts = cells[:-6]
        if not name_parts:
            continue
        try:
            nums_clean = [float(n.replace(",", "")) for n in nums]
        except ValueError:
            continue  # header row or non-numeric row, skip
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

def format_stock_list(items, comment=None):
    if not items:
        return "  (none)"
    lines = []
    for entry in items:
        if len(entry) == 3:
            symbol, price_chg, extra = entry
            line = f"  {symbol}: price {price_chg:+.1f}%, OI {extra:+.1f}%"
            if comment:
                # High delivery + price direction -> genuine interest read
                direction = "buying interest" if price_chg > 0 else "selling pressure"
                line += f" (high delivery {direction})"
            lines.append(line)
        else:
            symbol, val = entry
            lines.append(f"  {symbol}: {val}")
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

    # --- Delivery analysis ---
    deliv_df = fetch_delivery_data(session, date)
    if deliv_df is not None:
        deliv_signals = analyze_delivery(deliv_df)
        sections.append(
            f"\U0001F4E6 <b>High Delivery % Signals</b> (>{DELIVERY_PCT_THRESHOLD:.0f}% delivery, "
            f">{DELIVERY_PRICE_MOVE_THRESHOLD:.0f}% move)\n"
            f"<i>High delivery % with a real price move suggests investors are taking/exiting "
            f"positions, not just intraday trading</i>\n" +
            format_stock_list([(s, p, d) for s, p, d in deliv_signals], comment=True)
        )
        log.info("Delivery analysis: %d signals found.", len(deliv_signals))
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

    # --- FII stats (best-effort) ---
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

    # Telegram has a 4096-char message limit -- split if needed
    if len(message) <= 4000:
        send_telegram_message(message)
    else:
        for i in range(0, len(message), 3800):
            send_telegram_message(message[i:i + 3800])

    log.info("Done.")


if __name__ == "__main__":
    main()
