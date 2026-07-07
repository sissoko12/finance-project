import os
import sys
import json
import time
import random
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client
from dotenv import load_dotenv

# Credentials come from .env (same pattern as zacks_sync.py / risk_pipeline.py).
# Project ref daafhufkauamzrvyxfwg -- no hardcoded keys.
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("SUPABASE_URL / SUPABASE_KEY not set -- add them to .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_field(page, label):
    """Get value of a field by its label text using nextElementSibling"""
    try:
        val = page.evaluate(f"""
            Array.from(document.querySelectorAll('*')).find(
                el => el.childNodes.length === 1 && el.textContent.trim() === '{label}'
            )?.nextElementSibling?.textContent?.trim()
        """)
        return val
    except Exception:
        return None

def parse_num(val):
    """Parse a numeric string to float"""
    if not val or val in ['NA', '--', 'N/A', '']:
        return None
    try:
        return float(val.replace(',', '').replace('%', '').replace('$', '').replace('B', '').strip())
    except Exception:
        return None

def scrape_zacks_playwright(ticker, page):
    try:
        page.goto(f'https://www.zacks.com/stock/quote/{ticker}', timeout=30000)
        page.wait_for_timeout(5000)
        
        html = page.content()
        if len(html) < 10000:
            return None
            
        soup = BeautifulSoup(html, 'html.parser')
        result = {"ticker": ticker}

        # ── Market Cap & Beta via JS ──────────────────────────────────────────
        mc = get_field(page, 'Market Cap')
        bt = get_field(page, 'Beta')
        div = get_field(page, 'Dividend')
        
        if mc:
            # Format: "4,342.02 B" -> parse as billions
            mc_clean = mc.replace(',', '').replace('B', '').strip()
            try:
                result["market_cap_b"] = float(mc_clean)
            except Exception:
                pass
        
        if bt:
            result["beta"] = parse_num(bt)
            
        if div:
            # Format: "1.08 ( 0.37%)"
            parts = div.split('(')
            if parts:
                result["dividend"] = parse_num(parts[0].strip())
            if len(parts) > 1:
                result["dividend_yield"] = parse_num(parts[1].replace(')', '').strip())

        # ── Sector & Industry ─────────────────────────────────────────────────
        sector_link = soup.find('a', href=lambda h: h and 'sector' in str(h).lower())
        if sector_link:
            result["sector"] = sector_link.get_text(strip=True)
            
        industry_link = soup.find('a', href=lambda h: h and 'industry' in str(h).lower() and 'rank' not in str(h).lower())
        if industry_link:
            result["industry"] = industry_link.get_text(strip=True)

        # ── Zacks Rank ────────────────────────────────────────────────────────
        rank_p = soup.find('p', class_='rank_view')
        if rank_p:
            text = rank_p.get_text()
            for i in range(1, 6):
                if f'{i}-' in text:
                    result["zacks_rank"] = i
                    break

        # ── Parse tables ──────────────────────────────────────────────────────
        tables = soup.find_all('table')
        for t in tables:
            rows = t.find_all('tr')
            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
                if not cells or len(cells) < 2:
                    continue

                # EPS estimates Q1 Q2 F1 F2
                if cells[0] == 'Current' and len(cells) >= 5:
                    try:
                        result["eps_est_q1"] = parse_num(cells[1])
                        result["eps_est_q2"] = parse_num(cells[2])
                        result["eps_est_f1"] = parse_num(cells[3])
                        result["eps_est_f2"] = parse_num(cells[4])
                    except Exception:
                        pass

                # EPS surprise reported
                if cells[0] == 'Reported' and len(cells) >= 5:
                    try:
                        result["eps_reported_q0"] = parse_num(cells[1])
                        result["eps_reported_q1"] = parse_num(cells[2])
                        result["eps_reported_q2"] = parse_num(cells[3])
                        result["eps_reported_q3"] = parse_num(cells[4])
                    except Exception:
                        pass

                # EPS surprise estimate
                if cells[0] == 'Estimate' and len(cells) >= 3:
                    try:
                        result["eps_estimate_q0"] = parse_num(cells[1])
                        result["eps_estimate_q1"] = parse_num(cells[2])
                    except Exception:
                        pass

                # VGM Scores
                if cells[0] == 'Value Score' and len(cells) >= 2:
                    grades = [c for c in cells[1:] if c in ['A', 'B', 'C', 'D', 'F']]
                    if len(grades) >= 1: result["value_score"] = grades[0]
                    if len(grades) >= 2: result["growth_score"] = grades[1]
                    if len(grades) >= 3: result["momentum_score"] = grades[2]

                # Valuation & ratios
                label = cells[0]
                val = cells[1] if len(cells) > 1 else ''
                
                label_clean = label.split('More Info')[0].strip()
                
                mapping = {
                    'P/E (F1)': 'pe_f1',
                    'PEG Ratio': 'peg_ratio',
                    'Price/Book (P/B)': 'price_book',
                    'Price/Sales (P/S)': 'price_sales',
                    'Price/Cash Flow': 'price_cf',
                    'EV/EBITDA': 'ev_ebitda',
                    'Debt/Equity': 'debt_equity',
                    'Current Ratio': 'current_ratio',
                    'Debt/Capital': 'debt_capital',
                    'Net Margin': 'net_margin',
                    'Return on Equity': 'roe',
                    'Return on Assets': 'roa',
                    'Return on Investment': 'roi',
                    'Proj. Sales Growth (F1/F0)': 'sales_growth_f1',
                    '52 Week Price Chg': 'price_chg_52w',
                    'Hist. Cash Flow Growth': 'cf_growth',
                    'Cash Flow ($/share)': 'cf_per_share',
                }
                
                for lbl, key in mapping.items():
                    if lbl in label_clean and val and val not in ['NA', '--']:
                        try:
                            result[key] = parse_num(val)
                        except Exception:
                            pass

        return result if len(result) > 3 else None

    except Exception as e:
        return None


def run_scraper(limit=None, max_consecutive_failures=8):
    print("Fetching tickers from database...")
    # Project is already scoped to the S&P 500 companies table, and that table
    # has no `exchange` column -> pull ALL tickers (no exchange filter). Grab
    # `name` too so we can populate company_name without re-scraping it.
    r = supabase.table("companies").select("ticker,name").execute()
    name_by_ticker = {row["ticker"]: row.get("name") for row in r.data}
    tickers = list(name_by_ticker.keys())
    if limit:
        tickers = tickers[:limit]

    print(f"Scraping {len(tickers):,} companies with Playwright...")
    success = 0
    failed = 0
    consecutive_failures = 0
    aborted = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for i, ticker in enumerate(tickers):
            data = scrape_zacks_playwright(ticker, page)

            ok = False
            if data:
                # company_name comes from our own companies table, not Zacks.
                data["company_name"] = name_by_ticker.get(ticker)
                try:
                    supabase.table("zacks_stock_details").upsert(data, on_conflict="ticker").execute()
                    success += 1
                    ok = True
                except Exception:
                    failed += 1
            else:
                failed += 1

            # Consecutive-failure guard: a CAPTCHA/block (or a DB outage) shows
            # up as a run of failures. Reset on any success; abort if we hit the
            # threshold so a mid-run block doesn't silently burn the rest.
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    aborted = True
                    print(f"\n⛔ Aborting at {ticker} ({i+1}/{len(tickers)}): "
                          f"{consecutive_failures} consecutive failures. Likely a "
                          f"CAPTCHA/block or DB issue -- stopping so we don't waste "
                          f"the remaining tickers. Progress so far is saved.")
                    break

            if (i + 1) % 50 == 0:
                print(f"Progress: {i+1}/{len(tickers)} | ✅ {success} | ❌ {failed}")

            time.sleep(random.uniform(2.0, 4.0))

        browser.close()

    status = "⛔ Aborted early." if aborted else "🎉 Done!"
    print(f"\n{status} Success: {success:,} | Failed: {failed:,}")


def test_scrape():
    """Quick 3-ticker smoke test -- prints scraped data, writes NOTHING to the DB."""
    print("Testing with AAPL, MSFT, NVDA...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for ticker in ["AAPL", "MSFT", "NVDA"]:
            data = scrape_zacks_playwright(ticker, page)
            print(f"\n{ticker}:", json.dumps(data, indent=2))
            time.sleep(3)

        browser.close()


if __name__ == "__main__":
    #   python zacks_scraper.py            -> 3-ticker smoke test (no DB writes)
    #   python zacks_scraper.py --run      -> full 498-ticker scrape + upsert
    #   python zacks_scraper.py --run 25   -> full flow, first 25 tickers only
    args = sys.argv[1:]
    if args and args[0] == "--run":
        limit = int(args[1]) if len(args) > 1 else None
        run_scraper(limit=limit)
    else:
        test_scrape()