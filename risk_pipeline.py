#!/usr/bin/env python3
"""
risk_pipeline.py

Batch job that derives per-company liquidity-runway and synthetic-credit
signals from the XBRL facts in the Supabase `financials` / `companies`
tables. It does NOT re-pull from EDGAR -- it reads the ~478k-row extraction
that extract.py already loaded into Supabase.

For every company it computes, per fiscal year (latest N years):
  * Runway  -- how many months of liquid assets remain against either the
              company's actual cash burn (burn mode) or, for cash-generative
              companies, a revenue-to-zero stress opex (stress_overlay mode).
  * Synthetic rating / spread / implied cost of debt -- a Damodaran-style
              interest-coverage -> rating -> default-spread mapping, using the
              financial-firm breakpoints for the Financials sector and the
              non-financial breakpoints for everyone else.

Results are upserted into `risk_metrics` (see schema.sql). The most recent
period per ticker is flagged is_latest = true.

--------------------------------------------------------------------------
Single source of truth: SUPABASE. This job READS `companies`/`financials`
from Supabase and WRITES `risk_metrics` back to Supabase. No SQLite / local
file is part of the data flow.

    --years N       fiscal years of history per ticker (default 6)
    --dry-run       read + compute + print summary, write nothing

Examples:
    python risk_pipeline.py                # read Supabase -> upsert risk_metrics
    python risk_pipeline.py --dry-run      # read Supabase, compute, write nothing

Config (.env), same keys as the other jobs:
    SUPABASE_URL, SUPABASE_KEY     (service-role key -- bypasses RLS)
    RISK_FREE_RATE                 (decimal, default 0.0435 -- 10-yr Treasury)

NOTE: `risk_metrics` must already exist in Supabase (run schema.sql once in
the Supabase SQL editor). PostgREST cannot create tables.

--------------------------------------------------------------------------
DATA CAVEATS (surfaced, not hidden):
  * `financials` is ANNUAL only. "TTM CFO" == latest fiscal-year CFO; the
    "rolling TTM history" is an annual history. fiscal_period is 'FY<year>'.
  * ShortTermInvestments / MarketableSecuritiesCurrent and undrawn revolver
    capacity are NOT in the current 66-tag extraction. The code reads them if
    present and falls back to 0 (revolver_data_available=false) otherwise, so
    liquid_assets == cash & equivalents today. The formula is wired to pick
    them up automatically once a future extraction adds those tags.
  * InterestExpense is sparse for large firms; we fall back to InterestPaidNet
    and record which tag was used in interest_expense_source.
  * There is no ROIC/WACC module in this repo yet, so implied_cost_of_debt
    stands alone (risk_free_rate is a static assumption). When a WACC module
    lands, compare its market/synthetic cost of debt against this column.
"""

import os
import json
import math
import argparse
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Static risk-free rate used for implied cost of debt. Replace with a live
# feed (or the WACC module's risk-free input) when one exists.
RISK_FREE_RATE = float(os.environ.get("RISK_FREE_RATE", "0.0435"))

DEFAULT_YEARS = 6

# ---- XBRL tags we read (all already present in `financials`) --------------
# Cash & equivalents fallback chain (first non-null wins). Banks/insurers
# (JPM, WFC, BAC) and some others (PG) report only the cash-flow end-of-period
# line -- CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents --
# rather than the balance-sheet CashAndCashEquivalentsAtCarryingValue, so we
# fall back to it. These are the only two cash tags in the extraction.
TAGS_CASH = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
# Short-term investments: try these in order; none are in the current
# extraction, so this resolves to None -> 0 until a future pull adds them.
TAGS_STI = ["ShortTermInvestments", "MarketableSecuritiesCurrent"]
TAG_REVOLVER = "UndrawnRevolverCapacity"          # never in companyfacts -> 0
TAG_CFO = "NetCashProvidedByUsedInOperatingActivities"
TAG_COST_OF_REVENUE = "CostOfGoodsAndServicesSold"
TAG_OPEX = "OperatingExpenses"
TAG_SGA = "SellingGeneralAndAdministrativeExpense"
TAG_EBIT = "OperatingIncomeLoss"
# Pre-tax income, used to reconstruct EBIT (pretax + interest) when
# OperatingIncomeLoss is absent -- the norm for financial-sector firms.
TAG_PRETAX = ("IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
              "ExtraordinaryItemsNoncontrollingInterest")
TAG_INTEREST = "InterestExpense"
TAG_INTEREST_PAID = "InterestPaidNet"
TAG_TOTAL_DEBT = "TotalDebt"
TAG_NET_DEBT = "NetDebt"

# Every tag we need to pull, for the source query.
NEEDED_TAGS = sorted(set([
    *TAGS_CASH, *TAGS_STI, TAG_REVOLVER, TAG_CFO,
    TAG_COST_OF_REVENUE, TAG_OPEX, TAG_SGA,
    TAG_EBIT, TAG_PRETAX, TAG_INTEREST, TAG_INTEREST_PAID,
    TAG_TOTAL_DEBT, TAG_NET_DEBT,
]))

# Runway-month -> flag buckets (user-specified).
#   Critical <6 | Warning 6-12 | Watch 12-24 | Healthy >=24
def runway_flag(months):
    if months is None:
        return "N/A"
    if months < 6:
        return "Critical"
    if months < 12:
        return "Warning"
    if months < 24:
        return "Watch"
    return "Healthy"


# --------------------------------------------------------------------------
# Damodaran synthetic-rating tables
# interest-coverage lower-bound (inclusive) -> (rating, default spread).
# Ranges are read top-down; the first row whose `lo` the coverage meets wins.
# Financials carry structurally higher leverage, so they get their own
# (higher) coverage breakpoints -- this is the "financial breakpoint table".
# Spreads are Damodaran's published default-spread column (decimals).
# --------------------------------------------------------------------------
NONFIN_TABLE = [
    (8.50,  "Aaa/AAA",  0.0059),
    (6.50,  "Aa2/AA",   0.0078),
    (5.50,  "A1/A+",    0.0098),
    (4.25,  "A2/A",     0.0108),
    (3.00,  "A3/A-",    0.0122),
    (2.50,  "Baa2/BBB", 0.0156),
    (2.25,  "Ba1/BB+",  0.0200),
    (2.00,  "Ba2/BB",   0.0240),
    (1.75,  "B1/B+",    0.0351),
    (1.50,  "B2/B",     0.0421),
    (1.25,  "B3/B-",    0.0515),
    (0.80,  "Caa/CCC",  0.0820),
    (0.65,  "Ca2/CC",   0.0864),
    (0.20,  "C2/C",     0.1134),
    (float("-inf"), "D2/D", 0.1512),
]

FIN_TABLE = [
    (12.50, "Aaa/AAA",  0.0059),
    (9.50,  "Aa2/AA",   0.0078),
    (7.50,  "A1/A+",    0.0098),
    (6.00,  "A2/A",     0.0108),
    (4.50,  "A3/A-",    0.0122),
    (4.00,  "Baa2/BBB", 0.0156),
    (3.50,  "Ba1/BB+",  0.0200),
    (3.00,  "Ba2/BB",   0.0240),
    (2.50,  "B1/B+",    0.0351),
    (2.00,  "B2/B",     0.0421),
    (1.50,  "B3/B-",    0.0515),
    (1.25,  "Caa/CCC",  0.0820),
    (0.80,  "Ca2/CC",   0.0864),
    (0.50,  "C2/C",     0.1134),
    (float("-inf"), "D2/D", 0.1512),
]

FINANCIAL_SECTORS = {"Financials"}


def synthetic_rating(coverage, is_financial):
    """coverage -> (rating, spread) using the correct breakpoint table."""
    if coverage is None:
        return None, None
    table = FIN_TABLE if is_financial else NONFIN_TABLE
    for lo, rating, spread in table:
        if coverage >= lo:
            return rating, spread
    return None, None


# --------------------------------------------------------------------------
# Small numeric helpers -- every division is guarded (no divide-by-zero).
# --------------------------------------------------------------------------
def _num(v):
    """Coerce to float or None (handles '', None, NaN)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def safe_div(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def first_present(facts_year, tags):
    """First non-None value among `tags` for a given {tag: value} dict."""
    for t in tags:
        v = _num(facts_year.get(t))
        if v is not None:
            return v, t
    return None, None


# --------------------------------------------------------------------------
# Core computation for one (ticker, fiscal_year)
# --------------------------------------------------------------------------
def compute_row(ticker, company, fy, facts):
    """
    facts: {tag: value} for this ticker+fiscal_year.
    Returns a dict matching the risk_metrics columns.
    """
    sector = company.get("sector")
    is_financial = sector in FINANCIAL_SECTORS
    notes = []

    # ---- liquid assets ----------------------------------------------------
    cash, cash_source = first_present(facts, TAGS_CASH)
    sti, sti_tag = first_present(facts, TAGS_STI)
    revolver = _num(facts.get(TAG_REVOLVER))
    revolver_available = revolver is not None
    if not revolver_available:
        revolver = 0.0
    if sti is None:
        sti = 0.0
        notes.append("no short-term-investments tag; STI treated as 0")
    else:
        notes.append(f"STI from {sti_tag}")
    if not revolver_available:
        notes.append("no revolver disclosure; undrawn revolver treated as 0")

    liquid_assets = None
    if cash is not None:
        liquid_assets = cash + sti + revolver

    # ---- runway -----------------------------------------------------------
    cfo = _num(facts.get(TAG_CFO))
    cost_rev = _num(facts.get(TAG_COST_OF_REVENUE)) or 0.0
    opex = _num(facts.get(TAG_OPEX)) or 0.0
    sga = _num(facts.get(TAG_SGA)) or 0.0
    stress_opex = cost_rev + opex + sga  # annual; revenue-to-zero stress

    runway_mode = "insufficient_data"
    runway_months = None
    monthly_outflow = None
    denominator_basis = None

    if liquid_assets is None:
        notes.append("no cash tag; runway not computable")
    elif cfo is not None and cfo < 0:
        # Actual cash burn: months until liquid assets are exhausted.
        monthly_outflow = -cfo / 12.0
        denominator_basis = "operating_cash_burn"
        runway_months = safe_div(liquid_assets, monthly_outflow)
        runway_mode = "burn" if runway_months is not None else "insufficient_data"
    else:
        # Cash-generative (or CFO missing): revenue-to-zero stress overlay.
        # Monthly stress opex = (CostOfRevenue + OperatingExpenses + SG&A)/12.
        if stress_opex > 0:
            monthly_outflow = stress_opex / 12.0
            denominator_basis = "stress_opex"
            runway_months = safe_div(liquid_assets, monthly_outflow)
            runway_mode = "stress_overlay" if runway_months is not None else "insufficient_data"
            if cfo is None:
                notes.append("CFO missing; assumed cash-generative for stress overlay")
        else:
            notes.append("no opex tags; stress runway not computable")

    flag = runway_flag(runway_months) if runway_mode != "insufficient_data" else "N/A"

    # ---- synthetic credit -------------------------------------------------
    # Interest expense: P&L tag preferred, cash-paid fallback.
    interest, int_source = first_present(facts, [TAG_INTEREST, TAG_INTEREST_PAID])
    if interest is not None and interest <= 0:
        # Zero/negative interest -> not a meaningful denominator; treat as none.
        interest, int_source = None, None

    # EBIT: OperatingIncomeLoss when present, else reconstruct as pre-tax income
    # + interest expense. OperatingIncomeLoss is absent for almost all financial
    # firms (banks, insurers, asset managers), so without this fallback they all
    # land at N/A; the reconstruction gives them a real coverage-based rating.
    # Requires a positive interest figure to add back.
    ebit = _num(facts.get(TAG_EBIT))
    if ebit is None and interest is not None:
        pretax = _num(facts.get(TAG_PRETAX))
        if pretax is not None:
            ebit = pretax + interest
            notes.append(f"EBIT reconstructed = pre-tax income + {int_source} "
                         "(no OperatingIncomeLoss tag)")

    coverage = safe_div(ebit, interest)  # None if EBIT or interest missing/zero
    if coverage is not None:
        rating, spread = synthetic_rating(coverage, is_financial)
    elif interest is None and ebit is not None and ebit >= 0:
        # No interest expense tagged (both InterestExpense and InterestPaidNet
        # absent or <= 0) AND profitable at the operating line => no meaningful
        # debt-service burden => best synthetic rating (per spec). Guarded on
        # EBIT >= 0 so a distressed no-interest firm is not mislabeled AAA.
        best = (FIN_TABLE if is_financial else NONFIN_TABLE)[0]
        rating, spread = best[1], best[2]
        int_source = None
        notes.append(f"no interest expense tagged; EBIT>=0 -> assigned {rating} "
                     "(negligible debt-service burden)")
    else:
        rating, spread = None, None
        if ebit is None:
            notes.append("no EBIT (OperatingIncomeLoss) tag; synthetic rating N/A")
        elif ebit < 0:
            notes.append("negative EBIT and no interest tag; synthetic rating N/A")
    implied_cod = (RISK_FREE_RATE + spread) if spread is not None else None

    return {
        "ticker": ticker,
        "fiscal_period": f"FY{fy}",
        "fiscal_year": fy,
        "company_name": company.get("name"),
        "sector": sector,
        "is_financial_sector": is_financial,
        "is_latest": False,  # set later, once we know each ticker's max year
        "cash_and_equivalents": cash,
        "cash_source": cash_source,
        "short_term_investments": sti,
        "undrawn_revolver": revolver,
        "revolver_data_available": revolver_available,
        "liquid_assets": liquid_assets,
        "ttm_cfo": cfo,
        "stress_opex": stress_opex if stress_opex > 0 else None,
        "monthly_outflow": monthly_outflow,
        "denominator_basis": denominator_basis,
        "runway_mode": runway_mode,
        "runway_months": round(runway_months, 2) if runway_months is not None else None,
        "runway_flag": flag,
        "ebit": ebit,
        "interest_expense": interest,
        "interest_expense_source": int_source,
        "interest_coverage": round(coverage, 3) if coverage is not None else None,
        "total_debt": _num(facts.get(TAG_TOTAL_DEBT)),
        "net_debt": _num(facts.get(TAG_NET_DEBT)),
        "synthetic_rating": rating,
        "synthetic_spread": spread,
        "risk_free_rate": RISK_FREE_RATE,
        "implied_cost_of_debt": round(implied_cod, 4) if implied_cod is not None else None,
        "notes": "; ".join(notes) if notes else None,
    }


# --------------------------------------------------------------------------
# Load facts -- Supabase is the single source of truth
# --------------------------------------------------------------------------
def _sb_headers():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY not set in .env")
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def load_facts(years):
    """Read companies + financials from Supabase.

    Returns (companies_by_ticker, facts_by_ticker) where
    facts_by_ticker[ticker] = {fiscal_year: {tag: value}}, limited to the
    latest `years` fiscal years per ticker that carry any needed tag.
    """
    base = SUPABASE_URL.rstrip("/") + "/rest/v1"
    headers = _sb_headers()

    companies = {}
    rows = _sb_get_all(f"{base}/companies", headers,
                       {"select": "cik,ticker,name,sector,industry"})
    for r in rows:
        if r.get("ticker"):
            companies[r["ticker"]] = r

    facts = {}
    tag_filter = "(" + ",".join(NEEDED_TAGS) + ")"
    frows = _sb_get_all(f"{base}/financials", headers, {
        "select": "ticker,fiscal_year,metric,value",
        "metric": f"in.{tag_filter}",
        "value": "not.is.null",
    })
    for r in frows:
        t = r.get("ticker")
        if t is None:
            continue
        facts.setdefault(t, {}).setdefault(r["fiscal_year"], {})[r["metric"]] = r["value"]
    return _limit_years(companies, facts, years)


def _sb_get_all(url, headers, params, page=1000):
    """Paginate PostgREST with Range headers until fewer than `page` rows."""
    out, offset = [], 0
    while True:
        h = dict(headers)
        h["Range-Unit"] = "items"
        h["Range"] = f"{offset}-{offset + page - 1}"
        r = requests.get(url, headers=h, params=params, timeout=60)
        if r.status_code >= 300 and r.status_code != 206:
            raise RuntimeError(f"Supabase GET {r.status_code}: {r.text[:300]}")
        batch = r.json()
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


def _limit_years(companies, facts, years):
    """Keep only the latest `years` fiscal years per ticker."""
    for t, by_year in facts.items():
        keep = sorted(by_year.keys(), reverse=True)[:years]
        facts[t] = {y: by_year[y] for y in keep}
    return companies, facts


# --------------------------------------------------------------------------
# Sink -- upsert into the Supabase risk_metrics table
# --------------------------------------------------------------------------
RISK_COLUMNS = [
    "ticker", "fiscal_period", "fiscal_year", "company_name", "sector",
    "is_financial_sector", "is_latest", "cash_and_equivalents", "cash_source",
    "short_term_investments", "undrawn_revolver", "revolver_data_available",
    "liquid_assets", "ttm_cfo", "stress_opex", "monthly_outflow",
    "denominator_basis", "runway_mode", "runway_months", "runway_flag",
    "ebit", "interest_expense", "interest_expense_source", "interest_coverage",
    "total_debt", "net_debt", "synthetic_rating", "synthetic_spread",
    "risk_free_rate", "implied_cost_of_debt", "notes",
]


def write_supabase(records, chunk=500):
    """Upsert into public.risk_metrics (on conflict ticker,fiscal_period)."""
    base = SUPABASE_URL.rstrip("/") + "/rest/v1"
    endpoint = f"{base}/risk_metrics?on_conflict=ticker,fiscal_period"
    headers = dict(_sb_headers())
    headers.update({
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    payload = [{c: rec.get(c) for c in RISK_COLUMNS} for rec in records]
    total = 0
    for i in range(0, len(payload), chunk):
        batch = payload[i:i + chunk]
        r = requests.post(endpoint, headers=headers, data=json.dumps(batch))
        if r.status_code >= 300:
            if "PGRST205" in r.text or "Could not find the table" in r.text:
                raise SystemExit(
                    "\n  ERROR: public.risk_metrics does not exist in Supabase.\n"
                    "  Create it once by running schema.sql in the Supabase SQL "
                    "editor, then re-run this pipeline.\n")
            print(f"  ERROR {r.status_code}: {r.text[:400]}")
            r.raise_for_status()
        total += len(batch)
        print(f"  supabase: upserted {total}/{len(payload)}")
    print(f"  supabase: done, {total} rows in risk_metrics")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Compute risk_metrics from the Supabase financials tables.")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--dry-run", action="store_true",
                    help="read Supabase + compute + print summary, write nothing")
    args = ap.parse_args()

    started = datetime.now()
    print(f"=== risk_pipeline started {started:%Y-%m-%d %H:%M:%S} ===")
    print(f"  source=supabase  sink={'none (dry-run)' if args.dry_run else 'supabase'}"
          f"  years={args.years}  risk_free={RISK_FREE_RATE:.4f}")

    companies, facts = load_facts(args.years)
    print(f"  loaded {len(companies)} companies, "
          f"{sum(len(v) for v in facts.values())} ticker-years of facts from Supabase")

    # ---- compute every (ticker, year) ------------------------------------
    records = []
    insufficient = []
    for ticker, by_year in facts.items():
        company = companies.get(ticker, {"name": ticker, "sector": None})
        years_sorted = sorted(by_year.keys(), reverse=True)
        for i, fy in enumerate(years_sorted):
            rec = compute_row(ticker, company, fy, by_year[fy])
            rec["is_latest"] = (i == 0)  # newest year per ticker
            records.append(rec)
            if rec["is_latest"] and rec["runway_mode"] == "insufficient_data":
                insufficient.append((ticker, rec["fiscal_period"], rec["notes"]))

    # ---- distribution summary --------------------------------------------
    latest = [r for r in records if r["is_latest"]]
    by_flag, by_mode = {}, {}
    for r in latest:
        by_flag[r["runway_flag"]] = by_flag.get(r["runway_flag"], 0) + 1
        by_mode[r["runway_mode"]] = by_mode.get(r["runway_mode"], 0) + 1
    print(f"\n  latest snapshot: {len(latest)} companies")
    print(f"    runway flags : {dict(sorted(by_flag.items()))}")
    print(f"    runway modes : {dict(sorted(by_mode.items()))}")
    rated = sum(1 for r in latest if r["synthetic_rating"])
    print(f"    synthetic rating assigned: {rated}/{len(latest)}")

    # ---- audit log for insufficient_data ---------------------------------
    if insufficient:
        log_path = PROJECT_DIR / "risk_insufficient_data.log"
        with open(log_path, "w") as f:
            f.write(f"# insufficient_data (latest period) -- {started:%Y-%m-%d %H:%M}\n")
            for tkr, per, why in sorted(insufficient):
                f.write(f"{tkr}\t{per}\t{why}\n")
        print(f"\n  {len(insufficient)} companies with insufficient_data (latest) "
              f"-> logged to {log_path.name}")

    # ---- write -----------------------------------------------------------
    print()
    if args.dry_run:
        print("  dry-run: nothing written to Supabase.")
    else:
        write_supabase(records)

    print(f"\n=== finished in {(datetime.now() - started).seconds}s, "
          f"{len(records)} rows computed ===")


if __name__ == "__main__":
    main()
