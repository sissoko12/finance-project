#!/usr/bin/env python3
"""
Phase 3: Build dashboard/index.html from processed CSVs.
Reads:
  data/processed/bank_financials.csv
  data/processed/crisis_language.csv   (optional)
  data/processed/mda_sentiment.csv     (optional)
  data/news/news.json                  (optional)
Writes:
  dashboard/index.html
"""

import json
import math
import sys
from pathlib import Path
from collections import defaultdict

import pandas as pd

BASE      = Path(__file__).resolve().parent.parent
DATA      = BASE / "data"
PROCESSED = DATA / "processed"
OUT_FILE  = Path(__file__).parent / "index.html"

TICKERS = ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","STT","BK","SCHW"]
BANK_NAMES = {
    "JPM":  "JPMorgan Chase", "BAC": "Bank of America",
    "WFC":  "Wells Fargo",    "C":   "Citigroup",
    "GS":   "Goldman Sachs",  "MS":  "Morgan Stanley",
    "USB":  "US Bancorp",     "PNC": "PNC Financial",
    "TFC":  "Truist Financial","COF": "Capital One",
    "STT":  "State Street",   "BK":  "BNY Mellon",
    "SCHW": "Charles Schwab",
}

METRIC_LABELS = {
    "total_assets":            "Total Assets ($M)",
    "net_income":              "Net Income ($M)",
    "total_net_revenue":       "Net Revenue ($M)",
    "net_interest_income":     "Net Interest Income ($M)",
    "noninterest_revenue":     "Noninterest Revenue ($M)",
    "total_noninterest_expense":"Noninterest Expense ($M)",
    "provision_credit_losses": "Provision for Credit Losses ($M)",
    "total_deposits":          "Total Deposits ($M)",
    "net_loans":               "Net Loans ($M)",
    "total_loans_gross":       "Total Loans Gross ($M)",
    "allowance_loan_losses":   "Allowance for Loan Losses ($M)",
    "total_equity":            "Stockholders Equity ($M)",
    "tier1_capital_ratio":     "Tier 1 Capital Ratio (%)",
    "total_capital_ratio":     "Total Capital Ratio (%)",
    "tier1_leverage_ratio":    "Tier 1 Leverage Ratio (%)",
    "cet1_ratio":              "CET1 Ratio (%)",
    "rwa":                     "Risk-Weighted Assets ($M)",
    "roe":                     "Return on Equity (%)",
    "roa":                     "Return on Assets (%)",
    "rotce":                   "Return on Tangible Equity (%)",
    "nim":                     "Net Interest Margin (%)",
    "efficiency_ratio":        "Efficiency Ratio (%)",
    "net_charge_offs":         "Net Charge-offs ($M)",
    "net_charge_off_rate":     "Net Charge-off Rate (%)",
    "npl":                     "Nonperforming Loans ($M)",
    "npl_ratio":               "NPL Ratio (%)",
    "allowance_coverage_ratio":"Allowance Coverage Ratio (%)",
    "level3_assets":           "Level 3 Assets ($M)",
    "eps_diluted":             "EPS Diluted ($)",
    "bvps":                    "Book Value Per Share ($)",
    "tbvps":                   "Tangible Book Value Per Share ($)",
}

# ── Load data ──────────────────────────────────────────────────────────────────

def load_financials():
    fp = PROCESSED / "bank_financials.csv"
    if not fp.exists():
        print("WARNING: bank_financials.csv not found")
        return {}
    df = pd.read_csv(fp)
    df = df.dropna(subset=['fiscal_year'])
    df['fiscal_year'] = df['fiscal_year'].astype(int)
    result = {}
    for ticker, grp in df.groupby('ticker'):
        rows = []
        for _, row in grp.sort_values('fiscal_year').iterrows():
            d = {'year': int(row['fiscal_year'])}
            for col in df.columns:
                if col in ('ticker', 'fiscal_year', 'filing_date',
                           'accession_number', 'source_file'):
                    continue
                v = row[col]
                d[col] = None if pd.isna(v) else float(v)
            rows.append(d)
        result[ticker] = rows
    return result


def load_crisis():
    fp = PROCESSED / "crisis_language.csv"
    if not fp.exists():
        print("NOTE: crisis_language.csv not found, crisis charts will be empty")
        return {}
    df = pd.read_csv(fp)
    result = {}
    for ticker, grp in df.groupby('ticker'):
        result[ticker] = {}
        for _, row in grp.iterrows():
            year = int(row['year'])
            if year not in result[ticker]:
                result[ticker][year] = {}
            result[ticker][year][str(row['term'])] = int(row['count'])
    return result


def load_sentiment():
    fp = PROCESSED / "mda_sentiment.csv"
    if not fp.exists():
        return {}
    df = pd.read_csv(fp)
    result = {}
    for ticker, grp in df.groupby('ticker'):
        result[ticker] = {}
        for _, row in grp.iterrows():
            year = int(row['year'])
            score = row.get('sentiment_score')
            result[ticker][year] = {
                'score':   None if pd.isna(score) else float(score),
                'excerpt': str(row.get('opening_paragraph_excerpt', '') or '')[:400],
            }
    return result


def load_news():
    fp = DATA / "news" / "news.json"
    if not fp.exists():
        return {"last_updated": None, "headlines": []}
    try:
        return json.loads(fp.read_text(encoding='utf-8'))
    except Exception:
        return {"last_updated": None, "headlines": []}


# ── Build HTML ─────────────────────────────────────────────────────────────────

def js(obj):
    return json.dumps(obj, ensure_ascii=False)

def build():
    fin    = load_financials()
    crisis = load_crisis()
    sent   = load_sentiment()
    news   = load_news()

    # Collect all years across all banks for the all-banks view
    all_years = sorted({r['year'] for rows in fin.values() for r in rows})

    html = generate_html(fin, crisis, sent, all_years)
    OUT_FILE.write_text(html, encoding='utf-8')
    print(f"✓ Dashboard written → {OUT_FILE}")
    total_rows = sum(len(v) for v in fin.values())
    print(f"  Banks: {len(fin)}, Data rows: {total_rows}, Years: {min(all_years) if all_years else '?'}–{max(all_years) if all_years else '?'}")
    print(f"  Crisis terms: {sum(len(v) for t in crisis.values() for v in t.values())} bank-year-term entries")
    print(f"  News: {len(news.get('headlines', []))} headlines")


def generate_html(fin, crisis, sent, all_years):
    bank_opts = '\n'.join(
        f'<option value="{t}">{BANK_NAMES.get(t, t)} ({t})</option>'
        for t in TICKERS if t in fin
    )
    year_opts_all = '\n'.join(
        f'<option value="{y}">{y}</option>'
        for y in sorted(all_years, reverse=True)
    )
    default_ticker = next((t for t in TICKERS if t in fin), 'JPM')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>US Bank Financial Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
{CSS}
</style>
</head>
<body>

<!-- NAV -->
<nav class="topnav">
  <div class="nav-brand">US Bank Financial Dashboard</div>
  <div class="nav-controls">
    <input type="search" id="search-bar" placeholder="Search ticker or metric…" autocomplete="off"/>
    <div class="nav-tabs">
      <button class="tab-btn active" data-view="bank">Per-Bank</button>
      <button class="tab-btn" data-view="allbanks">All Banks</button>
      <button class="tab-btn" data-view="news">📰 News</button>
      <button class="tab-btn" data-view="methodology">Methodology</button>
    </div>
    <button class="btn-print" onclick="window.print()">Print</button>
  </div>
</nav>

<!-- BANK SELECTOR (visible in bank view) -->
<div class="bank-selector-bar" id="bank-selector-bar">
  <label for="bank-select">Select Bank:</label>
  <select id="bank-select">
    {bank_opts}
  </select>
  <span id="bank-title-label" class="bank-title-label"></span>
</div>

<!-- SEARCH HIGHLIGHT BANNER -->
<div id="search-banner" class="search-banner" style="display:none"></div>

<main>

<!-- ===== BANK VIEW ===== -->
<section id="view-bank" class="view active">

  <!-- KPI Cards -->
  <div class="section-header">KEY METRICS <span id="kpi-year-label" class="muted-label"></span></div>
  <div class="kpi-grid" id="kpi-grid"></div>

  <!-- Charts -->
  <div class="section-header">BALANCE SHEET TRENDS</div>
  <div class="chart-panel" id="panel-assets">
    <div class="chart-panel-header">
      <span>Total Assets &amp; Net Loans Over Time</span>
      <button class="info-btn" data-target="info-assets">ⓘ</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-assets"></canvas></div>
    <div class="info-box" id="info-assets" style="display:none">
      <strong>Total Assets</strong> = All bank assets (loans, securities, cash, etc.)<br>
      <strong>Net Loans</strong> = Gross loans minus allowance for loan losses<br>
      <strong>Source:</strong> Consolidated balance sheet in 10-K filing<br>
      <strong>Why it matters:</strong> Asset growth indicates balance sheet expansion;
      loan growth shows lending appetite relative to risk appetite.<br>
      <em>Shaded regions: 2008–2009 (GFC amber), 2020 (COVID blue)</em>
    </div>
    <div class="data-table-wrap" id="table-assets"></div>
  </div>

  <div class="section-header">INCOME vs CREDIT LOSSES</div>
  <div class="chart-panel" id="panel-income">
    <div class="chart-panel-header">
      <span>Net Income vs Provision for Credit Losses</span>
      <button class="info-btn" data-target="info-income">ⓘ</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-income"></canvas></div>
    <div class="info-box" id="info-income" style="display:none">
      <strong>Net Income</strong> = Bottom-line profit after all expenses and taxes<br>
      <strong>Provision for Credit Losses</strong> = Amount set aside for expected loan losses<br>
      <strong>Source:</strong> Consolidated income statement in 10-K filing<br>
      <strong>Why it matters:</strong> Provision spikes during stress periods;
      the gap between income and provision shows underlying earnings power.<br>
      <em>Shaded regions: 2008–2009 (GFC amber), 2020 (COVID blue)</em>
    </div>
    <div class="data-table-wrap" id="table-income"></div>
  </div>

  <div class="section-header">CAPITAL RATIOS</div>
  <div class="chart-panel" id="panel-capital">
    <div class="chart-panel-header">
      <span>Capital Adequacy Ratios Over Time</span>
      <button class="info-btn" data-target="info-capital">ⓘ</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-capital"></canvas></div>
    <div class="info-box" id="info-capital" style="display:none">
      <strong>Tier 1 Capital Ratio</strong> = Tier 1 Capital / Risk-Weighted Assets<br>
      <strong>CET1 Ratio</strong> = Common Equity Tier 1 Capital / RWA (Basel III, post-2013)<br>
      <strong>Total Capital Ratio</strong> = Total Regulatory Capital / RWA<br>
      <strong>Tier 1 Leverage Ratio</strong> = Tier 1 Capital / Average Total Assets<br>
      <strong>Why it matters:</strong> Higher ratios = more buffer against losses.
      Basel II minimum total capital: 8%. Basel III CET1 minimum: 4.5%.<br>
      <em>Note: CET1 ratio introduced under Basel III; null values before 2013 are correct.</em>
    </div>
    <div class="data-table-wrap" id="table-capital"></div>
  </div>

  <div class="section-header">CREDIT QUALITY</div>
  <div class="chart-panel" id="panel-credit">
    <div class="chart-panel-header">
      <span>Credit Quality Metrics</span>
      <button class="info-btn" data-target="info-credit">ⓘ</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-credit"></canvas></div>
    <div class="info-box" id="info-credit" style="display:none">
      <strong>Net Charge-off Rate</strong> = Net Charge-offs / Net Loans × 100%<br>
      <strong>NPL Ratio</strong> = Nonperforming Loans / Total Loans × 100%<br>
      <strong>Allowance</strong> = Dollar amount reserved for loan losses ($B)<br>
      <strong>Why it matters:</strong> Rising charge-off and NPL ratios signal
      deteriorating loan portfolio quality — key indicators during financial stress.<br>
      <em>Note: Bars (right axis) show Allowance for Loan Losses in $B</em>
    </div>
    <div class="data-table-wrap" id="table-credit"></div>
  </div>

  <div class="section-header">RISK LANGUAGE IN FILINGS</div>
  <div class="chart-panel" id="panel-crisis">
    <div class="chart-panel-header">
      <span>Crisis &amp; Risk Term Frequency (Top Terms)</span>
      <button class="info-btn" data-target="info-crisis">ⓘ</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-crisis"></canvas></div>
    <div class="info-box" id="info-crisis" style="display:none">
      <strong>Method:</strong> Case-insensitive count of each term
      in Item 1A (Risk Factors) + Item 7 (MD&amp;A) of each annual 10-K filing.<br>
      <strong>Terms tracked:</strong> Crisis terms (subprime, CDO, write-down, impairment…),
      risk terms (liquidity risk, stress test, Basel…),
      macro terms (recession, Federal Reserve, yield curve…)<br>
      <strong>Why it matters:</strong> Frequency of risk language often correlates with
      management's assessment of the current environment.
      A spike in "nonperforming" or "impairment" language foreshadowed GFC losses.
    </div>
  </div>

</section>

<!-- ===== ALL-BANKS VIEW ===== -->
<section id="view-allbanks" class="view">
  <div class="allbanks-controls">
    <label>Select Year:
      <select id="year-select-all">
        {year_opts_all}
      </select>
    </label>
    <span class="muted-label">Comparison across all 13 banks for selected year</span>
  </div>

  <div class="section-header">CROSS-BANK COMPARISON</div>
  <div class="compare-grid">
    <div class="chart-panel">
      <div class="chart-panel-header"><span>Return on Equity (%)</span></div>
      <div class="chart-wrap"><canvas id="cmp-roe"></canvas></div>
    </div>
    <div class="chart-panel">
      <div class="chart-panel-header"><span>Total Assets ($B)</span></div>
      <div class="chart-wrap"><canvas id="cmp-assets"></canvas></div>
    </div>
    <div class="chart-panel">
      <div class="chart-panel-header"><span>CET1 / Tier 1 Capital Ratio (%)</span></div>
      <div class="chart-wrap"><canvas id="cmp-cet1"></canvas></div>
    </div>
    <div class="chart-panel">
      <div class="chart-panel-header"><span>Net Charge-off Rate (%)</span></div>
      <div class="chart-wrap"><canvas id="cmp-nco"></canvas></div>
    </div>
  </div>

  <div class="section-header">CRISIS LANGUAGE HEATMAP
    <span class="muted-label" style="font-weight:400; font-size:12px; text-transform:none; letter-spacing:0">
      — Total crisis/risk term occurrences in 10-K filings (hover for detail)
    </span>
  </div>
  <div class="heatmap-container" id="heatmap-container">
    <div class="heatmap-legend">
      <span>Low</span>
      <div class="heatmap-legend-bar"></div>
      <span>High</span>
    </div>
    <div id="heatmap-grid"></div>
  </div>

  <div class="section-header">MD&amp;A SENTIMENT TRENDS</div>
  <div class="chart-panel">
    <div class="chart-panel-header"><span>MD&amp;A Sentiment Score Over Time (all banks)</span></div>
    <div class="chart-wrap"><canvas id="cmp-sentiment"></canvas></div>
    <div style="padding:10px 16px;font-size:12px;color:#666688">
      Sentiment = (positive words − negative words) / total words in MD&amp;A section.
      Positive threshold &gt;0, Negative &lt;0. Values near zero indicate neutral tone.
    </div>
  </div>
</section>

<!-- ===== NEWS VIEW ===== -->
<section id="view-news" class="view">
  <div class="news-header">
    <div class="section-header" style="margin:0">📰 MARKET NEWS</div>
    <div class="news-header-right">
      <span class="news-updated" id="news-updated"></span>
      <button id="news-refresh-btn" class="news-refresh-btn" title="Refresh news">🔄 Refresh</button>
    </div>
  </div>

  <div class="news-search-wrapper">
    <input type="text" id="newsSearchInput" placeholder="Search any company... (e.g. Apple, Tesla, JPMorgan)" autocomplete="off" />
    <div id="newsSearchDropdown" class="search-dropdown hidden"></div>
  </div>

  <div class="news-showing-for" id="news-showing-for"></div>

  <div id="news-list" class="news-list"></div>
</section>

<!-- ===== METHODOLOGY VIEW ===== -->
<section id="view-methodology" class="view methodology-view">
  <div class="section-header">FORMULAS &amp; METHODOLOGY</div>
  <div class="method-grid">
    <div class="method-card">
      <h3>Efficiency Ratio</h3>
      <code>Noninterest Expense / Net Revenue × 100%</code>
      <p>Lower is better. Measures cost to generate each dollar of revenue. Typical range: 50–70% for US banks.</p>
    </div>
    <div class="method-card">
      <h3>Net Interest Margin (NIM)</h3>
      <code>Net Interest Income / Average Earning Assets × 100%</code>
      <p>Core banking profitability metric. Range ~1.5–3.5% for large US banks; Schwab and custody banks tend lower.</p>
    </div>
    <div class="method-card">
      <h3>Return on Equity (ROE)</h3>
      <code>Net Income / Average Stockholders Equity × 100%</code>
      <p>Measures profit generated on shareholders' capital. Target ~10–15% for large banks.</p>
    </div>
    <div class="method-card">
      <h3>Return on Assets (ROA)</h3>
      <code>Net Income / Average Total Assets × 100%</code>
      <p>Measures how efficiently assets generate profit. Well-run US banks typically target ~1%.</p>
    </div>
    <div class="method-card">
      <h3>CET1 Capital Ratio</h3>
      <code>Common Equity Tier 1 / Risk-Weighted Assets × 100%</code>
      <p>Basel III core capital measure (post-2013). US G-SIB minimum ~11.5–12.5%. Higher = stronger capital position.</p>
    </div>
    <div class="method-card">
      <h3>Tier 1 Capital Ratio</h3>
      <code>Tier 1 Capital / Risk-Weighted Assets × 100%</code>
      <p>Regulatory capital divided by risk-adjusted assets. Basel II minimum: 4%, Basel III: 6%.</p>
    </div>
    <div class="method-card">
      <h3>Net Charge-off Rate</h3>
      <code>(Gross Charge-offs − Recoveries) / Average Net Loans × 100%</code>
      <p>Portion of loan portfolio written off as uncollectible. Typically &lt;1% in normal times; spiked 2–4%+ in 2009–2010.</p>
    </div>
    <div class="method-card">
      <h3>NPL Ratio</h3>
      <code>Nonperforming Loans / Total Loans × 100%</code>
      <p>Loans 90+ days past due or on non-accrual. Rose sharply during GFC; key early warning indicator.</p>
    </div>
    <div class="method-card">
      <h3>Allowance Coverage Ratio</h3>
      <code>Allowance for Loan Losses / Nonperforming Loans × 100%</code>
      <p>How much of NPLs are covered by reserves. Above 100% = more than fully covered.</p>
    </div>
    <div class="method-card">
      <h3>Pre-Provision Profit</h3>
      <code>Net Revenue − Total Noninterest Expense</code>
      <p>Earnings before credit loss provisions. Shows underlying business profitability independent of credit cycle.</p>
    </div>
    <div class="method-card">
      <h3>Trend Lines (all charts)</h3>
      <code>slope = (n·Σxy − Σx·Σy) / (n·Σx² − (Σx)²)</code>
      <p><strong>Linear regression</strong> (dashed blue): long-term directional trend.<br>
      <strong>3-year rolling avg</strong> (dotted orange): smoothed recent trend, reduces year-to-year noise.</p>
    </div>
    <div class="method-card">
      <h3>MD&amp;A Sentiment Score</h3>
      <code>(Positive words − Negative words) / Total words</code>
      <p>Applied to Item 7 (MD&amp;A) section. Uses a domain-specific financial word list.
      Negative scores dominated the 2008–2009 period across all banks.</p>
    </div>
    <div class="method-card">
      <h3>Data Sources</h3>
      <p>All data extracted from SEC EDGAR 10-K annual filings.
      XBRL (inline) extraction used for filings post-2009;
      HTML table extraction for earlier filings.
      Pre-2009 quality is lower — no XBRL tags available.</p>
    </div>
    <div class="method-card">
      <h3>Data Coverage Notes</h3>
      <p>WFC and USB: EDGAR delivered a stub/schema file for some recent years —
      financial tables not available in those filings as downloaded.
      BK data begins 2007 (post-merger with Mellon).
      CET1 ratio null before 2013 is correct (introduced in Basel III).</p>
    </div>
  </div>

  <div class="disclaimer">
    <strong>DISCLAIMER:</strong>
    This dashboard presents historical figures extracted from public SEC filings.
    Any derived scores, trends, or summary statistics are illustrative analytical tools —
    they are NOT validated financial models and NOT investment advice.
    Past patterns in these filings do not predict future performance.
    Consult a licensed financial advisor before making investment decisions.
  </div>
</section>

</main>

<!-- TOOLTIP for heatmap -->
<div id="heatmap-tooltip" class="heatmap-tooltip" style="display:none"></div>

<script>
// ═══════════════════════════════════════════════════════════════════════
// EMBEDDED DATA
// ═══════════════════════════════════════════════════════════════════════
const BANK_DATA    = {js(fin)};
const CRISIS_DATA  = {js(crisis)};
const SENT_DATA    = {js(sent)};
const ALL_YEARS    = {js(all_years)};
const TICKERS      = {js(TICKERS)};
const BANK_NAMES   = {js(BANK_NAMES)};
const METRIC_LABELS= {js(METRIC_LABELS)};
const DEFAULT_TICKER = {js(default_ticker)};

// ═══════════════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════════════

function fmtVal(v, col) {{
  if (v === null || v === undefined || isNaN(v)) return '—';
  const pct = ['tier1_capital_ratio','total_capital_ratio','tier1_leverage_ratio',
               'cet1_ratio','roe','roa','rotce','nim','efficiency_ratio',
               'net_charge_off_rate','npl_ratio','level3_pct_assets','allowance_coverage_ratio'];
  const share = ['eps_basic','eps_diluted','bvps','tbvps'];
  if (pct.includes(col)) return v.toFixed(2) + '%';
  if (share.includes(col)) return '$' + v.toFixed(2);
  const abs = Math.abs(v);
  if (abs >= 1e6) return (v/1e6).toFixed(2) + 'T';
  if (abs >= 1e3) return (v/1e3).toFixed(1) + 'B';
  return v.toFixed(0);
}}

function fmtPct(v) {{
  if (v === null || v === undefined || isNaN(v)) return '—';
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(1) + '%';
}}

function yoy(rows, col, year) {{
  const cur  = rows.find(r => r.year === year);
  const prev = rows.find(r => r.year === year - 1);
  if (!cur || !prev || cur[col] === null || prev[col] === null || prev[col] === 0) return null;
  return (cur[col] - prev[col]) / Math.abs(prev[col]) * 100;
}}

function linearRegression(xs, ys) {{
  const valid = xs.map((x,i) => [x, ys[i]]).filter(([,y]) => y !== null && !isNaN(y));
  if (valid.length < 3) return null;
  const n = valid.length;
  const sx = valid.reduce((s,[x]) => s+x, 0);
  const sy = valid.reduce((s,[,y]) => s+y, 0);
  const sxy = valid.reduce((s,[x,y]) => s+x*y, 0);
  const sxx = valid.reduce((s,[x]) => s+x*x, 0);
  const denom = n*sxx - sx*sx;
  if (denom === 0) return null;
  const m = (n*sxy - sx*sy) / denom;
  const b = (sy - m*sx) / n;
  return xs.map(x => m*x + b);
}}

function rollingAvg(ys, w=3) {{
  return ys.map((_, i) => {{
    const slice = ys.slice(Math.max(0, i-w+1), i+1).filter(v => v !== null && !isNaN(v));
    return slice.length ? slice.reduce((a,b) => a+b, 0)/slice.length : null;
  }});
}}

// Crisis shading plugin for Chart.js
const crisisShadePlugin = {{
  id: 'crisisShade',
  beforeDraw(chart) {{
    const {{ctx, chartArea, scales}} = chart;
    if (!chartArea || !scales.x) return;
    const xScale = scales.x;
    const years = chart.data.labels || [];
    function shadeYearRange(y1, y2, color) {{
      const idx1 = years.findIndex(y => +y >= y1);
      const idx2 = years.findLastIndex(y => +y <= y2);
      if (idx1 < 0 || idx2 < 0) return;
      const x1 = xScale.getPixelForValue(idx1);
      const x2 = xScale.getPixelForValue(idx2);
      ctx.save();
      ctx.fillStyle = color;
      ctx.fillRect(x1, chartArea.top, x2-x1, chartArea.height);
      ctx.restore();
    }}
    shadeYearRange(2008, 2009, 'rgba(204,102,0,0.08)');
    shadeYearRange(2020, 2020, 'rgba(0,102,204,0.06)');
  }}
}};
Chart.register(crisisShadePlugin);

const CHART_DEFAULTS = {{
  responsive: true, maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{
    legend: {{ position: 'top', labels: {{ font: {{ family:'Inter', size:11 }}, boxWidth:12, padding:12 }} }},
    tooltip: {{ backgroundColor:'#1a1a2e', titleFont:{{family:'Inter',size:12}},
               bodyFont:{{family:'JetBrains Mono',size:11}}, padding:10 }},
  }},
  scales: {{
    x: {{ grid:{{ color:'#e8eaf0' }}, ticks:{{ font:{{family:'Inter',size:10}} }} }},
    y: {{ grid:{{ color:'#e8eaf0' }}, ticks:{{ font:{{family:'JetBrains Mono',size:10}} }} }},
  }},
}};

function makeChart(id, config) {{
  const el = document.getElementById(id);
  if (!el) return null;
  const existing = Chart.getChart(el);
  if (existing) existing.destroy();
  return new Chart(el, config);
}}

function addTrendDatasets(years, data, color='#0066cc', label='') {{
  const xs = years.map((_,i) => i);
  const regY = linearRegression(xs, data);
  const rollY = rollingAvg(data, 3);
  const datasets = [];
  if (regY) datasets.push({{
    label: `${{label}} Trend (linear)`,
    data: regY,
    borderColor: color + '99',
    borderWidth: 2,
    borderDash: [6,3],
    pointRadius: 0,
    fill: false,
    tension: 0,
  }});
  datasets.push({{
    label: `${{label}} 3Y Avg`,
    data: rollY,
    borderColor: '#cc6600b3',
    borderWidth: 2,
    borderDash: [2,3],
    pointRadius: 0,
    fill: false,
    tension: 0.2,
  }});
  return datasets;
}}

// ═══════════════════════════════════════════════════════════════════════
// KPI CARDS
// ═══════════════════════════════════════════════════════════════════════

function renderKPIs(ticker) {{
  const rows = BANK_DATA[ticker] || [];
  if (!rows.length) {{ document.getElementById('kpi-grid').innerHTML='<p>No data for '+ticker+'</p>'; return; }}
  const latest = rows[rows.length-1];
  const yr = latest.year;
  document.getElementById('kpi-year-label').textContent = '(Latest year: ' + yr + ')';

  const cet1  = latest.cet1_ratio;
  const tier1 = latest.tier1_capital_ratio;
  const capLabel = cet1 !== null ? 'CET1 Ratio' : 'Tier 1 Ratio';
  const capVal   = cet1 !== null ? cet1 : tier1;
  const capCol   = cet1 !== null ? 'cet1_ratio' : 'tier1_capital_ratio';

  const l3pct = latest.level3_pct_assets;
  const l3elevated = l3pct !== null && l3pct > 5;

  const provAvg = rows.filter(r => r.provision_credit_losses !== null)
    .reduce((s,r,_,a) => s + r.provision_credit_losses/a.length, 0);
  const provAboveAvg = latest.provision_credit_losses !== null &&
                       latest.provision_credit_losses > provAvg * 1.2;

  const kpis = [
    {{ col:'total_assets',          label:'Total Assets',             format: v => fmtVal(v,'total_assets') }},
    {{ col:'net_income',            label:'Net Income',               format: v => fmtVal(v,'net_income') }},
    {{ col:'roe',                   label:'Return on Equity',         format: v => v !== null ? v.toFixed(2)+'%' : '—' }},
    {{ col:capCol,                  label:capLabel,                   format: v => v !== null ? v.toFixed(2)+'%' : '—' }},
    {{ col:'provision_credit_losses',label:'Provision for Credit Losses', format: v => fmtVal(v,'provision_credit_losses'), warn: provAboveAvg }},
    {{ col:'level3_assets',         label:'Level 3 Assets',          format: v => fmtVal(v,'level3_assets'), warn: l3elevated }},
  ];

  const grid = document.getElementById('kpi-grid');
  grid.innerHTML = kpis.map(k => {{
    const val = latest[k.col];
    const change = yoy(rows, k.col, yr);
    const changeClass = change === null ? '' : change >= 0 ? 'pos' : 'neg';
    const arrow = change === null ? '' : change >= 0 ? '▲' : '▼';
    const warnClass = k.warn ? ' card-warn' : '';
    return `<div class="kpi-card${{warnClass}}">
      <div class="kpi-label">${{k.label}}</div>
      <div class="kpi-value">${{k.format(val)}}</div>
      <div class="kpi-change ${{changeClass}}">${{arrow}} ${{change !== null ? Math.abs(change).toFixed(1)+'% vs prior yr' : 'No prior year'}}</div>
    </div>`;
  }}).join('');
}}

// ═══════════════════════════════════════════════════════════════════════
// CHART 1: ASSETS & NET LOANS
// ═══════════════════════════════════════════════════════════════════════

function renderAssetsChart(ticker) {{
  const rows = (BANK_DATA[ticker] || []).filter(r => r.total_assets !== null || r.net_loans !== null || r.total_loans_gross !== null);
  if (!rows.length) return;
  const years = rows.map(r => r.year);
  const assets = rows.map(r => r.total_assets !== null ? r.total_assets/1000 : null);
  const loans  = rows.map(r => {{
    const nl = r.net_loans || r.total_loans_gross;
    return nl !== null ? nl/1000 : null;
  }});

  const trendAssets = addTrendDatasets(years, assets, '#0066cc', 'Assets');
  const trendLoans  = addTrendDatasets(years, loans,  '#007a3d', 'Loans');

  makeChart('chart-assets', {{
    type: 'line',
    data: {{
      labels: years,
      datasets: [
        {{ label:'Total Assets ($B)', data:assets, borderColor:'#0066cc', backgroundColor:'rgba(0,102,204,0.05)',
           borderWidth:2.5, pointRadius:3, fill:true, tension:0.2 }},
        {{ label:'Net Loans ($B)', data:loans, borderColor:'#007a3d', backgroundColor:'rgba(0,122,61,0.03)',
           borderWidth:2.5, pointRadius:3, fill:true, tension:0.2 }},
        ...trendAssets, ...trendLoans,
      ]
    }},
    options: {{ ...CHART_DEFAULTS,
      scales: {{ ...CHART_DEFAULTS.scales,
        y: {{ ...CHART_DEFAULTS.scales.y, title:{{ display:true, text:'$B', font:{{size:10}} }} }}
      }}
    }}
  }});

  // Data table
  renderTable('table-assets', years,
    [{{label:'Total Assets ($B)', data:assets}}, {{label:'Net Loans ($B)', data:loans}}]);
}}

// ═══════════════════════════════════════════════════════════════════════
// CHART 2: NET INCOME vs PROVISION
// ═══════════════════════════════════════════════════════════════════════

function renderIncomeChart(ticker) {{
  const rows = (BANK_DATA[ticker] || []).filter(r => r.net_income !== null || r.provision_credit_losses !== null);
  if (!rows.length) return;
  const years = rows.map(r => r.year);
  const income = rows.map(r => r.net_income !== null ? r.net_income/1000 : null);
  const prov   = rows.map(r => r.provision_credit_losses !== null ? r.provision_credit_losses/1000 : null);

  const trendInc = addTrendDatasets(years, income, '#007a3d', 'Income');

  makeChart('chart-income', {{
    type: 'bar',
    data: {{
      labels: years,
      datasets: [
        {{ label:'Provision ($B)', data:prov, backgroundColor:'rgba(204,0,0,0.55)',
           borderColor:'#cc0000', borderWidth:1, order:1, type:'bar' }},
        {{ label:'Net Income ($B)', data:income, borderColor:'#007a3d',
           backgroundColor:'rgba(0,122,61,0.1)', borderWidth:2.5, pointRadius:3,
           fill:false, tension:0.2, type:'line', order:0, yAxisID:'y' }},
        ...trendInc.map(d => ({{...d, type:'line', order:-1, yAxisID:'y'}})),
      ]
    }},
    options: {{ ...CHART_DEFAULTS,
      scales: {{
        x: CHART_DEFAULTS.scales.x,
        y: {{ ...CHART_DEFAULTS.scales.y, title:{{ display:true, text:'$B', font:{{size:10}} }} }},
      }}
    }}
  }});

  renderTable('table-income', years,
    [{{label:'Net Income ($B)', data:income}}, {{label:'Provision ($B)', data:prov}}]);
}}

// ═══════════════════════════════════════════════════════════════════════
// CHART 3: CAPITAL RATIOS
// ═══════════════════════════════════════════════════════════════════════

function renderCapitalChart(ticker) {{
  const rows = (BANK_DATA[ticker] || []).filter(r =>
    r.tier1_capital_ratio !== null || r.cet1_ratio !== null || r.total_capital_ratio !== null);
  if (!rows.length) return;
  const years = rows.map(r => r.year);
  const t1    = rows.map(r => r.tier1_capital_ratio);
  const cet1  = rows.map(r => r.cet1_ratio);
  const totCap= rows.map(r => r.total_capital_ratio);
  const lev   = rows.map(r => r.tier1_leverage_ratio);

  const trend1 = addTrendDatasets(years, t1, '#0066cc', 'Tier1');

  makeChart('chart-capital', {{
    type: 'line',
    data: {{
      labels: years,
      datasets: [
        {{ label:'Tier 1 Ratio (%)', data:t1, borderColor:'#0066cc', borderWidth:2.5, pointRadius:3, tension:0.2 }},
        {{ label:'CET1 Ratio (%)',   data:cet1, borderColor:'#7c3aed', borderWidth:2.5, pointRadius:3, tension:0.2, borderDash:[] }},
        {{ label:'Total Capital (%)', data:totCap, borderColor:'#0891b2', borderWidth:2, pointRadius:2, tension:0.2 }},
        {{ label:'Leverage Ratio (%)', data:lev, borderColor:'#059669', borderWidth:1.5, pointRadius:2, tension:0.2 }},
        ...trend1,
        // Reference lines
        {{ label:'Basel II min 8%', data:years.map(()=>8),  borderColor:'#cc0000', borderWidth:1, borderDash:[4,4], pointRadius:0 }},
        {{ label:'CET1 min 4.5%',  data:years.map(()=>4.5),borderColor:'#cc6600', borderWidth:1, borderDash:[4,4], pointRadius:0 }},
      ]
    }},
    options: {{ ...CHART_DEFAULTS,
      scales: {{ ...CHART_DEFAULTS.scales,
        y: {{ ...CHART_DEFAULTS.scales.y, title:{{ display:true, text:'%', font:{{size:10}} }}, min:0 }}
      }}
    }}
  }});

  renderTable('table-capital', years, [
    {{label:'Tier 1 Ratio (%)', data:t1}},
    {{label:'CET1 Ratio (%)', data:cet1}},
    {{label:'Total Capital (%)', data:totCap}},
    {{label:'Leverage Ratio (%)', data:lev}},
  ]);
}}

// ═══════════════════════════════════════════════════════════════════════
// CHART 4: CREDIT QUALITY
// ═══════════════════════════════════════════════════════════════════════

function renderCreditChart(ticker) {{
  const rows = (BANK_DATA[ticker] || []).filter(r =>
    r.net_charge_off_rate !== null || r.npl_ratio !== null || r.allowance_loan_losses !== null);
  if (!rows.length) return;
  const years = rows.map(r => r.year);
  const nco   = rows.map(r => r.net_charge_off_rate);
  const npl   = rows.map(r => r.npl_ratio);
  const allow = rows.map(r => r.allowance_loan_losses !== null ? r.allowance_loan_losses/1000 : null);

  const trendNco = addTrendDatasets(years, nco, '#cc0000', 'NCO');

  makeChart('chart-credit', {{
    type: 'bar',
    data: {{
      labels: years,
      datasets: [
        {{ label:'Allowance ($B)', data:allow, backgroundColor:'rgba(0,102,204,0.3)',
           borderColor:'#0066cc', borderWidth:1, order:2, type:'bar', yAxisID:'y2' }},
        {{ label:'NCO Rate (%)', data:nco, borderColor:'#cc0000',
           borderWidth:2.5, pointRadius:3, fill:false, tension:0.2, type:'line', order:0, yAxisID:'y' }},
        {{ label:'NPL Ratio (%)', data:npl, borderColor:'#cc6600',
           borderWidth:2, pointRadius:3, fill:false, tension:0.2, type:'line', order:1, yAxisID:'y' }},
        ...trendNco.map(d => ({{...d, type:'line', order:-1, yAxisID:'y'}})),
      ]
    }},
    options: {{ ...CHART_DEFAULTS,
      scales: {{
        x: CHART_DEFAULTS.scales.x,
        y:  {{ ...CHART_DEFAULTS.scales.y, position:'left',  title:{{ display:true, text:'Rate (%)', font:{{size:10}} }}, min:0 }},
        y2: {{ ...CHART_DEFAULTS.scales.y, position:'right', title:{{ display:true, text:'Allowance ($B)', font:{{size:10}} }},
               grid:{{ drawOnChartArea:false }} }},
      }}
    }}
  }});

  renderTable('table-credit', years, [
    {{label:'NCO Rate (%)', data:nco}},
    {{label:'NPL Ratio (%)', data:npl}},
    {{label:'Allowance ($B)', data:allow}},
  ]);
}}

// ═══════════════════════════════════════════════════════════════════════
// CHART 5: CRISIS LANGUAGE
// ═══════════════════════════════════════════════════════════════════════

function renderCrisisChart(ticker) {{
  const byYear = CRISIS_DATA[ticker];
  if (!byYear) return;
  const years = Object.keys(byYear).map(Number).sort();
  if (!years.length) return;

  // Find top 5 terms by peak count for this bank
  const termTotals = {{}};
  for (const yd of Object.values(byYear)) {{
    for (const [t, c] of Object.entries(yd)) {{
      termTotals[t] = Math.max(termTotals[t] || 0, c);
    }}
  }}
  const top5 = Object.entries(termTotals).sort((a,b)=>b[1]-a[1]).slice(0,5).map(([t])=>t);

  const colors = ['#0066cc','#cc0000','#007a3d','#cc6600','#7c3aed'];
  const datasets = top5.map((term, i) => ({{
    label: term,
    data: years.map(y => byYear[y]?.[term] || 0),
    borderColor: colors[i], backgroundColor: colors[i]+'33',
    borderWidth: 2, pointRadius: 3, fill: false, tension: 0.2,
  }}));

  makeChart('chart-crisis', {{
    type: 'line',
    data: {{ labels: years, datasets }},
    options: {{ ...CHART_DEFAULTS,
      plugins: {{ ...CHART_DEFAULTS.plugins, crisisShade: {{}} }},
      scales: {{ ...CHART_DEFAULTS.scales,
        y: {{ ...CHART_DEFAULTS.scales.y, title:{{ display:true, text:'Occurrences', font:{{size:10}} }}, min:0 }}
      }}
    }}
  }});
}}

// ═══════════════════════════════════════════════════════════════════════
// DATA TABLE RENDERER
// ═══════════════════════════════════════════════════════════════════════

function renderTable(containerId, years, series) {{
  const container = document.getElementById(containerId);
  if (!container) return;

  let html = '<div class="table-scroll"><table><thead><tr><th>Year</th>';
  for (const s of series) html += `<th>${{s.label}}</th><th>YoY</th>`;
  html += '</tr></thead><tbody>';

  for (let i=0; i<years.length; i++) {{
    html += `<tr><td>${{years[i]}}</td>`;
    for (const s of series) {{
      const v = s.data[i];
      const vf = v !== null && !isNaN(v) ? (Math.abs(v) > 100 ? v.toFixed(1) : v.toFixed(2)) : '—';
      let yoyStr = '—', yoyClass = '';
      if (i > 0 && v !== null && !isNaN(v) && s.data[i-1] !== null && !isNaN(s.data[i-1]) && s.data[i-1] !== 0) {{
        const ch = (v - s.data[i-1]) / Math.abs(s.data[i-1]) * 100;
        yoyStr = (ch >= 0 ? '+' : '') + ch.toFixed(1) + '%';
        yoyClass = ch >= 0 ? 'pos' : 'neg';
      }}
      html += `<td>${{vf}}</td><td class="${{yoyClass}}">${{yoyStr}}</td>`;
    }}
    html += '</tr>';
  }}
  html += '</tbody></table></div>';
  container.innerHTML = html;
}}

// ═══════════════════════════════════════════════════════════════════════
// ALL-BANKS COMPARISON CHARTS
// ═══════════════════════════════════════════════════════════════════════

function renderAllBanks(year) {{
  const banks  = TICKERS.filter(t => BANK_DATA[t]);
  const colors = banks.map((_,i) => `hsl(${{i * 28}},65%,45%)`);

  function barDataFor(col, scale=1) {{
    return banks.map(t => {{
      const row = (BANK_DATA[t]||[]).find(r => r.year === year);
      return row && row[col] !== null ? row[col] / scale : null;
    }});
  }}

  const cmpOpts = (ylabel) => ({{ ...CHART_DEFAULTS,
    plugins: {{ ...CHART_DEFAULTS.plugins, legend:{{ display:false }} }},
    scales: {{ x:CHART_DEFAULTS.scales.x, y:{{ ...CHART_DEFAULTS.scales.y,
               title:{{ display:true, text:ylabel, font:{{size:10}} }} }} }},
    indexAxis: 'x',
  }});

  [['cmp-roe', 'roe', '%', 1], ['cmp-assets', 'total_assets', '$B', 1000],
   ['cmp-nco', 'net_charge_off_rate', '%', 1]].forEach(([id, col, lbl, sc]) => {{
    makeChart(id, {{
      type:'bar',
      data:{{ labels:banks, datasets:[{{
        data:barDataFor(col, sc), backgroundColor:colors,
        borderColor:colors.map(c=>c+'cc'), borderWidth:1,
      }}] }},
      options: cmpOpts(lbl),
    }});
  }});

  // CET1 / Tier1 (prefer CET1 where available)
  const capData = banks.map(t => {{
    const row = (BANK_DATA[t]||[]).find(r => r.year === year);
    if (!row) return null;
    return row.cet1_ratio !== null ? row.cet1_ratio : row.tier1_capital_ratio;
  }});
  makeChart('cmp-cet1', {{
    type:'bar',
    data:{{ labels:banks, datasets:[{{ data:capData, backgroundColor:colors, borderColor:colors, borderWidth:1 }}] }},
    options: cmpOpts('%'),
  }});

  // Sentiment multi-line
  const sentDatasets = banks.map((t,i) => {{
    const byYear = SENT_DATA[t] || {{}};
    const ys = ALL_YEARS.map(y => byYear[y] ? byYear[y].score : null);
    return {{
      label: t, data: ys, borderColor: colors[i], borderWidth: 1.5,
      pointRadius: 2, fill: false, tension: 0.2,
    }};
  }});
  makeChart('cmp-sentiment', {{
    type:'line',
    data:{{ labels: ALL_YEARS, datasets: sentDatasets }},
    options: {{
      ...CHART_DEFAULTS,
      scales: {{ ...CHART_DEFAULTS.scales,
        y: {{ ...CHART_DEFAULTS.scales.y, title:{{ display:true, text:'Sentiment', font:{{size:10}} }} }}
      }}
    }}
  }});
}}

// ═══════════════════════════════════════════════════════════════════════
// HEATMAP
// ═══════════════════════════════════════════════════════════════════════

function renderHeatmap() {{
  const container = document.getElementById('heatmap-grid');
  if (!container) return;
  const tooltip = document.getElementById('heatmap-tooltip');

  // Compute total crisis counts per bank-year
  const counts = {{}};
  let maxCount = 1;
  for (const t of TICKERS) {{
    counts[t] = {{}};
    const byYear = CRISIS_DATA[t] || {{}};
    for (const y of ALL_YEARS) {{
      const total = Object.values(byYear[y] || {{}}).reduce((s,v) => s+v, 0);
      counts[t][y] = total;
      if (total > maxCount) maxCount = total;
    }}
  }}

  function countToColor(n) {{
    const t = Math.sqrt(n / maxCount);  // sqrt scale for better contrast
    if (t < 0.25) return `rgba(245,246,248,${{1-t*2}})`;
    if (t < 0.5)  return `rgba(190,212,240,${{0.5+t}})`;
    if (t < 0.75) return `rgba(204,102,0,${{0.3+t*0.7}})`;
    return `rgba(204,0,0,${{0.4+t*0.6}})`;
  }}

  // Build grid: header row + one row per bank
  let html = '<div class="heatmap-table">';
  // Header
  html += '<div class="heatmap-row heatmap-header"><div class="heatmap-cell heatmap-ylabel"></div>';
  for (const y of ALL_YEARS) html += `<div class="heatmap-cell heatmap-xlabel">${{y}}</div>`;
  html += '</div>';
  // Rows
  for (const t of TICKERS) {{
    html += `<div class="heatmap-row">`;
    html += `<div class="heatmap-cell heatmap-ylabel">${{t}}</div>`;
    for (const y of ALL_YEARS) {{
      const n = counts[t][y] || 0;
      const color = countToColor(n);
      html += `<div class="heatmap-cell heatmap-data"
        style="background:${{color}}"
        data-ticker="${{t}}" data-year="${{y}}" data-count="${{n}}"
        title="${{t}} ${{y}}: ${{n}} total crisis/risk term occurrences"
        ></div>`;
    }}
    html += '</div>';
  }}
  html += '</div>';
  container.innerHTML = html;

  // Tooltip on hover
  container.querySelectorAll('.heatmap-data').forEach(cell => {{
    cell.addEventListener('mouseenter', e => {{
      const t = cell.dataset.ticker;
      const y = cell.dataset.year;
      const n = cell.dataset.count;
      const byYear = CRISIS_DATA[t]?.[+y] || {{}};
      const top = Object.entries(byYear).sort((a,b)=>b[1]-a[1]).slice(0,5);
      let tip = `<strong>${{BANK_NAMES[t]}} ${{y}}</strong><br>Total: ${{n}} occurrences<br>`;
      if (top.length) tip += '<br>Top terms:<br>' + top.map(([term,c]) => `&nbsp;&nbsp;${{term}}: ${{c}}`).join('<br>');
      tooltip.innerHTML = tip;
      tooltip.style.display = 'block';
    }});
    cell.addEventListener('mousemove', e => {{
      tooltip.style.left = (e.pageX + 14) + 'px';
      tooltip.style.top  = (e.pageY - 10) + 'px';
    }});
    cell.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
  }});
}}

// ═══════════════════════════════════════════════════════════════════════
// NEWS
// ═══════════════════════════════════════════════════════════════════════

const TICKERTICK_FEED_URL   = 'https://api.tickertick.com/feed?q=';
const TICKERTICK_SEARCH_URL = 'https://api.tickertick.com/tickers?p=';
const DEFAULT_NEWS_QUERY = '(or ' + TICKERS.map(t => 'tt:' + t.toLowerCase()).join(' ') + ')';

let newsTicker      = null;   // null => default all-banks feed
let newsCompanyName = null;
let newsDropdownItems = [];
let newsDropdownHighlight = -1;

function timeAgo(timeVal) {{
  if (!timeVal) return '';
  const dt = new Date(timeVal);
  if (isNaN(dt.getTime())) return '';
  const diffMin = Math.round((Date.now() - dt.getTime()) / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${{diffMin}} min ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${{diffHr}} hour${{diffHr === 1 ? '' : 's'}} ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${{diffDay}} day${{diffDay === 1 ? '' : 's'}} ago`;
}}

function storyTickers(story) {{
  const raw = (story.tags || []).concat(story.tickers || []).map(t => String(t).toUpperCase());
  return [...new Set(raw)];
}}

function escapeHtml(str) {{
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}}

function renderNewsList(stories) {{
  document.getElementById('news-updated').textContent =
    'Last updated: ' + new Date().toLocaleTimeString();

  const list = document.getElementById('news-list');
  if (!stories.length) {{
    list.innerHTML = '<p class="muted-label">No news available.</p>';
    return;
  }}
  list.innerHTML = stories.map(s => {{
    const timeStr = timeAgo(s.time);
    const badges = storyTickers(s).map(t =>
      `<span class="ticker-badge">${{escapeHtml(t)}}</span>`).join('');
    return `<div class="news-item">
      <a href="${{s.url}}" target="_blank" rel="noopener" class="news-title">${{escapeHtml(s.title)}}</a>
      <div class="news-meta">
        <span class="news-source">${{escapeHtml(s.site)}}</span>
        ${{timeStr ? `<span class="news-time">${{timeStr}}</span>` : ''}}
        ${{badges}}
      </div>
    </div>`;
  }}).join('');
}}

function renderNewsError() {{
  document.getElementById('news-updated').textContent = '';
  document.getElementById('news-list').innerHTML =
    '<p class="muted-label news-fallback">Unable to load news — check your internet connection and try again.</p>';
}}

function renderNewsSkeleton() {{
  document.getElementById('news-list').innerHTML = Array.from({{length: 3}}).map(() =>
    `<div class="news-skeleton-row">
      <div class="skeleton-bar skeleton-title"></div>
      <div class="skeleton-bar skeleton-meta"></div>
    </div>`
  ).join('');
}}

function updateShowingForLabel() {{
  const el = document.getElementById('news-showing-for');
  if (newsTicker) {{
    el.innerHTML = `Showing news for: ${{escapeHtml(newsCompanyName)}} (${{escapeHtml(newsTicker)}}) ` +
      `<button id="news-clear-btn" class="news-clear-btn" title="Clear">✕</button>`;
    document.getElementById('news-clear-btn').addEventListener('click', clearNewsSearch);
  }} else {{
    el.textContent = 'Showing news for: All tracked banks';
  }}
}}

function clearNewsSearch() {{
  newsTicker = null;
  newsCompanyName = null;
  const input = document.getElementById('newsSearchInput');
  if (input) input.value = '';
  closeNewsDropdown();
  loadNews();
}}

async function loadNews() {{
  updateShowingForLabel();
  renderNewsSkeleton();
  const query = newsTicker ? `tt:${{newsTicker.toLowerCase()}}` : DEFAULT_NEWS_QUERY;
  try {{
    const resp = await fetch(TICKERTICK_FEED_URL + encodeURIComponent(query) + '&n=30');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    renderNewsList(data.stories || data || []);
  }} catch (err) {{
    console.error('News fetch failed:', err);
    renderNewsError();
  }}
}}

// ── Company search / autocomplete ──────────────────────────────────────

function debounce(fn, delay) {{
  let timer;
  return (...args) => {{
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  }};
}}

function closeNewsDropdown() {{
  const dropdown = document.getElementById('newsSearchDropdown');
  if (!dropdown) return;
  dropdown.classList.add('hidden');
  dropdown.innerHTML = '';
  newsDropdownItems = [];
  newsDropdownHighlight = -1;
}}

function setDropdownHighlight(idx) {{
  newsDropdownHighlight = idx;
  document.querySelectorAll('#newsSearchDropdown .search-dropdown-item').forEach((el, i) => {{
    el.classList.toggle('highlighted', i === idx);
  }});
}}

function selectDropdownItem(idx) {{
  const item = newsDropdownItems[idx];
  if (!item) return;
  newsTicker = item.ticker.toUpperCase();
  newsCompanyName = item.company_name;
  const input = document.getElementById('newsSearchInput');
  if (input) input.value = item.company_name;
  closeNewsDropdown();
  loadNews();
}}

function renderNewsDropdown(items, query) {{
  const dropdown = document.getElementById('newsSearchDropdown');
  newsDropdownItems = items;
  newsDropdownHighlight = -1;
  if (!items.length) {{
    dropdown.innerHTML = `<div class="search-dropdown-empty">No companies found for '${{escapeHtml(query)}}'</div>`;
    dropdown.classList.remove('hidden');
    return;
  }}
  dropdown.innerHTML = items.map((item, i) =>
    `<div class="search-dropdown-item" data-idx="${{i}}">
      <span class="dropdown-company-name">${{escapeHtml(item.company_name)}}</span>
      <span class="ticker-badge">${{escapeHtml(item.ticker.toUpperCase())}}</span>
    </div>`
  ).join('');
  dropdown.classList.remove('hidden');
  dropdown.querySelectorAll('.search-dropdown-item').forEach(el => {{
    const idx = parseInt(el.dataset.idx, 10);
    el.addEventListener('click', () => selectDropdownItem(idx));
    el.addEventListener('mouseenter', () => setDropdownHighlight(idx));
  }});
}}

const searchCompanies = debounce(async (query) => {{
  try {{
    const resp = await fetch(TICKERTICK_SEARCH_URL + encodeURIComponent(query) + '&n=10');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    renderNewsDropdown(data.tickers || [], query);
  }} catch (err) {{
    console.error('Ticker search failed:', err);
    closeNewsDropdown();
  }}
}}, 250);

// ═══════════════════════════════════════════════════════════════════════
// SEARCH
// ═══════════════════════════════════════════════════════════════════════

function handleSearch(query) {{
  const banner = document.getElementById('search-banner');
  if (!query.trim()) {{ banner.style.display='none'; return; }}
  const q = query.trim().toUpperCase();

  // Match ticker
  if (TICKERS.includes(q) || Object.keys(BANK_NAMES).find(t => t === q)) {{
    const sel = document.getElementById('bank-select');
    if (sel) {{ sel.value = q; sel.dispatchEvent(new Event('change')); }}
    activateView('bank');
    banner.innerHTML = `Showing: ${{BANK_NAMES[q] || q}}`;
    banner.style.display = 'block';
    return;
  }}

  // Match full bank name
  const matchTicker = Object.entries(BANK_NAMES).find(([,name]) => name.toUpperCase().includes(q));
  if (matchTicker) {{
    const sel = document.getElementById('bank-select');
    if (sel) {{ sel.value = matchTicker[0]; sel.dispatchEvent(new Event('change')); }}
    activateView('bank');
    banner.innerHTML = `Showing: ${{matchTicker[1]}}`;
    banner.style.display = 'block';
    return;
  }}

  banner.innerHTML = `Search: no exact match for "${{query}}"`;
  banner.style.display = 'block';
}}

// ═══════════════════════════════════════════════════════════════════════
// VIEW MANAGEMENT
// ═══════════════════════════════════════════════════════════════════════

function activateView(view) {{
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  const el = document.getElementById('view-' + view);
  if (el) el.classList.add('active');
  const btn = document.querySelector(`.tab-btn[data-view="${{view}}"]`);
  if (btn) btn.classList.add('active');
  const selectorBar = document.getElementById('bank-selector-bar');
  if (selectorBar) selectorBar.style.display = view === 'bank' ? 'flex' : 'none';
}}

function renderBankView(ticker) {{
  document.getElementById('bank-title-label').textContent = BANK_NAMES[ticker] || ticker;
  renderKPIs(ticker);
  renderAssetsChart(ticker);
  renderIncomeChart(ticker);
  renderCapitalChart(ticker);
  renderCreditChart(ticker);
  renderCrisisChart(ticker);
}}

// ═══════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {{
  // Bank selector
  const sel = document.getElementById('bank-select');
  sel.value = DEFAULT_TICKER;
  sel.addEventListener('change', () => renderBankView(sel.value));
  renderBankView(DEFAULT_TICKER);

  // Tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const view = btn.dataset.view;
      activateView(view);
      if (view === 'allbanks') {{
        const yr = parseInt(document.getElementById('year-select-all').value) ||
                   ALL_YEARS[ALL_YEARS.length-1];
        renderAllBanks(yr);
        renderHeatmap();
      }} else if (view === 'news') {{
        loadNews();
      }}
    }});
  }});

  // News refresh
  const newsRefreshBtn = document.getElementById('news-refresh-btn');
  if (newsRefreshBtn) newsRefreshBtn.addEventListener('click', loadNews);

  // News company search / autocomplete
  const newsSearchInput = document.getElementById('newsSearchInput');
  if (newsSearchInput) {{
    newsSearchInput.addEventListener('input', () => {{
      const q = newsSearchInput.value.trim();
      if (!q) {{
        closeNewsDropdown();
        if (newsTicker) clearNewsSearch();
        return;
      }}
      searchCompanies(q);
    }});
    newsSearchInput.addEventListener('keydown', e => {{
      const dropdown = document.getElementById('newsSearchDropdown');
      if (dropdown.classList.contains('hidden') || !newsDropdownItems.length) return;
      if (e.key === 'ArrowDown') {{
        e.preventDefault();
        setDropdownHighlight(Math.min(newsDropdownHighlight + 1, newsDropdownItems.length - 1));
      }} else if (e.key === 'ArrowUp') {{
        e.preventDefault();
        setDropdownHighlight(Math.max(newsDropdownHighlight - 1, 0));
      }} else if (e.key === 'Enter') {{
        e.preventDefault();
        if (newsDropdownHighlight >= 0) selectDropdownItem(newsDropdownHighlight);
      }} else if (e.key === 'Escape') {{
        closeNewsDropdown();
      }}
    }});
  }}
  document.addEventListener('click', e => {{
    const wrapper = document.querySelector('.news-search-wrapper');
    if (wrapper && !wrapper.contains(e.target)) closeNewsDropdown();
  }});

  // Year picker (all-banks)
  const yrSel = document.getElementById('year-select-all');
  if (yrSel) yrSel.addEventListener('change', () => renderAllBanks(parseInt(yrSel.value)));

  // Info boxes
  document.addEventListener('click', e => {{
    const btn = e.target.closest('.info-btn');
    if (btn) {{
      const target = document.getElementById(btn.dataset.target);
      if (target) target.style.display = target.style.display === 'none' ? 'block' : 'none';
    }}
  }});

  // Search
  const searchEl = document.getElementById('search-bar');
  if (searchEl) {{
    let timer;
    searchEl.addEventListener('input', () => {{
      clearTimeout(timer);
      timer = setTimeout(() => handleSearch(searchEl.value), 300);
    }});
    searchEl.addEventListener('keydown', e => {{
      if (e.key === 'Enter') handleSearch(searchEl.value);
    }});
  }}
}});
</script>
</body>
</html>"""

# ── CSS ────────────────────────────────────────────────────────────────────────
CSS = """
:root {
  --bg: #f5f6f8;
  --card: #ffffff;
  --border: #d0d4de;
  --grid: #e8eaf0;
  --text: #2d2d2d;
  --head: #1a1a2e;
  --muted: #666688;
  --accent: #0066cc;
  --pos: #007a3d;
  --neg: #cc0000;
  --warn: #cc6600;
  --nav-bg: #1a1a2e;
  --shadow: 0 1px 4px rgba(0,0,0,0.08);
  --font: 'Inter', -apple-system, sans-serif;
  --mono: 'JetBrains Mono', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.5;
  min-height: 100vh;
}

/* NAV */
.topnav {
  background: var(--nav-bg);
  color: #fff;
  padding: 0 24px;
  display: flex;
  align-items: center;
  gap: 16px;
  height: 52px;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
.nav-brand { font-weight: 700; font-size: 15px; white-space: nowrap; color: #fff; }
.nav-controls { display: flex; align-items: center; gap: 10px; margin-left: auto; }
#search-bar {
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.2);
  color: #fff;
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 13px;
  width: 180px;
  transition: background 0.2s;
}
#search-bar::placeholder { color: rgba(255,255,255,0.5); }
#search-bar:focus { outline: none; background: rgba(255,255,255,0.2); }
.nav-tabs { display: flex; gap: 2px; }
.tab-btn {
  background: transparent;
  border: none;
  color: rgba(255,255,255,0.7);
  cursor: pointer;
  padding: 6px 14px;
  border-radius: 5px;
  font-size: 13px;
  font-family: var(--font);
  transition: background 0.15s, color 0.15s;
}
.tab-btn:hover { background: rgba(255,255,255,0.1); color: #fff; }
.tab-btn.active { background: var(--accent); color: #fff; }
.btn-print {
  background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.2);
  color: #fff;
  cursor: pointer;
  padding: 5px 12px;
  border-radius: 5px;
  font-size: 12px;
  font-family: var(--font);
}
.btn-print:hover { background: rgba(255,255,255,0.2); }

/* BANK SELECTOR BAR */
.bank-selector-bar {
  background: var(--card);
  border-bottom: 1px solid var(--border);
  padding: 10px 24px;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.bank-selector-bar label { font-weight: 600; color: var(--head); font-size: 13px; }
#bank-select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  font-family: var(--font);
  background: var(--bg);
  min-width: 220px;
}
.bank-title-label {
  font-size: 18px;
  font-weight: 700;
  color: var(--head);
}

.search-banner {
  background: #e8f0ff;
  border-left: 3px solid var(--accent);
  padding: 8px 24px;
  font-size: 13px;
  color: var(--head);
}

/* MAIN */
main { padding: 20px 24px 40px; max-width: 1400px; margin: 0 auto; }

/* VIEWS */
.view { display: none; }
.view.active { display: block; }

/* SECTION HEADERS */
.section-header {
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--head);
  border-bottom: 2px solid var(--accent);
  padding-bottom: 6px;
  margin: 24px 0 14px;
  display: flex;
  align-items: center;
  gap: 10px;
}

/* KPI CARDS */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
  margin-bottom: 4px;
}
.kpi-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  box-shadow: var(--shadow);
  transition: box-shadow 0.15s;
}
.kpi-card:hover { box-shadow: 0 3px 10px rgba(0,0,0,0.12); }
.kpi-card.card-warn { border-left: 3px solid var(--warn); }
.kpi-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  margin-bottom: 6px;
}
.kpi-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--head);
  font-family: var(--mono);
}
.kpi-change {
  font-size: 11px;
  margin-top: 4px;
  color: var(--muted);
}
.kpi-change.pos { color: var(--pos); }
.kpi-change.neg { color: var(--neg); }

/* CHART PANELS */
.chart-panel {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow);
  margin-bottom: 18px;
  overflow: hidden;
}
.chart-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px 8px;
  font-weight: 600;
  font-size: 13px;
  color: var(--head);
  border-bottom: 1px solid var(--grid);
}
.chart-wrap {
  position: relative;
  height: 320px;
  padding: 12px 12px 8px;
}
.info-btn {
  background: transparent;
  border: 1px solid var(--accent);
  color: var(--accent);
  border-radius: 50%;
  width: 22px;
  height: 22px;
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font);
  line-height: 1;
  padding: 0;
}
.info-btn:hover { background: var(--accent); color: #fff; }
.info-box {
  margin: 0 16px 12px;
  padding: 12px 14px;
  background: #e8f0ff;
  border-left: 3px solid var(--accent);
  border-radius: 4px;
  font-size: 12px;
  color: var(--text);
  font-family: var(--mono);
  line-height: 1.7;
}
.info-box strong { font-weight: 600; }
.info-box em { color: var(--muted); }

/* DATA TABLES */
.data-table-wrap {
  overflow-x: auto;
  padding: 0 0 8px;
  border-top: 1px solid var(--grid);
}
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td {
  padding: 6px 10px;
  text-align: right;
  border-bottom: 1px solid var(--grid);
  white-space: nowrap;
}
th:first-child, td:first-child { text-align: left; font-weight: 500; }
thead th { color: var(--muted); font-weight: 600; font-size: 11px; background: #f9fafb; }
tbody tr:hover td { background: rgba(0,102,204,0.04); }
.pos { color: var(--pos); }
.neg { color: var(--neg); }

/* ALL-BANKS */
.allbanks-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 4px;
  padding: 12px 0;
}
.allbanks-controls select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  font-family: var(--font);
  background: var(--bg);
}
.compare-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 4px;
}
.compare-grid .chart-wrap { height: 240px; }

/* HEATMAP */
.heatmap-container {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow);
  padding: 16px;
  margin-bottom: 18px;
  overflow-x: auto;
}
.heatmap-legend {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  font-size: 11px;
  color: var(--muted);
}
.heatmap-legend-bar {
  height: 10px;
  width: 120px;
  border-radius: 5px;
  background: linear-gradient(to right, #f5f6f8, #bed4f0, #cc6600, #cc0000);
  border: 1px solid var(--border);
}
.heatmap-table { display: inline-block; min-width: 100%; }
.heatmap-row { display: flex; }
.heatmap-cell {
  width: 32px;
  height: 24px;
  border: 1px solid rgba(255,255,255,0.5);
  flex-shrink: 0;
}
.heatmap-ylabel {
  width: 50px;
  font-size: 11px;
  font-weight: 600;
  color: var(--head);
  display: flex;
  align-items: center;
  background: transparent;
  border: none;
}
.heatmap-xlabel {
  font-size: 9px;
  color: var(--muted);
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  transform: rotate(-45deg);
  height: 28px;
  width: 32px;
}
.heatmap-header { margin-bottom: 4px; }
.heatmap-data { cursor: pointer; border-radius: 2px; }
.heatmap-data:hover { outline: 2px solid var(--accent); outline-offset: -1px; }
.heatmap-tooltip {
  position: fixed;
  background: #1a1a2e;
  color: #fff;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 12px;
  max-width: 260px;
  z-index: 200;
  pointer-events: none;
  line-height: 1.6;
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
.muted-label { color: var(--muted); font-weight: 400; font-size: 12px; }

/* NEWS */
.news-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 0 4px;
}
.news-header-right { display: flex; align-items: center; gap: 12px; }
.news-updated { font-size: 12px; color: var(--muted); }
.news-refresh-btn {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  padding: 4px 10px;
  cursor: pointer;
}
.news-refresh-btn:hover { background: #f0f4ff; }

.news-search-wrapper { position: relative; width: 100%; max-width: 500px; margin-bottom: 16px; }
#newsSearchInput {
  width: 100%;
  padding: 10px 16px;
  font-size: 14px;
  font-family: 'Inter', sans-serif;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  color: #2d2d2d;
  outline: none;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  box-sizing: border-box;
}
#newsSearchInput:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(0,102,204,0.12);
}
.search-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  z-index: 1000;
  max-height: 320px;
  overflow-y: auto;
}
.search-dropdown.hidden { display: none; }
.search-dropdown-item {
  padding: 10px 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  color: #2d2d2d;
}
.search-dropdown-item:hover, .search-dropdown-item.highlighted { background: #f0f4ff; }
.dropdown-company-name { font-weight: 700; flex: 1; }
.search-dropdown-item .ticker-badge {
  font-family: var(--mono);
  font-weight: 600;
  padding: 2px 7px;
  flex-shrink: 0;
}
.search-dropdown-empty { padding: 12px 16px; font-size: 13px; color: var(--muted); }

.news-showing-for { font-size: 12px; color: var(--muted); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.news-clear-btn { background: none; border: none; color: #cc0000; cursor: pointer; font-size: 14px; padding: 0 4px; }

.news-skeleton-row { padding: 12px 16px; border-bottom: 1px solid #e8eaf0; }
.skeleton-bar {
  background: linear-gradient(90deg, #eceff4 25%, #f5f6f8 37%, #eceff4 63%);
  background-size: 400% 100%;
  animation: skeleton-pulse 1.4s ease infinite;
  border-radius: 4px;
}
.skeleton-title { height: 14px; width: 70%; margin-bottom: 8px; }
.skeleton-meta  { height: 10px; width: 40%; }
@keyframes skeleton-pulse {
  0%   { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}

.news-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 600px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border) var(--card);
}
.news-list::-webkit-scrollbar { width: 8px; }
.news-list::-webkit-scrollbar-track { background: var(--card); }
.news-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.news-list::-webkit-scrollbar-thumb:hover { background: var(--muted); }
.news-fallback { padding: 24px 12px; text-align: center; }
.news-item {
  background: var(--card);
  padding: 12px 16px;
  border-bottom: 1px solid #e8eaf0;
  transition: background 0.1s;
}
.news-item:last-child { border-bottom: none; }
.news-item:hover { background: #f8f9ff; }
.news-title {
  color: var(--head);
  font-weight: 500;
  font-size: 14px;
  text-decoration: none;
  display: block;
  margin-bottom: 4px;
  line-height: 1.4;
}
.news-title:hover { color: var(--accent); text-decoration: underline; }
.news-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.news-source { font-size: 12px; color: var(--muted); }
.news-time   { font-size: 12px; color: var(--muted); }
.ticker-badge {
  background: var(--accent);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: var(--mono);
}

/* METHODOLOGY */
.methodology-view { max-width: 1000px; }
.method-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.method-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  box-shadow: var(--shadow);
}
.method-card h3 {
  font-size: 13px;
  font-weight: 700;
  color: var(--head);
  margin-bottom: 8px;
  border-bottom: 1px solid var(--grid);
  padding-bottom: 6px;
}
.method-card code {
  display: block;
  background: #f0f4ff;
  border-left: 3px solid var(--accent);
  padding: 6px 10px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 12px;
  color: #1a1a2e;
  margin-bottom: 8px;
}
.method-card p { font-size: 12px; color: var(--text); line-height: 1.6; }
.disclaimer {
  background: #fff8e6;
  border: 1px solid #e6a817;
  border-radius: 8px;
  padding: 16px;
  font-size: 12px;
  color: #5a4400;
  line-height: 1.6;
  margin-top: 8px;
}
.disclaimer strong { font-weight: 700; }

@media print {
  .topnav, .bank-selector-bar, .search-banner, .info-btn { display: none !important; }
  .view { display: block !important; }
  body { background: #fff; }
  .chart-panel, .kpi-card { break-inside: avoid; }
}

@media (max-width: 600px) {
  main { padding: 12px; }
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .compare-grid { grid-template-columns: 1fr; }
  .topnav { flex-wrap: wrap; height: auto; padding: 8px 12px; }
  #search-bar { width: 120px; }
}
"""

if __name__ == '__main__':
    build()
