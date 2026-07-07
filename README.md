# Finance Project

A professional financial **screener** dashboard built on the Zacks weekly
screener export (~5,400 US companies) enriched with SEC EDGAR XBRL fundamentals,
stored in Supabase PostgreSQL.

## Components

| File            | Purpose |
|-----------------|---------|
| `screener.html` | The screener dashboard (3-panel: sidebar filters, table/scatter/analysis views, peek panel). Open in a browser. |
| `dashboard.html`| Per-company "Financial Insight" viewer (EDGAR statements + analytics). |
| `zacks_sync.py` | Weekly Selenium automation: logs into Zacks, downloads the screener Excel, parses with pandas, upserts into Supabase. |
| `schema.sql`    | `zacks_fundamentals` table definition + RLS read policy for Supabase. |
| `extract.py`    | SEC EDGAR extraction → `companies` + `financials` tables. |

## Data flow

```
Zacks.com (weekly Excel)          SEC EDGAR (companyfacts)
        │                                  │
   zacks_sync.py                       extract.py
        │                                  │
        ▼                                  ▼
  zacks_fundamentals   ── joined on ticker ──  financials / companies   (Supabase)
        │
        ▼
   screener.html  (computes derived metrics client-side)
```

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` → `.env` and fill in:
   - `SUPABASE_URL`, `SUPABASE_KEY` (service-role key, for the sync job)
   - `ZACKS_EMAIL`, `ZACKS_PASSWORD`, optionally `ZACKS_SCREENER_URL`
3. Create the table: run `schema.sql` in the Supabase SQL editor.
4. Load data: `python zacks_sync.py`
   - Re-parse an already-downloaded file: `python zacks_sync.py path/to/export.xlsx`
   - Headless: `HEADLESS=1 python zacks_sync.py`
5. Open `screener.html` in a browser.

### Weekly schedule (Sunday)
```
0 6 * * 0  cd /path/to/finance-project && python zacks_sync.py >> zacks_sync.log 2>&1
```

## Demo mode

`screener.html` loads from Supabase `zacks_fundamentals`. If that table is empty
or unavailable it **falls back to a deterministic synthetic dataset** that matches
the real Zacks sector distribution (5,374 companies) so the full UI is usable
without live data. A banner indicates when demo data is showing.

## Calculated metrics (computed in the browser)

All unit conversions applied per spec — Zacks EPS/sales/EBITDA are in **millions**,
EDGAR revenue/debt/CF in **billions** (Zacks millions ÷ 1000 before combining):

- **EPS Growth F0→F1 / F1→F2** with signal classification (Turnaround, Improving,
  Normal, Deteriorating, Deepening) and ±99% sentinels for sign crossings.
- **Sales Growth** = (F1 sales mil / 1000) / EDGAR revenue bil − 1
- **EBITDA Margin** = (EBITDA mil / 1000) / EDGAR revenue bil × 100
- **Debt/EBITDA** = EDGAR LT-debt bil / (EBITDA mil / 1000)
- **Interest Coverage** = EBIT / interest expense (fallback: EBIT / (EBIT − pretax))
- **CF/LT Debt** = operating CF bil / LT-debt bil
- **P/E TTM** = `pe_trailing_12_months` (not pe_f1) · **PEG** = P/E TTM / EPS growth F0→F1
