"""
NSE End-of-Day F&O Participant Delivery & Flow Analyzer
================================================================
Runs daily at 8:30 PM.
1. Downloads the official NSE Participant-wise OI CSV.
2. Calculates Net OI for Smart Money (FII + PRO) and Retail (CLIENT).
3. Compares against yesterday's state to calculate daily flow.
4. Translates raw numbers into structural and tactical English descriptions.
5. Dispatches formatted Telegram alert.
"""

import os
import sys
import csv
import json
import logging
import datetime
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
SCRIPT_TAG = "🏦 [fno_delivery_analysis.py]"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

STATE_FILE = Path(__file__).parent / "fno_oi_state.json"
LOG_FILE = Path(__file__).parent / "fno_analysis.log"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("fno_analysis")

# ----------------------------------------------------------------------
# TELEGRAM NOTIFIER
# ----------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.error("Telegram credentials not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
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
# F&O INTERPRETER ENGINE
# ----------------------------------------------------------------------
def interpret_fno_flow(segment: str, net_oi: int, daily_change: int) -> str:
    """Translates raw F&O data into tactical English descriptions."""
    
    # 1. Structural Posture (Net OI Bias)
    if net_oi <= -200000:
        net_desc = "Heavily Short"
    elif net_oi <= -50000:
        net_desc = "Net Short"
    elif net_oi >= 200000:
        net_desc = "Heavily Long"
    elif net_oi >= 50000:
        net_desc = "Net Long"
    else:
        net_desc = "Neutral / Flat"

    # 2. Flow Intensity (Magnitude of Daily Change)
    abs_change = abs(daily_change)
    if abs_change >= 100000:
        intensity = "Aggressive"
    elif abs_change >= 40000:
        intensity = "Heavy"
    elif abs_change >= 15000:
        intensity = "Moderate"
    else:
        intensity = "Mild"

    # 3. Tactical Action (Matching Net Bias vs Daily Flow Direction)
    flow_desc = ""
    if net_oi < 0:  # Net Short
        if daily_change > 0:
            flow_desc = f"{intensity} short-covering"
        else:
            flow_desc = f"{intensity} fresh shorts"
    else:  # Net Long
        if daily_change > 0:
            flow_desc = f"{intensity} fresh longs"
        else:
            flow_desc = f"{intensity} profit booking / unwind"

    return f" • <b>{segment}:</b> {net_oi:,} (<i>{net_desc}</i>) | {daily_change:+,} (<i>{flow_desc}</i>)"

# ----------------------------------------------------------------------
# NSE DATA FETCHING & PARSING
# ----------------------------------------------------------------------
def fetch_nse_participant_oi(date_obj: datetime.date) -> dict:
    """Fetches and parses the NSE Participant-wise OI CSV for a given date."""
    date_str = date_obj.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{date_str}.csv"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"No CSV found for {date_str} (Status: {resp.status_code}). Market may be closed.")
            return None
            
        lines = resp.text.strip().split("\n")
        # Find header row index
        header_idx = -1
        for i, line in enumerate(lines):
            if "Client Type" in line:
                header_idx = i
                break
                
        if header_idx == -1:
            return None
            
        reader = csv.DictReader(lines[header_idx:])
        
        parsed_data = {}
        for row in reader:
            client_type = row.get("Client Type", "").strip().upper()
            if not client_type: continue
            
            # Safely extract and compute net positions
            try:
                idx_fut_net = int(row.get("Future Index Long", 0)) - int(row.get("Future Index Short", 0))
                idx_ce_net = int(row.get("Option Index Call Long", 0)) - int(row.get("Option Index Call Short", 0))
                idx_pe_net = int(row.get("Option Index Put Long", 0)) - int(row.get("Option Index Put Short", 0))
                
                parsed_data[client_type] = {
                    "IDX_FUT": idx_fut_net,
                    "IDX_CE": idx_ce_net,
                    "IDX_PE": idx_pe_net
                }
            except ValueError:
                continue
                
        return parsed_data
    except Exception as e:
        log.error(f"Failed to fetch NSE OI data: {e}")
        return None

# ----------------------------------------------------------------------
# MAIN EXECUTION LOOP
# ----------------------------------------------------------------------
def main():
    log.info("Starting Daily F&O Delivery Analysis...")
    
    # 1. Determine Date (Use today, but if it's weekend, abort or use last Friday if testing)
    today = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    if today.weekday() >= 5:
        log.info("Weekend detected. Exiting.")
        sys.exit(0)
        
    date_obj = today.date()
    
    # 2. Fetch Today's Data
    today_data = fetch_nse_participant_oi(date_obj)
    if not today_data:
        log.info("Today's NSE data not published yet or market holiday.")
        sys.exit(0)
        
    # 3. Consolidate Smart Money (FII + PRO) and Retail (CLIENT)
    try:
        fii = today_data.get("FII", {})
        pro = today_data.get("PRO", {})
        client = today_data.get("CLIENT", {})
        
        smart_money = {
            "IDX_FUT": fii.get("IDX_FUT", 0) + pro.get("IDX_FUT", 0),
            "IDX_CE": fii.get("IDX_CE", 0) + pro.get("IDX_CE", 0),
            "IDX_PE": fii.get("IDX_PE", 0) + pro.get("IDX_PE", 0),
        }
        retail = {
            "IDX_FUT": client.get("IDX_FUT", 0),
            "IDX_CE": client.get("IDX_CE", 0),
            "IDX_PE": client.get("IDX_PE", 0),
        }
    except Exception as e:
        log.error(f"Error consolidating data: {e}")
        sys.exit(1)

    # 4. Load Yesterday's State to calculate Flow
    last_state = {}
    if STATE_FILE.exists():
        try:
            last_state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    last_smart = last_state.get("smart_money", {})
    last_retail = last_state.get("retail", {})

    # 5. Calculate Daily Changes
    flow_smart = {
        "IDX_FUT": smart_money["IDX_FUT"] - last_smart.get("IDX_FUT", smart_money["IDX_FUT"]),
        "IDX_CE": smart_money["IDX_CE"] - last_smart.get("IDX_CE", smart_money["IDX_CE"]),
        "IDX_PE": smart_money["IDX_PE"] - last_smart.get("IDX_PE", smart_money["IDX_PE"]),
    }
    
    flow_retail = {
        "IDX_FUT": retail["IDX_FUT"] - last_retail.get("IDX_FUT", retail["IDX_FUT"]),
        "IDX_CE": retail["IDX_CE"] - last_retail.get("IDX_CE", retail["IDX_CE"]),
        "IDX_PE": retail["IDX_PE"] - last_retail.get("IDX_PE", retail["IDX_PE"]),
    }

    # 6. Generate Telegram Report
    report_lines = [
        f"{SCRIPT_TAG}",
        f"📊 <b>F&O Participant Matrix \u2014 {date_obj.strftime('%d %b %Y')}</b>\n",
        
        "🏛️ <b>Smart Money (FII + PRO):</b>",
        interpret_fno_flow("Index Futures", smart_money["IDX_FUT"], flow_smart["IDX_FUT"]),
        interpret_fno_flow("Index Calls", smart_money["IDX_CE"], flow_smart["IDX_CE"]),
        interpret_fno_flow("Index Puts", smart_money["IDX_PE"], flow_smart["IDX_PE"]),
        "\n",
        
        "🛍️ <b>Retail (CLIENT):</b>",
        interpret_fno_flow("Index Futures", retail["IDX_FUT"], flow_retail["IDX_FUT"]),
        interpret_fno_flow("Index Calls", retail["IDX_CE"], flow_retail["IDX_CE"]),
        interpret_fno_flow("Index Puts", retail["IDX_PE"], flow_retail["IDX_PE"]),
    ]

    final_msg = "\n".join(report_lines)
    
    # 7. Dispatch Alert & Save State
    if send_telegram_message(final_msg):
        log.info("Report dispatched successfully.")
        
        # Only save state if alert was successful so we don't corrupt the baseline
        new_state = {
            "date": date_obj.isoformat(),
            "smart_money": smart_money,
            "retail": retail
        }
        STATE_FILE.write_text(json.dumps(new_state, indent=2))
        log.info("F&O state updated for tomorrow.")
    else:
        log.error("Failed to send report.")

if __name__ == "__main__":
    main()
