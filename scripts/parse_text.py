#!/usr/bin/env python3
"""
Phase 2: Extract text sections, crisis-language counts, and MD&A sentiment
from SEC 10-K filings for 13 US banks.

Outputs:
  data/processed/crisis_language.csv   – per (ticker, year, term) occurrence counts
  data/processed/mda_sentiment.csv     – per (ticker, year) sentiment + opening excerpt
"""

import re
import sys
from pathlib import Path
from collections import Counter, defaultdict

import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE    = Path(__file__).resolve().parent.parent
DATA    = BASE / "data"
OUT_DIR = DATA / "processed"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TICKERS = ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","STT","BK","SCHW"]

# ── Crisis-vocabulary terms ──────────────────────────────────────────────────
CRISIS_TERMS = {
    # Crisis / losses
    "subprime":             r"\bsubprime\b",
    "mortgage-backed":      r"\bmortgage[- ]backed\b",
    "CDO":                  r"\bCDO\b|\bcollateralized\s+debt\s+obligation",
    "credit default swap":  r"\bcredit\s+default\s+swap",
    "leveraged loan":       r"\bleveraged\s+loan",
    "structured product":   r"\bstructured\s+product",
    "off-balance sheet":    r"\boff[- ]balance[- ]sheet",
    "write-down":           r"\bwrite[- ]down",
    "write-off":            r"\bwrite[- ]off",
    "impairment":           r"\bimpairment",
    "foreclosure":          r"\bforeclosure",
    "delinquency":          r"\bdelinquenc",
    "default":              r"\bdefault\b",
    "nonperforming":        r"\bnon[- ]?performing",
    "charge-off":           r"\bcharge[- ]off",
    # Risk
    "liquidity risk":       r"\bliquidity\s+risk",
    "funding risk":         r"\bfunding\s+risk",
    "counterparty risk":    r"\bcounterparty\s+risk",
    "market risk":          r"\bmarket\s+risk",
    "concentration risk":   r"\bconcentration\s+risk",
    "systemic risk":        r"\bsystemic\s+risk",
    "stress test":          r"\bstress\s+test",
    "Basel":                r"\bBasel\b",
    "Dodd-Frank":           r"\bDodd[- ]Frank",
    "Volcker Rule":         r"\bVolcker\s+Rule",
    "regulatory capital":   r"\bregulatory\s+capital",
    # Macro
    "recession":            r"\brecession",
    "unemployment":         r"\bunemployment",
    "housing market":       r"\bhousing\s+market",
    "interest rate risk":   r"\binterest\s+rate\s+risk",
    "Federal Reserve":      r"\bFederal\s+Reserve",
    "yield curve":          r"\byield\s+curve",
    "credit spread":        r"\bcredit\s+spread",
}
CRISIS_COMPILED = {term: re.compile(pat, re.I) for term, pat in CRISIS_TERMS.items()}

# ── Sentiment word lists (financial domain) ──────────────────────────────────
POSITIVE_WORDS = {
    "strong","strength","growth","grew","increase","increased","record","improvement",
    "improved","profitable","profit","gain","gains","expand","expanded","expansion",
    "recovery","positive","favorable","benefit","benefits","robust","solid","success",
    "successful","exceed","exceeded","outperform","efficient","higher","better","stable",
    "stability","resilient","resilience","confidence","confident","opportunity",
    "opportunities","momentum","diversified","diversification","capital","well-capitalized",
}
NEGATIVE_WORDS = {
    "loss","losses","decline","declined","decrease","decreased","deteriorated",
    "deterioration","adverse","adversely","risk","risks","concern","concerns","weak",
    "weakness","volatile","volatility","uncertain","uncertainty","challenge","challenges",
    "difficult","difficulty","pressure","pressures","headwind","headwinds","write-down",
    "write-off","impairment","nonperforming","default","delinquent","foreclosure",
    "recession","downturn","disruption","disruptions","stress","stressed","exposure",
    "elevated","severity","crisis","lower","worse","negative","insufficient",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_html(filepath):
    content = filepath.read_bytes()
    for enc in ('utf-8', 'latin-1', 'cp1252', 'ascii'):
        try:
            return content.decode(enc, errors='replace')
        except Exception:
            continue
    return content.decode('utf-8', errors='replace')


def get_soup(filepath):
    html = load_html(filepath)
    stripped = html.lstrip()
    if stripped.startswith('<DOCUMENT>') or stripped.startswith('<SEC-DOCUMENT>'):
        m = re.search(r'<(?:HTML|html)', html)
        if m:
            html = html[m.start():]
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception:
        return BeautifulSoup(html, 'html.parser')


def extract_section(soup, item_pattern, next_items=None):
    """
    Find an Item section (e.g. 'Item 1A') in a filing and return its text.
    Tries all matches and returns the longest one (skips table-of-contents stubs).
    Returns '' if not found.
    """
    if next_items is None:
        next_items = [r'item\s+\d+[a-z]?\.?\s', r'part\s+[ivx]+']

    full_text = soup.get_text(separator='\n')

    # Strategy 1: find ALL occurrences and pick the longest extracted section
    start_pat = re.compile(r'(?:^|\n)[\s\*]*' + item_pattern, re.I | re.M)
    end_pat   = re.compile(
        r'(?:^|\n)[\s\*]*(?:' + '|'.join(next_items) + ')', re.I | re.M
    )

    best = ''
    for m_start in start_pat.finditer(full_text):
        text_from = full_text[m_start.start():]
        m_end = end_pat.search(text_from, len(m_start.group()))
        candidate = text_from[:m_end.start()].strip() if m_end else text_from[:50000].strip()
        if len(candidate) > len(best):
            best = candidate
        if len(best) > 5000:   # found a real section, stop searching
            break

    if best:
        return best

    # Strategy 2: look in heading elements
    for heading in soup.find_all(['h1','h2','h3','h4','b','strong','p']):
        htxt = heading.get_text(strip=True)
        if re.search(item_pattern, htxt, re.I) and len(htxt) < 200:
            parts = []
            for sib in heading.find_next_siblings():
                sib_txt = sib.get_text(strip=True)
                if re.search(r'item\s+\d+[a-z]?\.?\s', sib_txt[:60], re.I):
                    break
                parts.append(sib.get_text(separator=' '))
                if len(' '.join(parts)) > 50000:
                    break
            candidate = ' '.join(parts).strip()
            if len(candidate) > len(best):
                best = candidate

    return best


def count_crisis_terms(text):
    counts = {}
    for term, pat in CRISIS_COMPILED.items():
        counts[term] = len(pat.findall(text))
    return counts


def compute_sentiment(text):
    """
    Returns (sentiment_score, opening_excerpt).
    sentiment_score = (positive - negative) / total_words  (capped [-1, 1]).
    Uses first 300 words of Item 7 for the excerpt.
    """
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return None, ''
    pos   = sum(1 for w in words if w in POSITIVE_WORDS)
    neg   = sum(1 for w in words if w in NEGATIVE_WORDS)
    total = len(words)
    score = (pos - neg) / total if total else None

    # First 300 words as opening excerpt
    excerpt = ' '.join(words[:300])
    return score, excerpt


def parse_filing_date(fname):
    parts = fname.split('_', 1)
    filing_date = parts[0] if parts else ''
    if filing_date and len(filing_date) >= 7:
        fy    = int(filing_date[:4])
        month = int(filing_date[5:7])
        return filing_date, fy - 1 if month <= 5 else fy
    return filing_date, None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    crisis_rows   = []   # (ticker, year, term, count)
    sentiment_rows = []  # (ticker, year, score, excerpt)

    stats = defaultdict(lambda: {'files': 0, '1a': 0, '7': 0, 'neither': 0})

    for ticker in TICKERS:
        ticker_dir = DATA / ticker / "10-K"
        if not ticker_dir.exists():
            print(f"  {ticker}: directory not found")
            continue

        filings = sorted(
            list(ticker_dir.glob("*.htm")) +
            list(ticker_dir.glob("*.html")) +
            list(ticker_dir.glob("*.txt"))
        )
        if not filings:
            print(f"  {ticker}: no files")
            continue

        print(f"\nProcessing {ticker} ({len(filings)} filings)...", flush=True)

        # Track best extraction per fiscal year (in case of duplicates)
        best = {}   # fiscal_year → {'crisis': ..., 'sentiment_score': ..., 'excerpt': ..., 'has_1a': bool, 'has_7': bool}

        for fp in tqdm(filings, desc=f"  {ticker}", leave=False):
            filing_date, fy = parse_filing_date(fp.stem)
            if fy is None:
                continue

            try:
                soup = get_soup(fp)
            except Exception as e:
                continue

            # --- Item 1A ---
            text_1a = extract_section(soup,
                item_pattern=r'item\s+1a[\.\s:]+risk\s+factors',
                next_items=[r'item\s+1b', r'item\s+2[\.\s]', r'part\s+[ivx]+'])
            if not text_1a:
                text_1a = extract_section(soup,
                    item_pattern=r'item\s+1a',
                    next_items=[r'item\s+1b', r'item\s+2[\.\s]'])

            # --- Item 7 ---
            text_7 = extract_section(soup,
                item_pattern=r'item\s+7[\.\s:]+management',
                next_items=[r'item\s+7a', r'item\s+8[\.\s]', r'part\s+[ivx]+'])
            if not text_7:
                text_7 = extract_section(soup,
                    item_pattern=r'item\s+7[\.\s]',
                    next_items=[r'item\s+7a', r'item\s+8[\.\s]'])

            has_1a = len(text_1a) > 200
            has_7  = len(text_7)  > 200
            stats[ticker]['files'] += 1
            if has_1a: stats[ticker]['1a'] += 1
            if has_7:  stats[ticker]['7']  += 1
            if not has_1a and not has_7:
                stats[ticker]['neither'] += 1

            combined = text_1a + ' ' + text_7
            crisis_counts = count_crisis_terms(combined)
            score, excerpt = compute_sentiment(text_7 if has_7 else combined)

            prev = best.get(fy)
            quality = sum(1 for v in crisis_counts.values() if v > 0)
            if prev is None or quality > prev.get('quality', 0):
                best[fy] = {
                    'crisis': crisis_counts,
                    'score':  score,
                    'excerpt': excerpt,
                    'quality': quality,
                    'has_1a': has_1a,
                    'has_7':  has_7,
                }

        # Flatten into rows
        for fy, data in sorted(best.items()):
            for term, count in data['crisis'].items():
                crisis_rows.append({
                    'ticker': ticker,
                    'year':   fy,
                    'term':   term,
                    'count':  count,
                })
            sentiment_rows.append({
                'ticker':              ticker,
                'year':                fy,
                'sentiment_score':     data['score'],
                'opening_paragraph_excerpt': data['excerpt'],
            })

    # --- Save ---
    crisis_df = pd.DataFrame(crisis_rows)
    sent_df   = pd.DataFrame(sentiment_rows)

    crisis_df.to_csv(OUT_DIR / 'crisis_language.csv', index=False)
    sent_df.to_csv(OUT_DIR / 'mda_sentiment.csv', index=False)

    print(f"\n✓ crisis_language.csv: {len(crisis_df)} rows")
    print(f"✓ mda_sentiment.csv:   {len(sent_df)} rows\n")

    # --- Coverage report ---
    print("=" * 70)
    print("COVERAGE REPORT")
    print("=" * 70)
    print(f"{'Ticker':<8} {'Files':>6} {'Item 1A':>8} {'Item 7':>7} {'Neither':>8} {'Terms w/ any hit':>17}")
    print("-" * 70)
    for ticker in TICKERS:
        s = stats[ticker]
        if s['files'] == 0:
            print(f"{ticker:<8}  --")
            continue
        sub = crisis_df[crisis_df['ticker'] == ticker]
        terms_hit = (sub.groupby('term')['count'].sum() > 0).sum() if not sub.empty else 0
        print(f"{ticker:<8} {s['files']:>6} {s['1a']:>8} {s['7']:>7} {s['neither']:>8} {terms_hit:>17}")

    print("\nFilings with neither Item 1A nor Item 7 (likely pre-XBRL stubs or partial docs):")
    for ticker in TICKERS:
        if stats[ticker]['neither'] > 0:
            print(f"  {ticker}: {stats[ticker]['neither']} files")


if __name__ == '__main__':
    main()
