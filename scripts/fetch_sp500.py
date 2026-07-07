#!/usr/bin/env python3
"""
Fetch the S&P 500 company list from Wikipedia and match each company
to its SEC CIK number. Saves data/sp500_tickers.json.

Each entry:
  {ticker, name, sector, industry, cik}
"""

import json
import time
from pathlib import Path

import requests
import pandas as pd

BASE   = Path(__file__).resolve().parent.parent
DATA   = BASE / "data"
DATA.mkdir(exist_ok=True)

HEADERS = {"User-Agent": "Eliran Cohen eliran.eliran35@gmail.com"}

def fetch_sp500():
    print("Fetching S&P 500 list from Wikipedia...", flush=True)
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    wiki_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=wiki_headers, timeout=30)
    r.raise_for_status()
    from io import StringIO
    tables = pd.read_html(StringIO(r.text), header=0)
    sp500 = tables[0]
    print(f"  Got {len(sp500)} companies")
    return sp500

def fetch_cik_map():
    ct_path = DATA / "company_tickers.json"
    if ct_path.exists():
        age_h = (time.time() - ct_path.stat().st_mtime) / 3600
        if age_h < 168:
            print("Using cached company_tickers.json", flush=True)
            with ct_path.open() as f:
                raw = json.load(f)
            return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}

    print("Fetching company_tickers.json from SEC...", flush=True)
    r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS, timeout=30)
    r.raise_for_status()
    raw = r.json()
    with ct_path.open("w") as f:
        json.dump(raw, f)
    print(f"  Saved {len(raw):,} entries to data/company_tickers.json")
    return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}

def main():
    sp500 = fetch_sp500()
    cik_map = fetch_cik_map()

    companies = []
    missing = []

    # Determine column names (Wikipedia table headers can vary slightly)
    cols = list(sp500.columns)
    ticker_col   = next((c for c in cols if "symbol" in c.lower() or "ticker" in c.lower()), cols[0])
    name_col     = next((c for c in cols if "security" in c.lower() or "name" in c.lower()), cols[1])
    sector_col   = next((c for c in cols if "gics sector" in c.lower() or "sector" in c.lower()), None)
    industry_col = next((c for c in cols if "sub-industry" in c.lower() or "industry" in c.lower()), None)

    for _, row in sp500.iterrows():
        raw_ticker = str(row[ticker_col]).strip()
        # Wikipedia uses "." for share classes (e.g. BRK.B → BRK-B for SEC)
        ticker = raw_ticker.replace(".", "-")
        name   = str(row[name_col]).strip() if name_col else ""
        sector = str(row[sector_col]).strip() if sector_col else ""
        industry = str(row[industry_col]).strip() if industry_col else ""

        # Try exact match first, then try without hyphen variant
        cik = cik_map.get(ticker.upper()) or cik_map.get(raw_ticker.replace(".","").upper())
        if not cik:
            missing.append(ticker)

        companies.append({
            "ticker":   ticker,
            "name":     name,
            "sector":   sector,
            "industry": industry,
            "cik":      cik,
        })

    out_path = DATA / "sp500_tickers.json"
    with out_path.open("w") as f:
        json.dump(companies, f)

    print(f"\nSaved {len(companies)} companies to data/sp500_tickers.json")
    print(f"  CIK matched: {len(companies) - len(missing)}")
    if missing:
        print(f"  CIK missing ({len(missing)}): {', '.join(missing[:20])}")

    # Quick sanity check
    sample = [c for c in companies if c["ticker"] in ("AAPL","MSFT","JPM","TSLA","NVDA") and c["cik"]]
    for c in sample:
        print(f"  {c['ticker']:6s}  CIK={c['cik']:>10}  {c['name']}")

if __name__ == "__main__":
    main()
