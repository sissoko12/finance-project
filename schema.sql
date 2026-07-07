-- =====================================================================
-- zacks_fundamentals
-- Weekly snapshot of the Zacks screener export (~5,400 US companies).
-- Primary key: ticker. The sync job upserts on conflict (ticker).
-- Column names mirror the Zacks Excel export 1:1 so parsing stays trivial.
-- =====================================================================

create table if not exists public.zacks_fundamentals (
    ticker                        text primary key,
    company_name                  text,
    exchange                      text,
    sector                        text,
    industry                      text,

    market_cap_mil                double precision,   -- market cap, $ millions
    pe_trailing_12_months         double precision,   -- P/E TTM (use THIS, not pe_f1)
    peg_ratio                     double precision,

    f0_consensus_est              double precision,   -- current-year EPS estimate
    f1_consensus_est              double precision,   -- next-year EPS estimate
    f2_consensus_est              double precision,   -- 2-yr forward EPS estimate
    f1_consensus_sales_est_mil    double precision,   -- next-year sales est, $ millions

    ebitda_mil                    double precision,   -- EBITDA, $ millions
    quick_ratio                   double precision,
    debt_total_capital            double precision,
    eps_growth_q1_vs_q1           double precision,

    updated_at                    timestamptz not null default now()
);

-- Indexes for the common screener filter/sort paths.
create index if not exists idx_zacks_sector    on public.zacks_fundamentals (sector);
create index if not exists idx_zacks_industry  on public.zacks_fundamentals (industry);
create index if not exists idx_zacks_mktcap    on public.zacks_fundamentals (market_cap_mil);
create index if not exists idx_zacks_pe        on public.zacks_fundamentals (pe_trailing_12_months);

-- Keep updated_at fresh on every upsert.
create or replace function public.touch_zacks_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_zacks_touch on public.zacks_fundamentals;
create trigger trg_zacks_touch
    before update on public.zacks_fundamentals
    for each row execute function public.touch_zacks_updated_at();

-- Read-only access for the dashboard's anon key (RLS-friendly).
-- Enable RLS and allow SELECT to anon; the sync job uses the service-role key
-- which bypasses RLS entirely.
alter table public.zacks_fundamentals enable row level security;

drop policy if exists "anon read zacks" on public.zacks_fundamentals;
create policy "anon read zacks"
    on public.zacks_fundamentals
    for select
    to anon
    using (true);


-- =====================================================================
-- zacks_fundamentals_history
-- Append-only weekly snapshots. Same columns as zacks_fundamentals, but
-- keyed on (ticker, snapshot_date) so every pull ADDS a dated row instead
-- of overwriting. Re-running on the same date upserts that date's rows
-- (idempotent). Backs the Screener tab's "view a past Sunday" feature.
-- =====================================================================
create table if not exists public.zacks_fundamentals_history (
    ticker                        text not null,
    snapshot_date                 date not null,          -- the Sunday of the pull
    company_name                  text,
    exchange                      text,
    sector                        text,
    industry                      text,

    market_cap_mil                double precision,
    pe_trailing_12_months         double precision,
    peg_ratio                     double precision,

    f0_consensus_est              double precision,
    f1_consensus_est              double precision,
    f2_consensus_est              double precision,
    f1_consensus_sales_est_mil    double precision,

    ebitda_mil                    double precision,
    quick_ratio                   double precision,
    debt_total_capital            double precision,
    eps_growth_q1_vs_q1           double precision,

    captured_at                   timestamptz not null default now(),

    primary key (ticker, snapshot_date)
);

-- Date-picker + per-date screener queries hit snapshot_date first.
create index if not exists idx_zacks_hist_date   on public.zacks_fundamentals_history (snapshot_date);
create index if not exists idx_zacks_hist_sector on public.zacks_fundamentals_history (snapshot_date, sector);

-- Cheap source for the "available snapshots" date picker (one row per Sunday).
create or replace view public.zacks_snapshot_dates as
select snapshot_date, count(*) as company_count
from public.zacks_fundamentals_history
group by snapshot_date
order by snapshot_date desc;

-- Read-only anon access (same pattern as zacks_fundamentals).
alter table public.zacks_fundamentals_history enable row level security;

drop policy if exists "anon read zacks history" on public.zacks_fundamentals_history;
create policy "anon read zacks history"
    on public.zacks_fundamentals_history
    for select
    to anon
    using (true);

grant select on public.zacks_snapshot_dates to anon;


-- =====================================================================
-- risk_metrics
-- Per-company liquidity-runway + synthetic-credit signals derived from
-- the XBRL facts already in `financials` (no re-pull from EDGAR).
-- Written by risk_pipeline.py. One row per (ticker, fiscal_period); the
-- most recent period per ticker carries is_latest = true so the dashboard
-- can pull the current snapshot with a single is_latest filter while the
-- prior periods remain available as an annual history.
--
-- Every displayed number is stored alongside its raw inputs (cash
-- components, EBIT, interest, opex components) so the dashboard's
-- row-click breakdown can trace each figure back to its source tags.
--
-- NOTE ON GRANULARITY: `financials` holds ANNUAL (10-K) data only, so
-- "TTM" here means the latest fiscal year, and fiscal_period is 'FY<year>'.
-- The schema is period-agnostic (TEXT) so quarterly periods can drop in
-- later without a migration if the extraction ever adds them.
-- =====================================================================

create table if not exists public.risk_metrics (
    ticker                    text    not null,
    fiscal_period             text    not null,          -- e.g. 'FY2025'
    fiscal_year               integer,
    company_name              text,
    sector                    text,
    is_financial_sector       boolean,                    -- drives which rating table is used
    is_latest                 boolean not null default false,

    -- ---- liquidity / runway ----
    cash_and_equivalents      numeric,                    -- from cash_source tag below
    cash_source               text,                       -- which cash tag was used (balance-sheet vs cash-flow line)
    short_term_investments    numeric,                    -- ShortTermInvestments / MarketableSecuritiesCurrent (0 if untagged)
    undrawn_revolver          numeric,                    -- UndrawnRevolverCapacity (0 unless disclosed)
    revolver_data_available   boolean not null default false,
    liquid_assets             numeric,                    -- sum of the three above
    ttm_cfo                   numeric,                    -- NetCashProvidedByUsedInOperatingActivities (latest FY)
    stress_opex               numeric,                    -- annual: CostOfRevenue + OperatingExpenses + SG&A
    monthly_outflow           numeric,                    -- denominator actually used (burn or stress opex / 12)
    denominator_basis         text,                       -- 'operating_cash_burn' | 'stress_opex'
    runway_mode               text
        check (runway_mode in ('burn', 'stress_overlay', 'insufficient_data')),
    runway_months             numeric,
    runway_flag               text
        check (runway_flag in ('Critical', 'Warning', 'Watch', 'Healthy', 'N/A')),

    -- ---- synthetic credit ----
    ebit                      numeric,                    -- OperatingIncomeLoss
    interest_expense          numeric,                    -- InterestExpense, or InterestPaidNet fallback
    interest_expense_source   text,                       -- 'InterestExpense' | 'InterestPaidNet' | null
    interest_coverage         numeric,                    -- EBIT / interest_expense
    total_debt                numeric,
    net_debt                  numeric,
    synthetic_rating          text,                       -- e.g. 'A2/A', 'Ba1/BB+'
    synthetic_spread          numeric,                    -- default spread over risk-free (decimal, e.g. 0.0108)
    risk_free_rate            numeric,                    -- risk-free used for implied cost of debt
    implied_cost_of_debt      numeric,                    -- risk_free_rate + synthetic_spread

    notes                     text,                       -- caveats / reason for insufficient_data
    computed_at               timestamptz not null default now(),

    primary key (ticker, fiscal_period)
);

-- Common filter/sort paths for the Risk tab.
create index if not exists idx_risk_latest       on public.risk_metrics (is_latest);
create index if not exists idx_risk_sector        on public.risk_metrics (sector);
create index if not exists idx_risk_flag          on public.risk_metrics (runway_flag);
create index if not exists idx_risk_mode          on public.risk_metrics (runway_mode);
create index if not exists idx_risk_runway_months on public.risk_metrics (runway_months);

-- Read-only access for the dashboard's anon key (same pattern as zacks).
alter table public.risk_metrics enable row level security;

drop policy if exists "anon read risk" on public.risk_metrics;
create policy "anon read risk"
    on public.risk_metrics
    for select
    to anon
    using (true);


-- =====================================================================
-- zacks_stock_details
-- Per-ticker valuation / EPS / VGM-score data scraped from each company's
-- public Zacks quote page (https://www.zacks.com/stock/quote/{ticker}) by
-- zacks_scraper.py. DISTINCT data source & field set from the screener-export
-- table zacks_fundamentals -- hence a separate table. Scoped to the S&P 500
-- tickers in `companies`. Primary key: ticker (upsert on conflict).
--
-- Note on types: zacks_rank is the bare integer 1-5 parsed from "3-Hold"-style
-- text; value/growth/momentum_score are Zacks letter grades (A-F) -> text.
-- =====================================================================
create table if not exists public.zacks_stock_details (
    ticker            text primary key,
    company_name      text,               -- populated from our companies.name
    sector            text,               -- scraped from the Zacks quote page
    industry          text,

    market_cap_b      double precision,   -- market cap, $ billions
    beta              double precision,
    dividend          double precision,
    dividend_yield    double precision,

    zacks_rank        smallint,           -- 1..5 (Strong Buy .. Strong Sell)
    value_score       text,               -- A..F letter grade
    growth_score      text,
    momentum_score    text,

    eps_est_q1        double precision,
    eps_est_q2        double precision,
    eps_est_f1        double precision,
    eps_est_f2        double precision,
    eps_reported_q0   double precision,
    eps_reported_q1   double precision,
    eps_reported_q2   double precision,
    eps_reported_q3   double precision,
    eps_estimate_q0   double precision,
    eps_estimate_q1   double precision,

    pe_f1             double precision,
    peg_ratio         double precision,
    price_book        double precision,
    price_sales       double precision,
    price_cf          double precision,
    ev_ebitda         double precision,
    debt_equity       double precision,
    current_ratio     double precision,
    debt_capital      double precision,
    net_margin        double precision,
    roe               double precision,
    roa               double precision,
    roi               double precision,
    sales_growth_f1   double precision,
    price_chg_52w     double precision,
    cf_growth         double precision,
    cf_per_share      double precision,

    updated_at        timestamptz not null default now()
);

create index if not exists idx_zsd_sector on public.zacks_stock_details (sector);
create index if not exists idx_zsd_rank   on public.zacks_stock_details (zacks_rank);

-- Reuse the shared auto-touch function defined for zacks_fundamentals above.
drop trigger if exists trg_zsd_touch on public.zacks_stock_details;
create trigger trg_zsd_touch
    before update on public.zacks_stock_details
    for each row execute function public.touch_zacks_updated_at();

-- Read-only anon access (same pattern as the other tables).
alter table public.zacks_stock_details enable row level security;

drop policy if exists "anon read zacks details" on public.zacks_stock_details;
create policy "anon read zacks details"
    on public.zacks_stock_details
    for select
    to anon
    using (true);
