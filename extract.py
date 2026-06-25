#!/usr/bin/env python3
"""
extract.py

Extract financial data for all S&P 500 companies from SEC EDGAR
companyfacts JSON files and load it into a SQLite database.

Pipeline:
  1. Download the current S&P 500 constituents list from Wikipedia.
  2. Build a ticker -> CIK lookup from tickers.json.
  3. For each company, open its companyfacts file and pull 10-K (annual)
     facts from fiscal_year 2010 onward, deduplicated per fiscal year.
  4. Extract a fixed set of us-gaap / dei tags (with fallbacks).
  5. Compute derived metrics (EBITDA, FreeCashFlow, margins, ...).
  6. Save everything into financials.db (companies + financials tables).
  7. Print an AAPL validation table.

NOTE ON UNITS: SEC XBRL stores values in *full dollars*
(e.g. Apple FY2022 revenue = 394,328,000,000). The spec's "expected"
column is written in thousands (394,328,000) i.e. 1000x smaller. This
script stores the raw SEC values unchanged; the validation table prints
both so the unit convention is visible rather than silently rescaled.
"""

import os
import json
import math
import sqlite3
from datetime import datetime

import requests
import pandas as pd
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Environment / credentials (loaded from .env via python-dotenv)
# --------------------------------------------------------------------------
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
HOME = os.path.expanduser("~")
COMPANYFACTS_DIR = os.path.join(HOME, "Desktop", "companyfacts")
TICKERS_FILE = os.path.join(HOME, "Desktop", "tickers.json")
PROJECT_DIR = os.path.join(HOME, "Desktop", "finance-project")
DB_PATH = os.path.join(PROJECT_DIR, "financials.db")

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
MIN_FISCAL_YEAR = 2010

# --------------------------------------------------------------------------
# Tag configuration
# --------------------------------------------------------------------------
# Each entry: (stored_metric_name, namespace, [primary_tag, *fallback_tags])
# The first tag that has a value for a given fiscal year wins; fallbacks
# only fill fiscal years the primary tag does not cover.
TAG_CONFIG = [
    # ---- INCOME STATEMENT ----
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap",
        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]),
    ("CostOfGoodsAndServicesSold", "us-gaap", ["CostOfGoodsAndServicesSold"]),
    ("GrossProfit", "us-gaap", ["GrossProfit"]),
    ("OperatingExpenses", "us-gaap", ["OperatingExpenses"]),
    ("OperatingIncomeLoss", "us-gaap", ["OperatingIncomeLoss"]),
    ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap",
        ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"]),
    ("IncomeTaxExpenseBenefit", "us-gaap", ["IncomeTaxExpenseBenefit"]),
    ("NetIncomeLoss", "us-gaap", ["NetIncomeLoss"]),
    ("EarningsPerShareBasic", "us-gaap", ["EarningsPerShareBasic"]),
    ("EarningsPerShareDiluted", "us-gaap", ["EarningsPerShareDiluted"]),
    ("WeightedAverageNumberOfSharesOutstandingBasic", "us-gaap",
        ["WeightedAverageNumberOfSharesOutstandingBasic"]),
    ("WeightedAverageNumberOfDilutedSharesOutstanding", "us-gaap",
        ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    ("InterestExpense", "us-gaap", ["InterestExpense"]),
    ("DepreciationDepletionAndAmortization", "us-gaap",
        ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"]),
    ("SellingGeneralAndAdministrativeExpense", "us-gaap",
        ["SellingGeneralAndAdministrativeExpense"]),
    ("ResearchAndDevelopmentExpense", "us-gaap", ["ResearchAndDevelopmentExpense"]),
    ("ShareBasedCompensation", "us-gaap", ["ShareBasedCompensation"]),
    ("OtherNonoperatingIncomeExpense", "us-gaap", ["OtherNonoperatingIncomeExpense"]),
    ("NonoperatingIncomeExpense", "us-gaap", ["NonoperatingIncomeExpense"]),
    ("ComprehensiveIncomeNetOfTax", "us-gaap", ["ComprehensiveIncomeNetOfTax"]),
    ("CommonStockDividendsPerShareCashPaid", "us-gaap",
        ["CommonStockDividendsPerShareCashPaid"]),

    # ---- BALANCE SHEET ----
    ("Assets", "us-gaap", ["Assets"]),
    ("AssetsCurrent", "us-gaap", ["AssetsCurrent"]),
    ("AssetsNoncurrent", "us-gaap", ["AssetsNoncurrent"]),
    ("Liabilities", "us-gaap", ["Liabilities"]),
    ("LiabilitiesCurrent", "us-gaap", ["LiabilitiesCurrent"]),
    ("LiabilitiesNoncurrent", "us-gaap", ["LiabilitiesNoncurrent"]),
    ("StockholdersEquity", "us-gaap", ["StockholdersEquity"]),
    ("RetainedEarningsAccumulatedDeficit", "us-gaap", ["RetainedEarningsAccumulatedDeficit"]),
    ("LongTermDebt", "us-gaap", ["LongTermDebt"]),
    ("LongTermDebtNoncurrent", "us-gaap", ["LongTermDebtNoncurrent"]),
    ("CashAndCashEquivalentsAtCarryingValue", "us-gaap",
        ["CashAndCashEquivalentsAtCarryingValue"]),
    ("AccountsReceivableNetCurrent", "us-gaap", ["AccountsReceivableNetCurrent"]),
    ("InventoryNet", "us-gaap", ["InventoryNet"]),
    ("AccountsPayableCurrent", "us-gaap", ["AccountsPayableCurrent"]),
    ("PropertyPlantAndEquipmentNet", "us-gaap", ["PropertyPlantAndEquipmentNet"]),
    ("PropertyPlantAndEquipmentGross", "us-gaap", ["PropertyPlantAndEquipmentGross"]),
    ("Goodwill", "us-gaap", ["Goodwill"]),
    ("FiniteLivedIntangibleAssetsNet", "us-gaap", ["FiniteLivedIntangibleAssetsNet"]),
    ("DeferredRevenueCurrent", "us-gaap", ["DeferredRevenueCurrent"]),
    ("CommercialPaper", "us-gaap", ["CommercialPaper"]),
    ("CommonStockSharesIssued", "us-gaap", ["CommonStockSharesIssued"]),
    ("CommonStockSharesOutstanding", "us-gaap", ["CommonStockSharesOutstanding"]),
    ("OperatingLeaseLiability", "us-gaap", ["OperatingLeaseLiability"]),
    ("FinanceLeaseLiability", "us-gaap", ["FinanceLeaseLiability"]),

    # ---- CASH FLOW ----
    ("NetCashProvidedByUsedInOperatingActivities", "us-gaap",
        ["NetCashProvidedByUsedInOperatingActivities"]),
    ("NetCashProvidedByUsedInInvestingActivities", "us-gaap",
        ["NetCashProvidedByUsedInInvestingActivities"]),
    ("NetCashProvidedByUsedInFinancingActivities", "us-gaap",
        ["NetCashProvidedByUsedInFinancingActivities"]),
    ("PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap",
        ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    ("PaymentsToAcquireBusinessesNetOfCashAcquired", "us-gaap",
        ["PaymentsToAcquireBusinessesNetOfCashAcquired"]),
    ("ProceedsFromIssuanceOfLongTermDebt", "us-gaap", ["ProceedsFromIssuanceOfLongTermDebt"]),
    ("RepaymentsOfLongTermDebt", "us-gaap", ["RepaymentsOfLongTermDebt"]),
    ("PaymentsForRepurchaseOfCommonStock", "us-gaap", ["PaymentsForRepurchaseOfCommonStock"]),
    ("PaymentsOfDividends", "us-gaap", ["PaymentsOfDividends"]),
    ("ProceedsFromIssuanceOfCommonStock", "us-gaap", ["ProceedsFromIssuanceOfCommonStock"]),
    ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "us-gaap",
        ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    ("IncomeTaxesPaidNet", "us-gaap", ["IncomeTaxesPaidNet"]),
    ("InterestPaidNet", "us-gaap", ["InterestPaidNet"]),

    # ---- DEI ----
    ("EntityCommonStockSharesOutstanding", "dei", ["EntityCommonStockSharesOutstanding"]),
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_date(s):
    """Parse an ISO date string; return None on failure."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def normalize_ticker(t):
    """Normalize a ticker for matching: uppercase, dots -> dashes, trimmed."""
    if t is None:
        return ""
    return str(t).strip().upper().replace(".", "-")


def get_sp500():
    """Download the S&P 500 constituents from Wikipedia and return a DataFrame
    with columns: ticker, name, sector, industry."""
    headers = {"User-Agent": "Mozilla/5.0 (financial-research; contact@example.com)"}
    resp = requests.get(WIKI_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    tables = pd.read_html(resp.text)
    # Find the constituents table (the one with a 'Symbol' column).
    df = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if "Symbol" in cols and "Security" in cols:
            df = t
            break
    if df is None:
        raise RuntimeError("Could not locate the S&P 500 constituents table on Wikipedia")

    df = df.rename(columns={
        "Symbol": "ticker",
        "Security": "name",
        "GICS Sector": "sector",
        "GICS Sub-Industry": "industry",
    })
    df = df[["ticker", "name", "sector", "industry"]].copy()
    # Clean tickers: dots -> dashes (BRK.B -> BRK-B).
    df["ticker"] = df["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
    df = df.drop_duplicates(subset="ticker").reset_index(drop=True)
    return df


def build_cik_lookup(tickers_path):
    """Load tickers.json and build two dicts:
       ticker_to_info: ticker -> {cik_str, title}
       norm_to_cik:    normalized_ticker -> cik
    """
    with open(tickers_path, "r") as f:
        raw = json.load(f)

    ticker_to_info = {}
    norm_to_cik = {}
    for _, rec in raw.items():
        ticker = rec.get("ticker")
        cik = rec.get("cik_str")
        title = rec.get("title")
        if ticker is None or cik is None:
            continue
        ticker_to_info[ticker] = {"cik_str": cik, "title": title}
        norm_to_cik[normalize_ticker(ticker)] = cik
    return ticker_to_info, norm_to_cik


def extract_tag(facts, namespace, tags):
    """Return {fiscal_year: value} for the given tag list.

    Only 10-K filings with fiscal_year >= MIN_FISCAL_YEAR are considered.
    Within a fiscal year, the *current-period* value is selected (the entry
    with the latest 'filed' date, then the latest period 'end' date — which
    distinguishes the current year from prior-year comparatives that share
    the same fy/filing). Fallback tags only fill years the primary lacks.
    """
    ns = facts.get(namespace, {})
    out = {}  # fy -> value

    for tag in tags:
        node = ns.get(tag)
        if not node:
            continue
        units = node.get("units", {})

        # Collect the best entry per fiscal year for THIS tag.
        best = {}  # fy -> (filed_str, end_str, value)
        for entries in units.values():
            for e in entries:
                if e.get("form") != "10-K":
                    continue
                fy = e.get("fy")
                if not isinstance(fy, int) or fy < MIN_FISCAL_YEAR:
                    continue

                start = e.get("start")
                end = e.get("end")
                # For duration concepts (have a start), require an annual span
                # so quarterly/partial periods are excluded.
                if start:
                    sd, ed = _parse_date(start), _parse_date(end)
                    if sd is None or ed is None:
                        continue
                    span = (ed - sd).days
                    if span < 350 or span > 400:
                        continue

                val = e.get("val")
                if val is None:
                    continue

                filed = e.get("filed") or ""
                end_key = end or ""
                key = (filed, end_key)
                prev = best.get(fy)
                if prev is None or key > (prev[0], prev[1]):
                    best[fy] = (filed, end_key, val)

        for fy, (_, _, val) in best.items():
            if fy not in out:  # primary wins; fallback only fills gaps
                out[fy] = val

    return out


def compute_derived(per_year):
    """Given {metric: {fy: value}}, return {derived_metric: {fy: value}}."""

    def g(metric, fy):
        return per_year.get(metric, {}).get(fy)

    # Union of all fiscal years seen for this company.
    years = set()
    for d in per_year.values():
        years.update(d.keys())

    derived = {
        "EBITDA": {},
        "FreeCashFlow": {},
        "TotalDebt": {},
        "NetDebt": {},
        "GrossMarginPct": {},
        "OperatingMarginPct": {},
        "NetMarginPct": {},
    }

    for fy in years:
        op_inc = g("OperatingIncomeLoss", fy)
        dda = g("DepreciationDepletionAndAmortization", fy)
        ocf = g("NetCashProvidedByUsedInOperatingActivities", fy)
        capex = g("PaymentsToAcquirePropertyPlantAndEquipment", fy)
        ltd = g("LongTermDebt", fy)
        cp = g("CommercialPaper", fy)
        cash = g("CashAndCashEquivalentsAtCarryingValue", fy)
        revenue = g("RevenueFromContractWithCustomerExcludingAssessedTax", fy)
        gross = g("GrossProfit", fy)
        net = g("NetIncomeLoss", fy)

        if op_inc is not None and dda is not None:
            derived["EBITDA"][fy] = op_inc + dda

        if ocf is not None and capex is not None:
            derived["FreeCashFlow"][fy] = ocf - capex

        # TotalDebt: use 0 if either component missing; only emit if at least
        # one component is present.
        if ltd is not None or cp is not None:
            total_debt = (ltd or 0) + (cp or 0)
            derived["TotalDebt"][fy] = total_debt
            if cash is not None:
                derived["NetDebt"][fy] = total_debt - cash

        if revenue not in (None, 0):
            if gross is not None:
                derived["GrossMarginPct"][fy] = gross / revenue
            if op_inc is not None:
                derived["OperatingMarginPct"][fy] = op_inc / revenue
            if net is not None:
                derived["NetMarginPct"][fy] = net / revenue

    return derived


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def init_db(conn):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS companies")
    cur.execute("DROP TABLE IF EXISTS financials")
    cur.execute("""
        CREATE TABLE companies (
            cik INTEGER PRIMARY KEY,
            ticker TEXT,
            name TEXT,
            sector TEXT,
            industry TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cik INTEGER,
            ticker TEXT,
            fiscal_year INTEGER,
            metric TEXT,
            value REAL
        )
    """)
    conn.commit()


# --------------------------------------------------------------------------
# Supabase upload (PostgREST upsert)
# --------------------------------------------------------------------------
SUPABASE_BATCH = 5000


def upload_to_supabase(company_rows, fin_rows):
    """Upsert companies and financials into Supabase via the PostgREST API.

    Requires SUPABASE_URL / SUPABASE_KEY (service_role) in the environment and
    the tables to already exist (run the setup SQL in the Supabase SQL editor).
    NULL financial values are skipped — they carry no information and the
    dashboard treats missing (cik, year, metric) rows as N/A.
    """
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("Skipping Supabase upload (no SUPABASE_URL / SUPABASE_KEY in .env).")
        return

    base = SUPABASE_URL.rstrip("/") + "/rest/v1"
    auth = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    write_headers = dict(auth)
    write_headers["Content-Type"] = "application/json"
    write_headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    # Verify the tables exist before attempting to upload.
    try:
        chk = requests.get(f"{base}/companies?select=cik&limit=1", headers=auth, timeout=30)
    except requests.RequestException as exc:
        print(f"Supabase unreachable, skipping upload: {exc}")
        return
    if chk.status_code == 404:
        print("\n*** Supabase tables not found. Run the setup SQL in the Supabase "
              "SQL editor first, then re-run extract.py. Skipping upload. ***")
        return
    if chk.status_code >= 300:
        print(f"Supabase check failed ({chk.status_code}): {chk.text[:200]} — skipping upload.")
        return

    print("\nUploading to Supabase ...")

    # ---- companies (upsert on cik) ----
    comp_payload = [
        {"cik": c[0], "ticker": c[1], "name": c[2], "sector": c[3], "industry": c[4]}
        for c in company_rows
    ]
    r = requests.post(f"{base}/companies?on_conflict=cik",
                      headers=write_headers, data=json.dumps(comp_payload), timeout=120)
    if r.status_code < 300:
        print(f"  companies: uploaded {len(comp_payload)} rows")
    else:
        print(f"  companies upload FAILED ({r.status_code}): {r.text[:300]}")

    # ---- financials (upsert on cik,fiscal_year,metric); skip NULL values ----
    payload = []
    for cik, ticker, fy, metric, value in fin_rows:
        if value is None:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        payload.append({
            "cik": cik, "ticker": ticker, "fiscal_year": fy,
            "metric": metric, "value": value,
        })

    total = len(payload)
    uploaded = 0
    failed = 0
    for start in range(0, total, SUPABASE_BATCH):
        chunk = payload[start:start + SUPABASE_BATCH]
        try:
            r = requests.post(
                f"{base}/financials?on_conflict=cik,fiscal_year,metric",
                headers=write_headers, data=json.dumps(chunk), timeout=180,
            )
        except requests.RequestException as exc:
            failed += len(chunk)
            print(f"  batch @{start} error: {exc}")
            continue
        if r.status_code < 300:
            uploaded += len(chunk)
        else:
            failed += len(chunk)
            print(f"  batch @{start} FAILED ({r.status_code}): {r.text[:200]}")
        if (start // SUPABASE_BATCH) % 5 == 0:
            print(f"  financials: {uploaded}/{total} uploaded ...")

    print(f"  financials: uploaded {uploaded}/{total} non-null rows "
          f"({failed} failed).")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    os.makedirs(PROJECT_DIR, exist_ok=True)

    if SUPABASE_URL and SUPABASE_KEY:
        print(f"Supabase credentials loaded (URL host: {SUPABASE_URL.split('//')[-1]})")
    else:
        print("Supabase credentials not set (.env) — running in local SQLite mode only.")

    print("Downloading S&P 500 list from Wikipedia ...")
    sp500 = get_sp500()
    total = len(sp500)
    print(f"  -> {total} companies in the S&P 500 list")

    print("Building CIK lookup from tickers.json ...")
    _, norm_to_cik = build_cik_lookup(TICKERS_FILE)
    print(f"  -> {len(norm_to_cik)} tickers in lookup")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    cur = conn.cursor()

    derived_names = [
        "EBITDA", "FreeCashFlow", "TotalDebt", "NetDebt",
        "GrossMarginPct", "OperatingMarginPct", "NetMarginPct",
    ]
    base_names = [cfg[0] for cfg in TAG_CONFIG]
    all_metric_names = base_names + derived_names

    companies_loaded = 0
    skipped = []  # (ticker, reason)
    fin_rows = []  # (cik, ticker, fiscal_year, metric, value)
    company_rows = []  # (cik, ticker, name, sector, industry)

    for i, row in enumerate(sp500.itertuples(index=False), start=1):
        ticker = row.ticker
        norm = normalize_ticker(ticker)

        if i % 25 == 0:
            print(f"Processing {i}/{total}: {ticker}")

        cik = norm_to_cik.get(norm)
        if cik is None:
            skipped.append((ticker, "no CIK in tickers.json"))
            continue

        path = os.path.join(COMPANYFACTS_DIR, f"CIK{cik:010d}.json")
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            skipped.append((ticker, f"companyfacts file not found (CIK {cik})"))
            continue
        except (json.JSONDecodeError, OSError) as exc:
            skipped.append((ticker, f"read error: {exc}"))
            continue

        facts = data.get("facts", {})

        # Extract base metrics: {metric: {fy: value}}
        per_year = {}
        for metric_name, namespace, tags in TAG_CONFIG:
            try:
                vals = extract_tag(facts, namespace, tags)
            except Exception:  # never let one bad tag kill a company
                vals = {}
            if vals:
                per_year[metric_name] = vals

        # Derived metrics
        derived = compute_derived(per_year)
        for dname, dvals in derived.items():
            if dvals:
                per_year[dname] = dvals

        # Determine fiscal-year span for this company.
        years = set()
        for d in per_year.values():
            years.update(d.keys())

        if not years:
            skipped.append((ticker, f"no 10-K data >= {MIN_FISCAL_YEAR} (CIK {cik})"))
            continue

        # Company row
        comp = (int(cik), ticker, getattr(row, "name", None),
                getattr(row, "sector", None), getattr(row, "industry", None))
        cur.execute(
            "INSERT OR REPLACE INTO companies (cik, ticker, name, sector, industry) "
            "VALUES (?, ?, ?, ?, ?)",
            comp,
        )
        company_rows.append(comp)

        # Financial rows: one per (fiscal_year, metric); missing -> NULL.
        for fy in sorted(years):
            for metric_name in all_metric_names:
                value = per_year.get(metric_name, {}).get(fy)
                fin_rows.append((int(cik), ticker, int(fy), metric_name, value))

        companies_loaded += 1

    print("Writing financial rows to database ...")
    cur.executemany(
        "INSERT INTO financials (cik, ticker, fiscal_year, metric, value) "
        "VALUES (?, ?, ?, ?, ?)",
        fin_rows,
    )
    conn.commit()

    # ----------------------------------------------------------------------
    # Validation table for AAPL
    # ----------------------------------------------------------------------
    print_validation(cur)

    # ----------------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------------
    cur.execute("SELECT COUNT(*) FROM financials")
    total_fin_rows = cur.fetchone()[0]

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total companies loaded  : {companies_loaded}")
    print(f"Total rows in financials: {total_fin_rows}")
    print(f"Skipped tickers ({len(skipped)}):")
    for tkr, reason in skipped:
        print(f"  - {tkr}: {reason}")

    # ----------------------------------------------------------------------
    # Upload to Supabase (no-op if tables/credentials are missing)
    # ----------------------------------------------------------------------
    upload_to_supabase(company_rows, fin_rows)

    conn.close()


def print_validation(cur):
    """Print the AAPL validation table comparing extracted vs expected."""
    metrics = [
        ("Revenue", "RevenueFromContractWithCustomerExcludingAssessedTax",
            {2022: 394328000, 2023: 383285000, 2024: 391035000}),
        ("NetIncomeLoss", "NetIncomeLoss",
            {2022: 99803000, 2023: 96995000, 2024: 93736000}),
        ("OperatingCF", "NetCashProvidedByUsedInOperatingActivities",
            {2022: 122151000, 2023: 110543000, 2024: 118254000}),
        ("Assets", "Assets",
            {2022: 352755000, 2023: 352583000, 2024: 364980000}),
        ("StockholdersEq", "StockholdersEquity",
            {2022: 50672000, 2023: 62146000, 2024: 56950000}),
        ("FreeCashFlow", "FreeCashFlow",
            {2022: 111443000, 2023: 99584000, 2024: 108807000}),
    ]
    years = [2022, 2023, 2024]

    def got(metric, fy):
        cur.execute(
            "SELECT value FROM financials WHERE ticker='AAPL' AND metric=? AND fiscal_year=?",
            (metric, fy),
        )
        r = cur.fetchone()
        return r[0] if r and r[0] is not None else None

    def fmt(v):
        if v is None:
            return "N/A"
        return f"{v:,.0f}"

    print("\n" + "=" * 118)
    print("AAPL VALIDATION  (Expected column is in THOUSANDS; SEC 'Got' values are FULL DOLLARS = 1000x larger)")
    print("=" * 118)
    header = (f"{'Metric':<16}| {'FY2022 Expected':>17} | {'FY2022 Got':>17} | "
              f"{'FY2023 Expected':>17} | {'FY2023 Got':>17} | "
              f"{'FY2024 Expected':>17} | {'FY2024 Got':>17}")
    print(header)
    print("-" * len(header))
    for label, metric, expected in metrics:
        cells = [f"{label:<16}"]
        for fy in years:
            cells.append(f"{expected[fy]:>17,}")
            cells.append(f"{fmt(got(metric, fy)):>17}")
        print(f"{cells[0]}| " + " | ".join(cells[1:]))


if __name__ == "__main__":
    main()
