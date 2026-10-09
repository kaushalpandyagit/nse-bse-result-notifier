import os
import re
import json
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# Direct download link for your specific Google Drive file
DRIVE_URL = "https://drive.google.com/uc?export=download&id=1dsA355qOL5Vc9Uz0vlfTiwQKDYtHtLKY"
LOCAL_FILE = "master_sector_scan.xlsx"
OUTPUT_FILE = "custom_indices.json"

def get_ticker(name):
    # Strip out parentheses and extra descriptors for cleaner searching
    clean_name = re.sub(r'\(.*?\)', '', name).strip()
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={clean_name}&quotesCount=3"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=5).json()
        
        for quote in resp.get('quotes', []):
            sym = quote.get('symbol', '')
            if sym.endswith('.NS') or sym.endswith('.BO'):
                return sym
    except Exception as e:
        print(f"   [!] Failed to search ticker for {clean_name}: {e}")
    return None

def build_custom_indices():
    print("[*] Downloading latest Master Sector Scan from Google Drive...")
    resp = requests.get(DRIVE_URL)
    with open(LOCAL_FILE, "wb") as f:
        f.write(resp.content)
    print("[+] Download complete.")

    df = pd.read_excel(LOCAL_FILE, sheet_name=0, usecols=["Industry Group", "Key Listed Stocks"])
    df = df.dropna()

    custom_indices = {}

    print(f"[*] Processing {len(df)} custom sectors...")
    for idx, row in df.iterrows():
        sector_name = str(row['Industry Group']).strip()
        stocks_raw = str(row['Key Listed Stocks']).split(',')
        
        # Format the sector name to be a clean ticker (e.g., "Private Sector Bank" -> "CI_PRIVATE_SECTOR_BANK")
        sector_ticker = "CI_" + re.sub(r'[^A-Z0-9]+', '_', sector_name.upper()).strip('_')
        
        print(f"\n=> Building Index: {sector_ticker} ({sector_name})")
        
        valid_series = {}
        for stock in stocks_raw:
            stock = stock.strip()
            if not stock: continue
            
            ticker = get_ticker(stock)
            if ticker:
                print(f"   -> Found {stock} as {ticker}")
                try:
                    # Fetch 3 years of daily close data
                    hist = yf.Ticker(ticker).history(period="3y", interval="1d")
                    if not hist.empty:
                        # Convert to timezone-naive dates to align cleanly
                        hist.index = pd.to_datetime(hist.index).tz_localize(None).normalize()
                        valid_series[ticker] = hist['Close']
                except Exception as e:
                    print(f"   [!] Data fetch failed for {ticker}: {e}")
            else:
                print(f"   [!] Could not resolve ticker for: {stock}")

        if not valid_series:
            print(f"   [X] Skipping {sector_ticker}: No valid data found.")
            continue

        # Mathematical Blend: Equal-Weighted Index Construction
        # 1. Combine all price histories into one dataframe and forward-fill missing days
        sector_df = pd.concat(valid_series, axis=1).ffill().bfill()
        
        # 2. Normalize each stock to a base value of 100 on day 1
        normalized_df = (sector_df / sector_df.iloc[0]) * 100
        
        # 3. Take the mean across all normalized stocks for each trading day
        index_series = normalized_df.mean(axis=1)

        # Convert the series into the exact dictionary format our screener expects
        history_list = []
        for date_val, price in index_series.items():
            if pd.notna(price):
                history_list.append({
                    "time": date_val.strftime("%Y-%m-%d"),
                    "close": round(price, 2)
                })

        custom_indices[sector_ticker] = {
            "name": sector_name,
            "components": list(valid_series.keys()),
            "history": history_list
        }
        
        print(f"   [+] Successfully built {sector_ticker} with {len(history_list)} data points.")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(custom_indices, f)
    
    print(f"\n[+] SUCCESS! All custom indices saved to {OUTPUT_FILE}.")
    print("[*] Your frontend and python scanners can now read these flawless benchmarks.")

if __name__ == "__main__":
    build_custom_indices()