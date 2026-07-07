#!/usr/bin/env python3
"""
SEC EDGAR XBRL data layer.
Fetches and parses annual financial data for any SEC-filing company.

Usage:
  python3 scripts/sec_xbrl.py AAPL
  python3 scripts/sec_xbrl.py MSFT
  python3 scripts/sec_xbrl.py JPM
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE   = Path(__file__).resolve().parent.parent
DATA   = BASE / "data"
CACHE  = DATA / "xbrl_cache"
CACHE.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "Eliran Cohen eliran.eliran35@gmail.com"}
CACHE_HOURS = 24

# ── The 13 tracked banks ──────────────────────────────────────────────────────
BANK_TICKERS = {"JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","STT","BK","SCHW"}

# ── XBRL concepts to pull ──────────────────────────────────────────────────────
INCOME_CONCEPTS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "GrossProfit",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "InterestExpense",
    "IncomeTaxExpense",
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "ResearchAndDevelopmentExpense",
    "SellingGeneralAndAdministrativeExpense",
    "RestructuringCharges",
    "GoodwillImpairmentLoss",
]

BS_CONCEPTS = [
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "StockholdersEquityAttributableToParent",
    "CashAndCashEquivalentsAtCarryingValue",
    "CashAndCashEquivalents",
    "LongTermDebt",
    "LongTermDebtNoncurrent",
    "ShortTermBorrowings",
    "NotesPayableCurrent",
    "LongTermDebtCurrent",
    "AccountsReceivableNetCurrent",
    "InventoryNet",
    "AccountsPayableCurrent",
    "PropertyPlantAndEquipmentNet",
    "CommonStockSharesOutstanding",
]

CF_CONCEPTS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsOfDividends",
    "PaymentsForRepurchaseOfCommonStock",
]

ALL_CONCEPTS = INCOME_CONCEPTS + BS_CONCEPTS + CF_CONCEPTS


# ── Ticker → CIK lookup ────────────────────────────────────────────────────────

def load_ticker_map():
    """Download and cache the SEC company_tickers.json."""
    ct_path = DATA / "company_tickers.json"
    # Use cached version if recent (< 7 days)
    if ct_path.exists():
        age_h = (time.time() - ct_path.stat().st_mtime) / 3600
        if age_h < 168:
            with ct_path.open() as f:
                raw = json.load(f)
            return {v["ticker"].upper(): v["cik_str"] for v in raw.values()}

    print("Downloading company_tickers.json from SEC...", end="", flush=True)
    r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS, timeout=30)
    r.raise_for_status()
    raw = r.json()
    with ct_path.open("w") as f:
        json.dump(raw, f)
    print(f" done ({len(raw):,} entries)")
    return {v["ticker"].upper(): v["cik_str"] for v in raw.values()}


def get_cik(ticker, ticker_map):
    t = ticker.upper()
    cik = ticker_map.get(t)
    if not cik:
        raise ValueError(f"CIK not found for ticker '{t}'. Not in SEC database.")
    return str(cik).zfill(10)


# ── Cache helpers ──────────────────────────────────────────────────────────────

def cache_path(ticker):
    return CACHE / f"{ticker.upper()}.json"


def load_from_cache(ticker):
    p = cache_path(ticker)
    if not p.exists():
        return None
    age_h = (time.time() - p.stat().st_mtime) / 3600
    if age_h > CACHE_HOURS:
        return None
    with p.open() as f:
        return json.load(f)


def save_to_cache(ticker, data):
    with cache_path(ticker).open("w") as f:
        json.dump(data, f)


# ── XBRL fetching & parsing ────────────────────────────────────────────────────

def fetch_company_facts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def get_annual_values(gaap, concept):
    """Extract annual (10-K FY) values for a concept. Returns {year: value_in_USD}."""
    info = gaap.get(concept)
    if not info:
        return {}
    # Try USD units first, then pure (for shares / ratios)
    units = info.get("units", {})
    data  = units.get("USD") or units.get("pure") or []
    if not data:
        return {}

    best = {}
    for d in data:
        if d.get("form") != "10-K":
            continue
        fp = d.get("fp", "")
        # Accept FY or annual designations
        if fp not in ("FY", ""):
            continue
        # Prefer end date year
        end = d.get("end", "")
        if not end or len(end) < 4:
            continue
        yr = int(end[:4])
        filed = d.get("filed", "")
        if yr not in best or filed > best[yr]["filed"]:
            best[yr] = {"value": d.get("val"), "filed": filed}

    return {yr: info["value"] for yr, info in best.items() if info["value"] is not None}


def pick_revenue(gaap, yr):
    """Try multiple revenue concepts in priority order."""
    for c in [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ]:
        vals = get_annual_values(gaap, c)
        if yr in vals:
            return vals[yr]
    return None


def pick_cash(gaap, yr):
    for c in ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"]:
        vals = get_annual_values(gaap, c)
        if yr in vals:
            return vals[yr]
    return None


def pick_equity(gaap, yr):
    for c in ["StockholdersEquity", "StockholdersEquityAttributableToParent"]:
        vals = get_annual_values(gaap, c)
        if yr in vals:
            return vals[yr]
    return None


def pick_da(gaap, yr):
    for c in ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"]:
        vals = get_annual_values(gaap, c)
        if yr in vals:
            return vals[yr]
    return None


def pick_ltdebt(gaap, yr):
    for c in ["LongTermDebt", "LongTermDebtNoncurrent"]:
        vals = get_annual_values(gaap, c)
        if yr in vals:
            return vals[yr]
    return None


def pick_stdebt(gaap, yr):
    for c in ["ShortTermBorrowings", "NotesPayableCurrent", "LongTermDebtCurrent"]:
        vals = get_annual_values(gaap, c)
        if yr in vals:
            return vals[yr]
    return None


def M(v):
    """Convert USD to millions, or return None."""
    return round(v / 1_000_000, 3) if v is not None else None


def safe_div(a, b, pct=False):
    if a is None or b is None or b == 0:
        return None
    return round(a / b * (100 if pct else 1), 4)


def parse_xbrl(facts, ticker):
    """Parse Finnhub facts dict into {year: {metrics...}} with all values in $M."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    if not gaap:
        return {}

    # Pre-fetch all concepts
    concepts = {}
    for c in ALL_CONCEPTS:
        concepts[c] = get_annual_values(gaap, c)

    # Collect all years with any data
    all_years = set()
    for vals in concepts.values():
        all_years.update(vals.keys())
    all_years = sorted(y for y in all_years if y >= 2009)

    result = {}
    for yr in all_years:
        def g(concept): return concepts.get(concept, {}).get(yr)

        rev_raw   = pick_revenue(gaap, yr)
        gp_raw    = g("GrossProfit")
        ebit_raw  = g("OperatingIncomeLoss")
        ni_raw    = g("NetIncomeLoss")
        ie_raw    = g("InterestExpense")
        tax_raw   = g("IncomeTaxExpense")
        da_raw    = pick_da(gaap, yr)
        rnd_raw   = g("ResearchAndDevelopmentExpense")
        sga_raw   = g("SellingGeneralAndAdministrativeExpense")
        rest_raw  = g("RestructuringCharges")
        imp_raw   = g("GoodwillImpairmentLoss")

        assets_raw = g("Assets")
        liab_raw   = g("Liabilities")
        eq_raw     = pick_equity(gaap, yr)
        cash_raw   = pick_cash(gaap, yr)
        ltd_raw    = pick_ltdebt(gaap, yr)
        std_raw    = pick_stdebt(gaap, yr)
        ar_raw     = g("AccountsReceivableNetCurrent")
        inv_raw    = g("InventoryNet")
        ap_raw     = g("AccountsPayableCurrent")
        ppe_raw    = g("PropertyPlantAndEquipmentNet")
        shares_raw = g("CommonStockSharesOutstanding")

        ocf_raw   = g("NetCashProvidedByUsedInOperatingActivities")
        capex_raw = g("PaymentsToAcquirePropertyPlantAndEquipment")
        div_raw   = g("PaymentsOfDividends")
        buyback_raw = g("PaymentsForRepurchaseOfCommonStock")

        # Derived
        total_debt_raw = (ltd_raw or 0) + (std_raw or 0)
        net_debt_raw   = total_debt_raw - (cash_raw or 0)

        # EBITDA: prefer EBIT + D&A; fall back to NI + IE + Tax + D&A
        ebitda_raw = None
        if ebit_raw is not None and da_raw is not None:
            ebitda_raw = ebit_raw + da_raw
        elif ni_raw is not None and ie_raw is not None and tax_raw is not None and da_raw is not None:
            ebitda_raw = ni_raw + ie_raw + tax_raw + da_raw

        fcf_raw = None
        if ocf_raw is not None and capex_raw is not None:
            fcf_raw = ocf_raw - capex_raw

        cogs_raw = None
        if rev_raw is not None and gp_raw is not None:
            cogs_raw = rev_raw - gp_raw

        row = {
            "revenue":             M(rev_raw),
            "gross_profit":        M(gp_raw),
            "gross_margin":        safe_div(gp_raw, rev_raw, pct=True),
            "ebit":                M(ebit_raw),
            "ebitda":              M(ebitda_raw),
            "ebitda_margin":       safe_div(ebitda_raw, rev_raw, pct=True),
            "net_income":          M(ni_raw),
            "net_margin":          safe_div(ni_raw, rev_raw, pct=True),
            "interest_expense":    M(abs(ie_raw) if ie_raw else None),
            "tax_expense":         M(tax_raw),
            "da":                  M(da_raw),
            "sga":                 M(sga_raw),
            "rnd":                 M(rnd_raw),
            "restructuring":       M(rest_raw),
            "impairment":          M(imp_raw),
            "total_assets":        M(assets_raw),
            "liabilities":         M(liab_raw),
            "stockholders_equity": M(eq_raw),
            "cash":                M(cash_raw),
            "lt_debt":             M(ltd_raw),
            "st_debt":             M(std_raw),
            "total_debt":          M(total_debt_raw) if total_debt_raw else None,
            "net_debt":            M(net_debt_raw),
            "net_debt_ebitda":     safe_div(net_debt_raw, ebitda_raw),
            "roe":                 safe_div(ni_raw, eq_raw, pct=True),
            "roa":                 safe_div(ni_raw, assets_raw, pct=True),
            "ar":                  M(ar_raw),
            "inventory":           M(inv_raw),
            "accounts_payable":    M(ap_raw),
            "ppe":                 M(ppe_raw),
            "shares_outstanding":  M(shares_raw),
            "operating_cf":        M(ocf_raw),
            "capex":               M(abs(capex_raw) if capex_raw else None),
            "dividends":           M(div_raw),
            "buybacks":            M(buyback_raw),
            "fcf":                 M(fcf_raw),
            "fcf_margin":          safe_div(fcf_raw, rev_raw, pct=True),
            "fcf_over_ebitda":     safe_div(fcf_raw, ebitda_raw, pct=True),
            "receivable_days":     safe_div(ar_raw, rev_raw) and round(ar_raw / rev_raw * 365, 1) if ar_raw and rev_raw else None,
            "inventory_days":      round(inv_raw / cogs_raw * 365, 1) if inv_raw and cogs_raw else None,
            "payable_days":        round(ap_raw / cogs_raw * 365, 1) if ap_raw and cogs_raw else None,
            "cash_cycle":          None,
        }

        rd = row["receivable_days"]
        id_ = row["inventory_days"]
        pd_ = row["payable_days"]
        if rd is not None and pd_ is not None:
            row["cash_cycle"] = round((rd or 0) + (id_ or 0) - pd_, 1)

        # Only include year if we have at least some meaningful data
        key_fields = ["revenue", "ebitda", "net_income", "total_assets", "operating_cf"]
        if any(row[k] is not None for k in key_fields):
            result[yr] = row

    return result


def merge_bank_csv(data, ticker):
    """For our 13 banks: merge bank_financials.csv data into each year's dict."""
    csv_path = DATA / "processed" / "bank_financials.csv"
    if not csv_path.exists():
        return data
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        bank_rows = df[df["ticker"] == ticker.upper()]
        for _, row in bank_rows.iterrows():
            yr = int(row["fiscal_year"])
            if yr not in data:
                data[yr] = {}
            bank_cols = [c for c in df.columns if c not in ("ticker","fiscal_year","filing_date","accession_number","source_file")]
            for col in bank_cols:
                val = row[col]
                if val is not None and str(val).lower() not in ("nan","none",""):
                    data[yr][col] = float(val) if not isinstance(val, str) else val
    except Exception as e:
        print(f"  [warn] Could not merge bank CSV: {e}")
    return data


# ── Main entry point ───────────────────────────────────────────────────────────

def get_company_data(ticker, force=False):
    """
    Main function: returns {year: {metrics...}} for given ticker.
    Checks cache first; fetches from SEC if stale or missing.
    """
    ticker = ticker.upper()

    if not force:
        cached = load_from_cache(ticker)
        if cached:
            return cached

    print(f"[{ticker}] Loading ticker map...", flush=True)
    ticker_map = load_ticker_map()
    cik = get_cik(ticker, ticker_map)
    print(f"[{ticker}] CIK = {cik}", flush=True)

    print(f"[{ticker}] Fetching XBRL company facts...", flush=True)
    facts = fetch_company_facts(cik)
    company_name = facts.get("entityName", ticker)
    print(f"[{ticker}] Company: {company_name}", flush=True)

    print(f"[{ticker}] Parsing XBRL data...", flush=True)
    data = parse_xbrl(facts, ticker)

    if ticker in BANK_TICKERS:
        print(f"[{ticker}] Merging bank_financials.csv data...", flush=True)
        data = merge_bank_csv(data, ticker)

    save_to_cache(ticker, data)
    print(f"[{ticker}] Cached {len(data)} annual records → {cache_path(ticker)}", flush=True)
    return data


def pretty_print(ticker, data):
    """Print a summary table of key metrics."""
    print(f"\n{'='*70}")
    print(f"  {ticker} — SEC EDGAR Annual Financials ($M)")
    print(f"{'='*70}")
    years = sorted(data.keys())[-7:]  # last 7 years
    cols = ["revenue","ebitda","ebitda_margin","net_income","net_margin","fcf","net_debt","net_debt_ebitda","roe"]
    header = f"{'Year':>5}" + "".join(f"{c:>18}" for c in cols)
    print(header)
    print("-" * len(header))
    for yr in years:
        row = data[yr]
        def fmt(k):
            v = row.get(k)
            if v is None: return "—"
            if "margin" in k or "roe" in k or "roa" in k: return f"{v:.1f}%"
            if "ebitda" == k or "coverage" in k: return f"{v:.1f}"
            return f"{v:,.0f}"
        print(f"{yr:>5}" + "".join(f"{fmt(c):>18}" for c in cols))
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/sec_xbrl.py <TICKER> [--force]")
        sys.exit(1)
    t = sys.argv[1].upper()
    force = "--force" in sys.argv
    data = get_company_data(t, force=force)
    pretty_print(t, data)
