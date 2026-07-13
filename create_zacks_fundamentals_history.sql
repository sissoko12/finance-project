create table if not exists public.zacks_fundamentals_history (
    "snapshot_date" date not null,
    "company_name" text,
    "ticker" text,
    "market_cap_mil" double precision,
    "exchange" text,
    "sector" text,
    "industry" text,
    "shares_outstanding_mil" double precision,
    "last_close" double precision,
    "beta" double precision,
    "f0_consensus_est" double precision,
    "f1_consensus_est" double precision,
    "f2_consensus_est" double precision,
    "f1_consensus_sales_est_mil" double precision,
    "pe_trailing_12_months" double precision,
    "pe_f1" double precision,
    "pe_f2" double precision,
    "peg_ratio" double precision,
    "price_cash_flow" double precision,
    "price_book" double precision,
    "price_sales" double precision,
    "current_roi_ttm" double precision,
    "current_roe_ttm" double precision,
    "current_roa_ttm" double precision,
    "annual_sales_mil" double precision,
    "cost_of_goods_sold_mil" double precision,
    "ebitda_mil" double precision,
    "ebit_mil" double precision,
    "pretax_income_mil" double precision,
    "net_income_mil" double precision,
    "cash_flow_mil" double precision,
    "dividend" double precision,
    "inventory_turnover" double precision,
    "inventory_mil" double precision,
    "receivables_mil" double precision,
    "intangibles_mil" double precision,
    "current_assets_mil" double precision,
    "current_liabilities_mil" double precision,
    "long_term_debt_mil" double precision,
    "preferred_equity_mil" double precision,
    "common_equity_mil" double precision,
    "book_value" double precision,
    "debt_total_capital" double precision,
    "debt_equity_ratio" double precision,
    "current_ratio" double precision,
    "quick_ratio" double precision,
    "cash_ratio" double precision,
    "div_yield_pct" double precision,
    primary key ("ticker", "snapshot_date")
);

create index if not exists idx_zacks_fundamentals_history_ticker on public.zacks_fundamentals_history ("ticker");

-- Additive: add any columns missing from an existing zacks_fundamentals_history.
alter table public.zacks_fundamentals_history add column if not exists "company_name" text;
alter table public.zacks_fundamentals_history add column if not exists "ticker" text;
alter table public.zacks_fundamentals_history add column if not exists "market_cap_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "exchange" text;
alter table public.zacks_fundamentals_history add column if not exists "sector" text;
alter table public.zacks_fundamentals_history add column if not exists "industry" text;
alter table public.zacks_fundamentals_history add column if not exists "shares_outstanding_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "last_close" double precision;
alter table public.zacks_fundamentals_history add column if not exists "beta" double precision;
alter table public.zacks_fundamentals_history add column if not exists "f0_consensus_est" double precision;
alter table public.zacks_fundamentals_history add column if not exists "f1_consensus_est" double precision;
alter table public.zacks_fundamentals_history add column if not exists "f2_consensus_est" double precision;
alter table public.zacks_fundamentals_history add column if not exists "f1_consensus_sales_est_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "pe_trailing_12_months" double precision;
alter table public.zacks_fundamentals_history add column if not exists "pe_f1" double precision;
alter table public.zacks_fundamentals_history add column if not exists "pe_f2" double precision;
alter table public.zacks_fundamentals_history add column if not exists "peg_ratio" double precision;
alter table public.zacks_fundamentals_history add column if not exists "price_cash_flow" double precision;
alter table public.zacks_fundamentals_history add column if not exists "price_book" double precision;
alter table public.zacks_fundamentals_history add column if not exists "price_sales" double precision;
alter table public.zacks_fundamentals_history add column if not exists "current_roi_ttm" double precision;
alter table public.zacks_fundamentals_history add column if not exists "current_roe_ttm" double precision;
alter table public.zacks_fundamentals_history add column if not exists "current_roa_ttm" double precision;
alter table public.zacks_fundamentals_history add column if not exists "annual_sales_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "cost_of_goods_sold_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "ebitda_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "ebit_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "pretax_income_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "net_income_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "cash_flow_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "dividend" double precision;
alter table public.zacks_fundamentals_history add column if not exists "inventory_turnover" double precision;
alter table public.zacks_fundamentals_history add column if not exists "inventory_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "receivables_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "intangibles_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "current_assets_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "current_liabilities_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "long_term_debt_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "preferred_equity_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "common_equity_mil" double precision;
alter table public.zacks_fundamentals_history add column if not exists "book_value" double precision;
alter table public.zacks_fundamentals_history add column if not exists "debt_total_capital" double precision;
alter table public.zacks_fundamentals_history add column if not exists "debt_equity_ratio" double precision;
alter table public.zacks_fundamentals_history add column if not exists "current_ratio" double precision;
alter table public.zacks_fundamentals_history add column if not exists "quick_ratio" double precision;
alter table public.zacks_fundamentals_history add column if not exists "cash_ratio" double precision;
alter table public.zacks_fundamentals_history add column if not exists "div_yield_pct" double precision;
