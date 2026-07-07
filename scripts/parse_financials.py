#!/usr/bin/env python3
"""
Phase 1: Extract financial metrics from SEC 10-K filings for 13 US banks.
Output: data/processed/bank_financials.csv

Strategy:
  1. XBRL (post-2009): parse ix:nonFraction tags, filter to consolidated
     current-year contexts (no segment dimensions).
  2. Table extraction (all years): pattern-match row labels in HTML tables.
  3. Text extraction: regex for ratios mentioned in prose.
Priority: XBRL > table > text (first non-null wins).
All dollar amounts normalized to millions of USD.
"""
import re
import sys
from pathlib import Path
from collections import defaultdict
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
from tqdm import tqdm

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
OUT_DIR  = DATA_DIR / "processed"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TICKERS = ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","STT","BK","SCHW"]

# ── XBRL concept → output column ──────────────────────────────────────────────
# Keys are lowercased for case-insensitive matching
XBRL_CONCEPTS = {
    # Income Statement
    "us-gaap:netincomeloss": "net_income",
    "us-gaap:netincomelossavailabletocommonstockholdersbasic": "net_income",
    "us-gaap:interestanddividendincomeoperating": "net_interest_income",
    "us-gaap:interestincomeexpensenet": "net_interest_income",
    "us-gaap:noninterestincome": "noninterest_revenue",
    "us-gaap:revenuesnetofinterestexpense": "total_net_revenue",
    "us-gaap:noninterestexpense": "total_noninterest_expense",
    "us-gaap:incomelossbeforeincometaxes": "income_before_tax",
    "us-gaap:incomelossbeforeincometaxesextraordinaryitemsnoncontrollinginterest": "income_before_tax",
    "us-gaap:incomelossbeforeincometaxesminorityinterest": "income_before_tax",
    "us-gaap:provisionforloanandleaselosses": "provision_credit_losses",
    "us-gaap:provisionforloanleaseandotherlosses": "provision_credit_losses",
    "us-gaap:provisionforcreditlosses": "provision_credit_losses",
    "us-gaap:creditlossexpenseorreversal": "provision_credit_losses",
    "us-gaap:earningspersharebasic": "eps_basic",
    "us-gaap:earningspersharediluted": "eps_diluted",
    # Balance Sheet
    "us-gaap:assets": "total_assets",
    "us-gaap:liabilities": "total_liabilities",
    "us-gaap:stockholdersequity": "total_equity",
    "us-gaap:stockholdersequityincludingportionattributabletononcontrollinginterest": "total_equity",
    "us-gaap:deposits": "total_deposits",
    # Modern loan concepts (post-ASC 326 / CECL)
    "us-gaap:financingreceivableexcludingaccruedinterestafterallowanceforcreditloss": "net_loans",
    "us-gaap:financingreceivableexcludingaccruedinterestbeforeallowanceforcreditloss": "total_loans_gross",
    # Older loan concepts (pre-CECL)
    "us-gaap:loansandleasesreceivablenetreportedamount": "net_loans",
    "us-gaap:loansandleasesreceivablegrosscarryingamount": "total_loans_gross",
    "us-gaap:loansandleasesreceivablenetofunearnedincome": "total_loans_gross",
    "us-gaap:financingreceivablenetofunearnedincome": "total_loans_gross",
    "us-gaap:notesreceivablenet": "net_loans",
    # Allowance
    "us-gaap:allowanceforloanandleaselosses": "allowance_loan_losses",
    "us-gaap:financingreceivableallowanceforcreditlosses": "allowance_loan_losses",
    "us-gaap:financingreceivableallowanceforcreditlossexcludingaccruedinterest": "allowance_loan_losses",
    # Debt
    "us-gaap:longtermdebt": "long_term_debt",
    "us-gaap:longtermdebtandcapitalleaseobligations": "long_term_debt",
    "us-gaap:longtermdebtandcapitalleaseobligationsincludingcurrentmaturities": "long_term_debt",
    "us-gaap:shorttermborrowings": "short_term_borrowings",
    "us-gaap:cashandduefrombanks": "cash_due_from_banks",
    "us-gaap:cashandcashequivalentsatcarryingvalue": "cash_due_from_banks",
    "us-gaap:tradingsecurities": "trading_assets",
    "us-gaap:tradingassetsexcludingdebtandequitysecurities": "trading_assets",
    "us-gaap:availableforsalesecurities": "investment_securities_afs",
    "us-gaap:debtsecuritiesavailableforsaleamortizedcost": "investment_securities_afs",
    "us-gaap:heldtomaturitysecurities": "investment_securities_htm",
    # Capital — modern concepts use RiskWeightedAssets not RiskBasedCapital
    "us-gaap:tieronecapital": "tier1_capital",
    "us-gaap:tieroneriskcapital": "tier1_capital",
    "us-gaap:tieronecapitaltoriskbasedcapital": "tier1_capital_ratio",
    "us-gaap:tieronecapitaltoriskweightedassets": "tier1_capital_ratio",       # modern name
    "us-gaap:tieroneriskcapitaltoriskweightedassets": "tier1_capital_ratio",
    "us-gaap:capitaltoriskbasedcapital": "total_capital_ratio",
    "us-gaap:capitaltoriskweightedassets": "total_capital_ratio",              # modern name
    "us-gaap:tieronecapitaltoaverageassets": "tier1_leverage_ratio",
    "us-gaap:tieronelevageratio": "tier1_leverage_ratio",
    "us-gaap:commonequitytieronecapitalratio": "cet1_ratio",
    "us-gaap:riskweightedassets": "rwa",
    "us-gaap:riskbasedcapitalrequirementsriskweightedassets": "rwa",
    # Profitability
    "us-gaap:returnonequity": "roe",
    "us-gaap:returnonassets": "roa",
    # Credit Quality
    "us-gaap:loansandleasesreceivableimpairedinterestlostonnonaccrualloans": "npl",
    "us-gaap:financingreceivablerecordedinatcostnonaccrual": "npl",
    "us-gaap:loansandleasesreceivablenonperformingloansandleasesnettotal": "npl",
    "us-gaap:allowanceforcreditlossesoncreditcardreceivablescurrentperiodnetchargeoffs": "net_charge_offs",
    # Fair Value Hierarchy
    "us-gaap:fairvaluemeasurementwithnlevel1inputs": "level1_assets",
    "us-gaap:fairvaluemeasurementwithnlevel2inputs": "level2_assets",
    "us-gaap:fairvaluemeasurementwithnlevel3inputs": "level3_assets",
    "us-gaap:fairvaluemeasurementswithunobservableinputsreconciliationrecurringbasisassetvalue": "level3_assets",
}

# ── Table row label patterns → output column ──────────────────────────────────
LABEL_PATTERNS = [
    (r"net\s+income(?!\s+(?:tax|per\s|attributable|before|from))", "net_income"),
    (r"total\s+net\s+(?:interest\s+income|interest\s+revenue)", "net_interest_income"),
    (r"^net\s+interest\s+income", "net_interest_income"),
    (r"noninterest(?:ed)?\s+(?:income|revenue)", "noninterest_revenue"),
    (r"total\s+net\s+revenue", "total_net_revenue"),
    (r"total\s+(?:net\s+)?revenues?\s*$", "total_net_revenue"),
    (r"(?:total\s+)?noninterest(?:ed)?\s+expense", "total_noninterest_expense"),
    (r"provision\s+for\s+(?:credit\s+)?(?:loan|lease)?\s*loss(?:es)?", "provision_credit_losses"),
    (r"income\s+before\s+(?:income\s+)?tax(?:es)?", "income_before_tax"),
    (r"diluted\s+(?:earnings|net\s+income|eps)\s+per\s+(?:common\s+)?share", "eps_diluted"),
    (r"basic\s+(?:earnings|net\s+income|eps)\s+per\s+(?:common\s+)?share", "eps_basic"),
    (r"(?:net\s+income\s+)?(?:earnings\s+)?per\s+(?:common\s+)?share[,\s]+diluted", "eps_diluted"),
    (r"(?:net\s+income\s+)?(?:earnings\s+)?per\s+(?:common\s+)?share[,\s]+basic", "eps_basic"),
    (r"total\s+assets?\s*$", "total_assets"),
    (r"total\s+liabilities?\s*$", "total_liabilities"),
    (r"(?:total\s+)?(?:stockholders|shareholders)'?\s*(?:equity|common)", "total_equity"),
    (r"total\s+deposits?\s*$", "total_deposits"),
    (r"(?:total\s+)?(?:loans|loans\s+and\s+leases)\s*,?\s*net\s*$", "net_loans"),
    (r"(?:loans|loans\s+and\s+leases)\s*$", "total_loans_gross"),
    (r"allowance\s+for\s+(?:loan|credit)(?:\s+and\s+lease)?\s+loss(?:es)?", "allowance_loan_losses"),
    (r"long[-\s]?term\s+(?:debt|borrowings?)", "long_term_debt"),
    (r"short[-\s]?term\s+borrowings?", "short_term_borrowings"),
    (r"trading\s+assets?\s*$", "trading_assets"),
    (r"available[-\s]?for[-\s]?sale\s+securities?", "investment_securities_afs"),
    (r"held[-\s]?to[-\s]?maturity\s+securities?", "investment_securities_htm"),
    (r"cash\s+and\s+(?:due\s+from|cash\s+equiv)", "cash_due_from_banks"),
    (r"tier\s*1\s+(?:risk[-\s]?based\s+)?capital\s+ratio", "tier1_capital_ratio"),
    (r"(?:total\s+)?(?:risk[-\s]?based\s+)?capital\s+ratio\s*$", "total_capital_ratio"),
    (r"tier\s*1\s+leverage\s+ratio", "tier1_leverage_ratio"),
    (r"(?:cet1|common\s+equity\s+tier\s*1)\s+(?:capital\s+)?ratio", "cet1_ratio"),
    (r"risk[-\s]?weighted\s+assets?", "rwa"),
    (r"tangible\s+book\s+value\s+per\s+(?:common\s+)?share", "tbvps"),
    (r"book\s+value\s+per\s+(?:common\s+)?share", "bvps"),
    (r"return\s+on\s+(?:average\s+)?(?:common\s+)?equity", "roe"),
    (r"return\s+on\s+(?:average\s+)?(?:total\s+)?assets?", "roa"),
    (r"return\s+on\s+(?:average\s+)?tangible\s+(?:common\s+)?equity", "rotce"),
    (r"net\s+interest\s+margin", "nim"),
    (r"efficiency\s+ratio|overhead\s+ratio", "efficiency_ratio"),
    (r"net\s+charge[-\s]?off\s+(?:ratio|rate)\s*$", "net_charge_off_rate"),
    (r"net\s+charge[-\s]?offs?\s*$", "net_charge_offs"),
    (r"nonperforming\s+(?:loans?|assets?)\s*$", "npl"),
    (r"\bnpl\b.*total\b", "npl"),
    (r"level\s+1\s+(?:fair\s+value\s+)?assets?", "level1_assets"),
    (r"level\s+2\s+(?:fair\s+value\s+)?assets?", "level2_assets"),
    (r"level\s+3\s+(?:fair\s+value\s+)?assets?", "level3_assets"),
    (r"value(?:\s+at)?\s+risk|\bvar\b", "var_value"),
]
COMPILED = [(re.compile(p, re.I | re.M), col) for p, col in LABEL_PATTERNS]

COLUMNS = [
    "ticker", "fiscal_year", "filing_date", "accession_number", "source_file",
    # Income
    "total_net_revenue", "net_interest_income", "noninterest_revenue",
    "total_noninterest_expense", "pre_provision_profit", "provision_credit_losses",
    "income_before_tax", "net_income", "eps_basic", "eps_diluted", "efficiency_ratio",
    # Balance sheet
    "total_assets", "total_liabilities", "total_equity",
    "total_loans_gross", "allowance_loan_losses", "net_loans",
    "total_deposits", "long_term_debt", "short_term_borrowings",
    "trading_assets", "investment_securities_afs", "investment_securities_htm",
    "cash_due_from_banks",
    # Capital
    "tier1_capital", "tier1_capital_ratio", "total_capital_ratio",
    "tier1_leverage_ratio", "cet1_ratio", "rwa", "tbvps", "bvps",
    # Profitability
    "roe", "roa", "rotce", "nim",
    # Credit quality
    "net_charge_offs", "net_charge_off_rate", "npl", "npa", "npl_ratio",
    "allowance_coverage_ratio",
    # Trading / risk
    "derivatives_notional", "level1_assets", "level2_assets", "level3_assets",
    "level3_pct_assets", "var_value",
]

# Columns where values are already percentages or per-share → don't scale to millions
NO_SCALE_COLS = {
    "eps_basic", "eps_diluted", "tier1_capital_ratio", "total_capital_ratio",
    "tier1_leverage_ratio", "cet1_ratio", "roe", "roa", "rotce", "nim",
    "efficiency_ratio", "net_charge_off_rate", "npl_ratio", "tbvps", "bvps",
    "level3_pct_assets", "allowance_coverage_ratio", "var_value",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_number(text):
    if not text:
        return None
    t = str(text).strip()
    if t in ('', '—', '–', '-', 'N/A', 'NM', 'n/a', 'nm', '*', '**', '†', '‡', 'na'):
        return None
    negative = (t.startswith('(') and ')' in t) or t.startswith('−')
    t = re.sub(r'[\$,\s]', '', t)
    t = re.sub(r'[()%]', '', t)
    t = re.sub(r'[–—−]', '', t)
    if not t:
        return None
    try:
        v = float(t)
        return -abs(v) if negative else v
    except ValueError:
        return None


def detect_scale(text):
    """Return multiplier so that value * multiplier = millions USD."""
    if not text:
        return 1.0
    t = text.lower()
    if re.search(r'in\s+billions', t):
        return 1000.0
    if re.search(r'in\s+thousands', t):
        return 0.001
    if re.search(r'in\s+millions', t):
        return 1.0
    return 1.0  # large-bank default


def load_html(filepath):
    content = filepath.read_bytes()
    for enc in ('utf-8', 'latin-1', 'cp1252', 'ascii'):
        try:
            html = content.decode(enc, errors='replace')
            break
        except Exception:
            continue
    # Strip SGML wrapper around older EDGAR files
    stripped = html.lstrip()
    if stripped.startswith('<DOCUMENT>') or stripped.startswith('<SEC-DOCUMENT>'):
        m = re.search(r'<(?:HTML|html)', html)
        if m:
            html = html[m.start():]
        elif '<BODY' in html.upper() or '<TABLE' in html.upper():
            html = '<html><body>' + html[html.upper().find('<BODY'):]
    return html


# ── XBRL extraction ────────────────────────────────────────────────────────────

def parse_xbrl_contexts(html):
    """Return (duration_ctx_ids, instant_ctx_ids) for the most recent year with no segment dims."""
    ctxs_raw = re.findall(
        r'<xbrli:context\s+id="([^"]+)"[^>]*>(.*?)</xbrli:context>',
        html, re.DOTALL | re.IGNORECASE)

    # Build: end_date → list of (ctx_id, has_dimensions)
    by_end = defaultdict(list)
    for ctx_id, body in ctxs_raw:
        has_dim = bool(re.search(r'explicitMember|typedMember', body, re.I))
        start_m = re.search(r'<xbrli:startDate>([^<]+)</xbrli:startDate>', body, re.I)
        end_m   = re.search(r'<xbrli:endDate>([^<]+)</xbrli:endDate>', body, re.I)
        inst_m  = re.search(r'<xbrli:instant>([^<]+)</xbrli:instant>', body, re.I)
        if start_m and end_m:
            start, end = start_m.group(1).strip(), end_m.group(1).strip()
            try:
                from datetime import date as ddate
                s = ddate.fromisoformat(start)
                e = ddate.fromisoformat(end)
                span = (e - s).days
                if 350 <= span <= 400:  # full fiscal year
                    by_end[end].append(('duration', ctx_id, has_dim))
            except Exception:
                pass
        elif inst_m:
            d = inst_m.group(1).strip()
            by_end[d].append(('instant', ctx_id, has_dim))

    if not by_end:
        return set(), set()

    # Pick the most recent year (latest end date with both duration + instant entries)
    best_end = max(by_end.keys())
    duration_ids = {cid for kind, cid, hd in by_end[best_end]
                    if kind == 'duration' and not hd}
    instant_ids  = {cid for kind, cid, hd in by_end[best_end]
                    if kind == 'instant'  and not hd}

    # Fallback: if no dimension-free contexts found, include segmented ones too
    if not duration_ids:
        duration_ids = {cid for kind, cid, hd in by_end[best_end] if kind == 'duration'}
    if not instant_ids:
        instant_ids = {cid for kind, cid, hd in by_end[best_end] if kind == 'instant'}

    return duration_ids, instant_ids


def extract_xbrl(html):
    """Extract financial metrics from inline XBRL tags."""
    duration_ids, instant_ids = parse_xbrl_contexts(html)
    all_target = duration_ids | instant_ids
    result = {}

    # Parse all ix:nonFraction tags
    # Match full opening tag content
    for m in re.finditer(r'<ix:nonFraction([^>]+)>([^<]*)<', html, re.IGNORECASE):
        attrs_raw = m.group(1)
        val_text  = m.group(2).strip()

        name_m = re.search(r'\bname="([^"]+)"', attrs_raw, re.I)
        ctx_m  = re.search(r'\bcontextRef="([^"]+)"', attrs_raw, re.I)
        if not name_m or not ctx_m:
            continue

        name    = name_m.group(1).lower()
        ctx_id  = ctx_m.group(1)
        col     = XBRL_CONCEPTS.get(name)
        if not col:
            continue
        if result.get(col) is not None:
            continue  # first consolidated occurrence wins

        # Filter to target contexts if we have them
        if all_target and ctx_id not in all_target:
            continue

        val = parse_number(val_text)
        if val is None:
            continue

        # Determine scale
        scale_m = re.search(r'\bscale="([^"]+)"', attrs_raw, re.I)
        dec_m   = re.search(r'\bdecimals="([^"]+)"', attrs_raw, re.I)
        sign_m  = re.search(r'\bsign="([^"]+)"', attrs_raw, re.I)

        if col not in NO_SCALE_COLS:
            if scale_m:
                power = int(scale_m.group(1))
                # displayed_value * 10^power = actual dollars; / 1e6 = millions
                val = val * (10 ** power) / 1e6
            elif dec_m:
                d = int(dec_m.group(1))
                if d == -3:
                    val /= 1000     # thousands → millions
                elif d == -9:
                    val *= 1000     # billions → millions
                elif d >= 0:
                    pass            # small number (EPS, ratio), keep as-is
                # d == -6: already millions

        if sign_m and sign_m.group(1) == '-':
            val = -abs(val)

        result[col] = val

    return result


# ── Table extraction ───────────────────────────────────────────────────────────

def extract_from_tables(soup):
    result = {}
    tables = soup.find_all('table')

    for table in tables:
        rows = table.find_all('tr')
        if len(rows) < 3:
            continue

        # Detect scale from caption or preceding text sibling
        caption_text = ''
        cap = table.find('caption')
        if cap:
            caption_text = cap.get_text()
        prev = table.find_previous_sibling()
        surrounding = caption_text
        if prev:
            surrounding += prev.get_text()[:300]
        # Also scan the first few rows for "in millions"
        for r in rows[:4]:
            surrounding += r.get_text()[:100]
        scale = detect_scale(surrounding)

        # Count numeric cells to filter non-financial tables
        all_text = [c.get_text(strip=True)
                    for r in rows for c in r.find_all(['td','th'])]
        num_cells = sum(1 for t in all_text if re.match(r'^[\$\(]?[\d,]+\.?\d*\)?$', t))
        if num_cells < 4 and len(rows) < 8:
            continue

        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2:
                continue

            label = cells[0].get_text(separator=' ', strip=True)
            if not label or len(label) > 120:
                continue
            # Skip rows that are purely numbers
            if re.match(r'^[\d\s,\.\$\(\)%\-–—]+$', label):
                continue

            matched_col = None
            for pattern, col in COMPILED:
                if pattern.search(label):
                    matched_col = col
                    break
            if not matched_col or result.get(matched_col) is not None:
                continue

            # Try each subsequent cell for a parseable number
            for cell in cells[1:5]:
                cell_text = cell.get_text(strip=True)
                val = parse_number(cell_text)
                if val is not None and val != 0:
                    if matched_col not in NO_SCALE_COLS:
                        val = val * scale
                    result[matched_col] = val
                    break

    return result


# ── Text (prose) extraction ────────────────────────────────────────────────────

TEXT_PATTERNS = [
    (r"return\s+on\s+(?:average\s+)?(?:common\s+)?equity[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "roe"),
    (r"\bROE\b[^\d\(]{0,10}([\d]+\.?\d*)\s*%", "roe"),
    (r"return\s+on\s+(?:average\s+)?(?:total\s+)?assets?[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "roa"),
    (r"\bROA\b[^\d\(]{0,10}([\d]+\.?\d*)\s*%", "roa"),
    (r"return\s+on\s+(?:average\s+)?tangible\s+(?:common\s+)?equity[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "rotce"),
    (r"\bROTCE\b[^\d\(]{0,10}([\d]+\.?\d*)\s*%", "rotce"),
    (r"net\s+interest\s+margin[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "nim"),
    (r"efficiency\s+ratio[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "efficiency_ratio"),
    (r"(?:cet1|common\s+equity\s+tier\s*1)\s+(?:capital\s+)?ratio[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "cet1_ratio"),
    (r"tier\s*1\s+(?:capital\s+)?ratio[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "tier1_capital_ratio"),
    (r"leverage\s+ratio[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "tier1_leverage_ratio"),
    (r"net\s+charge[-\s]?off\s+(?:ratio|rate)[^\d\(]{0,20}([\d]+\.?\d*)\s*%", "net_charge_off_rate"),
    (r"book\s+value\s+per\s+(?:common\s+)?share[^\d\(]{0,10}\$?([\d]+\.?\d*)", "bvps"),
    (r"tangible\s+book\s+value\s+per\s+(?:common\s+)?share[^\d\(]{0,10}\$?([\d]+\.?\d*)", "tbvps"),
]
TEXT_COMPILED = [(re.compile(p, re.I), col) for p, col in TEXT_PATTERNS]


def extract_from_text(text):
    result = {}
    for pattern, col in TEXT_COMPILED:
        if result.get(col) is not None:
            continue
        m = pattern.search(text)
        if m:
            try:
                result[col] = float(m.group(1))
            except (ValueError, IndexError):
                pass
    return result


# ── Main filing parser ─────────────────────────────────────────────────────────

def parse_filing(filepath, ticker):
    result = {col: None for col in COLUMNS}
    result['ticker'] = ticker

    fname  = filepath.stem  # e.g. "2024-02-16_0000019617-24-000225"
    parts  = fname.split('_', 1)
    filing_date = parts[0] if parts else ''
    accession   = parts[1] if len(parts) > 1 else ''
    result['filing_date']      = filing_date
    result['accession_number'] = accession
    result['source_file']      = filepath.name

    if filing_date and len(filing_date) >= 7:
        fy    = int(filing_date[:4])
        month = int(filing_date[5:7])
        result['fiscal_year'] = fy - 1 if month <= 5 else fy

    try:
        html = load_html(filepath)
    except Exception as e:
        return result

    # ── XBRL extraction (modern filings ~2009+) ──
    is_xbrl = ('xbrli:context' in html[:300000] or
                'ix:nonFraction' in html[:100000])
    if is_xbrl:
        for k, v in extract_xbrl(html).items():
            if v is not None and result.get(k) is None:
                result[k] = v

    # ── Table extraction (all filings) ──
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        soup = BeautifulSoup(html, 'html.parser')

    for k, v in extract_from_tables(soup).items():
        if v is not None and result.get(k) is None:
            result[k] = v

    # ── Text extraction (ratios in prose) ──
    page_text = soup.get_text(separator=' ')
    for k, v in extract_from_text(page_text).items():
        if v is not None and result.get(k) is None:
            result[k] = v

    # ── Derived fields ──
    ni  = result['net_interest_income']
    nir = result['noninterest_revenue']
    if ni and nir and not result['total_net_revenue']:
        result['total_net_revenue'] = ni + nir

    rev = result['total_net_revenue']
    exp = result['total_noninterest_expense']
    if rev and exp and not result['pre_provision_profit']:
        result['pre_provision_profit'] = rev - exp

    if result['total_noninterest_expense'] and rev and rev > 0 and not result['efficiency_ratio']:
        result['efficiency_ratio'] = result['total_noninterest_expense'] / rev * 100

    if result['net_charge_offs'] and result['net_loans'] and result['net_loans'] > 0:
        if not result['net_charge_off_rate']:
            result['net_charge_off_rate'] = result['net_charge_offs'] / result['net_loans'] * 100

    if result['allowance_loan_losses'] and result['npl'] and result['npl'] > 0:
        if not result['allowance_coverage_ratio']:
            result['allowance_coverage_ratio'] = (
                result['allowance_loan_losses'] / result['npl'] * 100)

    if result['level3_assets'] and result['total_assets'] and result['total_assets'] > 0:
        if not result['level3_pct_assets']:
            result['level3_pct_assets'] = (
                result['level3_assets'] / result['total_assets'] * 100)

    if result['npl'] and result['net_loans'] and result['net_loans'] > 0:
        if not result['npl_ratio']:
            result['npl_ratio'] = result['npl'] / result['net_loans'] * 100

    return result


# ── Sanity filter: remove implausible values ───────────────────────────────────

def sanity_check(row):
    """Zero out values that are clearly wrong (unit conversion errors, etc.)."""
    # Total assets for these banks should be >$10B (10,000M) and <$5T (5,000,000M)
    for col in ('total_assets', 'total_liabilities', 'total_deposits', 'net_loans'):
        v = row.get(col)
        if v is not None and (abs(v) < 100 or abs(v) > 5_000_000):
            row[col] = None

    # Net income: -$100B to +$100B
    v = row.get('net_income')
    if v is not None and (abs(v) > 100_000 or (v > 0 and v < 0.01)):
        row['net_income'] = None

    # EPS: -$50 to +$500 (per share)
    for col in ('eps_basic', 'eps_diluted'):
        v = row.get(col)
        if v is not None and (abs(v) > 500 or (v != 0 and abs(v) < 0.01)):
            row[col] = None

    # Ratios: 0–200%
    for col in ('tier1_capital_ratio','total_capital_ratio','tier1_leverage_ratio',
                'cet1_ratio','roe','roa','rotce','nim','efficiency_ratio',
                'net_charge_off_rate','npl_ratio','level3_pct_assets'):
        v = row.get(col)
        if v is not None and (v < -5 or v > 200):
            row[col] = None

    return row


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    all_rows = []
    metric_cols = [c for c in COLUMNS if c not in
                   ('ticker','fiscal_year','filing_date','accession_number','source_file')]

    for ticker in TICKERS:
        ticker_dir = DATA_DIR / ticker / "10-K"
        if not ticker_dir.exists():
            print(f"  {ticker}: directory not found, skipping")
            continue

        filings = sorted(
            list(ticker_dir.glob("*.htm")) +
            list(ticker_dir.glob("*.html")) +
            list(ticker_dir.glob("*.txt"))
        )
        if not filings:
            print(f"  {ticker}: no files found")
            continue

        print(f"\nProcessing {ticker} ({len(filings)} filings) ...", flush=True)
        for fp in tqdm(filings, desc=f"  {ticker}", leave=False):
            row = parse_filing(fp, ticker)
            row = sanity_check(row)
            all_rows.append(row)

    if not all_rows:
        print("No data extracted!")
        return

    df = pd.DataFrame(all_rows, columns=COLUMNS)
    df = df.sort_values(['ticker', 'fiscal_year'])

    # Deduplicate fiscal years: keep the row with the most non-null values
    df['_nn'] = df[metric_cols].notna().sum(axis=1)
    df = (df.sort_values('_nn', ascending=False)
            .drop_duplicates(['ticker', 'fiscal_year'])
            .sort_values(['ticker', 'fiscal_year'])
            .drop(columns='_nn'))

    out = OUT_DIR / "bank_financials.csv"
    df.to_csv(out, index=False)
    print(f"\n✓ Saved {len(df)} rows → {out}\n")

    # ── Summary table ──
    print("=" * 90)
    print("EXTRACTION QUALITY SUMMARY")
    print("=" * 90)
    print(f"{'Ticker':<8} {'Yrs':<26} {'Fields ≥50%':>11}  {'Fields <50%':>11}  "
          f"{'Key coverage':}")
    print("-" * 90)
    key = ['total_assets','net_income','total_deposits','net_loans',
           'tier1_capital_ratio','cet1_ratio','roe','nim']
    for t in TICKERS:
        sub = df[df['ticker'] == t]
        if sub.empty:
            print(f"{t:<8} no data")
            continue
        yr_range = f"{sub['fiscal_year'].min()}-{sub['fiscal_year'].max()} ({len(sub)}yr)"
        good = sum(1 for c in metric_cols if sub[c].notna().mean() >= 0.5)
        poor = sum(1 for c in metric_cols if sub[c].notna().mean() <  0.5)
        key_cov = ''.join('▓' if sub[c].notna().mean() >= 0.5 else '░' for c in key)
        print(f"{t:<8} {yr_range:<26} {good:>11}  {poor:>11}  {key_cov}")

    print("\nKey column legend: total_assets | net_income | deposits | loans | "
          "tier1_ratio | cet1 | roe | nim")
    print("\nNOTE: Pre-2009 filings lack inline XBRL — lower extraction quality "
          "for those years is expected and not a bug.")
    print("NOTE: CET1 ratio only required post-2013; nulls before 2014 are correct.")

    # ── Per-column coverage ──
    print("\nTop metrics coverage across all banks/years:")
    for col in ['total_assets','net_income','total_deposits','net_loans','total_equity',
                'tier1_capital_ratio','cet1_ratio','roe','roa','nim',
                'provision_credit_losses','net_charge_offs','npl','level3_assets']:
        pct = df[col].notna().mean() * 100
        bar = '█' * int(pct / 5)
        print(f"  {col:<38} {pct:5.1f}%  {bar}")


if __name__ == '__main__':
    main()
