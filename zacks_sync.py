#!/usr/bin/env python3
"""
zacks_sync.py

Weekly (Sunday) automation that keeps the `zacks_fundamentals` table in
Supabase in sync with the Zacks.com screener export.

Pipeline:
  1. Selenium logs into Zacks.com with the configured credentials.
  2. Navigates to the saved custom screener and downloads the Excel export.
  3. pandas parses the workbook and normalises the column names.
  4. The rows are upserted into Supabase (on conflict: ticker -> update).

Credentials / config come from the environment (.env):
  ZACKS_EMAIL, ZACKS_PASSWORD
  SUPABASE_URL, SUPABASE_KEY   (service-role key -- bypasses RLS)

Run manually:      python zacks_sync.py
Run headless:      HEADLESS=1 python zacks_sync.py
Schedule (cron):   0 6 * * 0  cd /path/to/project && python zacks_sync.py >> zacks_sync.log 2>&1

The Zacks DOM changes from time to time. All the brittle selectors live in
the SELECTORS block below so they are easy to re-point without touching logic.
"""

import os
import sys
import glob
import time
import json
import math
from pathlib import Path
from datetime import datetime, date

import requests
import pandas as pd
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    ElementNotInteractableException,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
load_dotenv()

ZACKS_EMAIL = os.environ.get("ZACKS_EMAIL", "financep761@gmail.com")
ZACKS_PASSWORD = os.environ.get("ZACKS_PASSWORD", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

HEADLESS = os.environ.get("HEADLESS", "1") == "1"

PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = PROJECT_DIR / "zacks_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# The Zacks login form is NOT a standalone page -- /login/ serves 404 content.
# The real <form> is a hidden dropdown in the top nav of EVERY page, so we load
# the homepage and expand "Sign In" to reveal it. Override the entry URL with
# the ZACKS_LOGIN_URL env var if needed.
ZACKS_LOGIN_URL = os.environ.get(
    "ZACKS_LOGIN_URL",
    "https://www.zacks.com/",
)
# The saved screener page. Point this at your own saved screen URL; the
# default is the generic custom stock screener landing page.
ZACKS_SCREENER_URL = os.environ.get(
    "ZACKS_SCREENER_URL",
    "https://www.zacks.com/screening/stock-screener",
)

# All Zacks-DOM-dependent selectors in one place for easy maintenance.
SELECTORS = {
    # The login form is a hidden JS dropdown in the nav; click this to expand it.
    "signin_expand": (By.CSS_SELECTOR, "a.signin_expand, #log_me_in a"),
    "login_username": (By.ID, "username"),   # field id="username" (Username or Email Address)
    "login_password": (By.ID, "password"),
    "login_submit": (By.CSS_SELECTOR,
        "#login_form input[type=submit], #login_form button[type=submit], input#login-submit"),
    "logged_in_marker": (By.CSS_SELECTOR, "a[href*='logout'], a[href*='logoff'], #user_menu"),
    # Cookie / GDPR consent accept button (OneTrust, TrustArc, or generic).
    "cookie_accept": (By.CSS_SELECTOR,
        "#onetrust-accept-btn-handler, #truste-consent-button, "
        ".truste-button2, button[aria-label*='accept' i], button[title*='accept' i]"),
    # On the screener page, the "Run Query" then "Excel" export controls.
    "run_query": (By.ID, "btn_run_query"),
    "export_excel": (By.CSS_SELECTOR, "a#screener_export, a[href*='export'][href*='excel']"),
}

# Map raw Zacks export header -> our DB column. Zacks headers vary in case /
# spacing between exports, so we normalise (lower + collapse non-alnum to _)
# before looking up in this alias table.
COLUMN_ALIASES = {
    "ticker": "ticker",
    "symbol": "ticker",
    "company_name": "company_name",
    "company": "company_name",
    "exchange": "exchange",
    "sector": "sector",
    "industry": "industry",
    "market_cap_mil": "market_cap_mil",
    "market_cap_millions": "market_cap_mil",
    "market_cap": "market_cap_mil",
    "pe_trailing_12_months": "pe_trailing_12_months",
    "p_e_trailing_12_months": "pe_trailing_12_months",
    "pe_ttm": "pe_trailing_12_months",
    "peg_ratio": "peg_ratio",
    "peg": "peg_ratio",
    "f0_consensus_est": "f0_consensus_est",
    "f1_consensus_est": "f1_consensus_est",
    "f2_consensus_est": "f2_consensus_est",
    "f1_consensus_sales_est_mil": "f1_consensus_sales_est_mil",
    "ebitda_mil": "ebitda_mil",
    "ebitda": "ebitda_mil",
    "quick_ratio": "quick_ratio",
    "debt_total_capital": "debt_total_capital",
    "debt_to_total_capital": "debt_total_capital",
    "eps_growth_q1_vs_q1": "eps_growth_q1_vs_q1",
}

DB_COLUMNS = [
    "ticker", "company_name", "exchange", "sector", "industry",
    "market_cap_mil", "pe_trailing_12_months", "peg_ratio",
    "f0_consensus_est", "f1_consensus_est", "f2_consensus_est",
    "f1_consensus_sales_est_mil", "ebitda_mil", "quick_ratio",
    "debt_total_capital", "eps_growth_q1_vs_q1",
]


# --------------------------------------------------------------------------
# Selenium: log in + download the screener Excel
# --------------------------------------------------------------------------
def build_driver():
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    prefs = {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    opts.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(options=opts)
    # Allow downloads in headless Chrome.
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(DOWNLOAD_DIR)},
        )
    except Exception:
        pass
    return driver


def dismiss_overlays(driver):
    """Best-effort dismissal of cookie / GDPR / consent banners that overlay
    the login form (e.g. the "Accept All" / "Deny Optional" bar). A missing
    banner is the happy path, so all errors are swallowed. Returns True if
    something was clicked."""
    # 1) Fast path: known consent-widget IDs/classes (OneTrust, TrustArc, ...).
    by, sel = SELECTORS["cookie_accept"]
    try:
        for el in driver.find_elements(by, sel):
            if el.is_displayed():
                el.click()
                print("  dismissed consent banner")
                time.sleep(1)
                return True
    except Exception:
        pass
    # 2) Text match across buttons / links / role=button. Prefer "Accept All".
    accept_labels = ("accept all", "accept", "allow all", "agree", "i agree",
                     "got it", "ok")
    try:
        for el in driver.find_elements(By.XPATH, "//button | //a | //*[@role='button']"):
            try:
                if not el.is_displayed():
                    continue
                label = (el.text or "").strip().lower()
                if label in accept_labels or label.startswith("accept"):
                    el.click()
                    print(f"  dismissed consent banner ('{el.text.strip()}')")
                    time.sleep(1)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def type_into(driver, wait, selector, text, label):
    """Wait until the field is clickable, scroll it into view, then type -- with
    a JS value-set fallback if the element still refuses direct interaction."""
    el = wait.until(EC.element_to_be_clickable(selector))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
    time.sleep(0.3)                       # let any scroll/animation settle
    try:
        el.clear()
        el.click()
        el.send_keys(text)
    except ElementNotInteractableException:
        print(f"  {label}: send_keys not interactable -> JS value fallback")
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input',  {bubbles: true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            el, text,
        )
    return el


def dump_debug(driver, tag):
    """Save a screenshot + page source so the blocking element is visible even
    on a headless run (open the .png/.html afterwards to see the real page)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png = PROJECT_DIR / f"debug_{tag}_{ts}.png"
    htmlf = PROJECT_DIR / f"debug_{tag}_{ts}.html"
    try:
        driver.save_screenshot(str(png))
        htmlf.write_text(driver.page_source, encoding="utf-8")
        print(f"  DEBUG saved: {png.name}  {htmlf.name}")
    except Exception as e:
        print(f"  DEBUG dump failed: {e}")


def dump_clickables(driver):
    """List every candidate action control (button / submit input / link) with
    its id, class, and visible text so we can pinpoint the real Run/Export
    selectors instead of guessing. Writes a full file and prints the entries
    that look Run/Export-related."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PROJECT_DIR / f"debug_clickables_{ts}.txt"
    js = r"""
      const rows = [];
      const els = document.querySelectorAll(
        "button, input[type=submit], input[type=button], a, [role=button]");
      for (const e of els) {
        const r = e.getBoundingClientRect();
        const vis = r.width > 0 && r.height > 0 && e.offsetParent !== null;
        const txt = (e.innerText || e.value || e.getAttribute("aria-label") || "")
          .trim().replace(/\s+/g, " ").slice(0, 60);
        if (!txt && !e.id) continue;
        rows.push([vis ? "VIS" : "hid", e.tagName.toLowerCase(),
                   "id=" + (e.id || "-"),
                   "class=" + (e.className ? e.className.toString() : "-"),
                   "text=" + txt].join(" | "));
      }
      return rows.join("\n");
    """
    try:
        listing = driver.execute_script(js)
        out.write_text(listing, encoding="utf-8")
        kw = ("run", "export", "excel", "csv", "download", "search", "screen", "add")
        hits = [ln for ln in listing.splitlines() if any(w in ln.lower() for w in kw)]
        print("  candidate Run/Export controls:")
        for ln in hits[:30]:
            print("    " + ln)
        print(f"  full clickable list -> {out.name}")
    except Exception as e:
        print(f"  dump_clickables failed: {e}")


def login(driver):
    print(f"Logging into Zacks (headless={HEADLESS})...")
    driver.get(ZACKS_LOGIN_URL)          # homepage; the login form lives in the nav
    wait = WebDriverWait(driver, 25)

    # Cookie/GDPR banner overlays the nav -> clear it before opening the dropdown.
    dismiss_overlays(driver)

    try:
        # The real login form is a hidden JS dropdown in the top nav. Click
        # "Sign In" to expand it; the fields only become interactable after.
        expander = wait.until(EC.element_to_be_clickable(SELECTORS["signin_expand"]))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", expander)
        expander.click()

        # Some builds only toggle the dropdown on a JS-dispatched click.
        try:
            WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable(SELECTORS["login_username"]))
        except TimeoutException:
            print("  sign-in dropdown not open after click -> JS click fallback")
            driver.execute_script("arguments[0].click();", expander)

        type_into(driver, wait, SELECTORS["login_username"], ZACKS_EMAIL, "username")
        type_into(driver, wait, SELECTORS["login_password"], ZACKS_PASSWORD, "password")
        wait.until(EC.element_to_be_clickable(SELECTORS["login_submit"])).click()
        wait.until(EC.presence_of_element_located(SELECTORS["logged_in_marker"]))
        print("  login OK")
    except (TimeoutException, ElementNotInteractableException):
        # Capture what the page actually looked like, then re-raise.
        dump_debug(driver, "login_failure")
        print("  login FAILED -- inspect the debug_login_failure_*.png/.html "
              "artifacts, or re-run visibly with HEADLESS=0 python zacks_sync.py")
        raise


def download_export(driver):
    print("Opening screener and exporting Excel...")
    before = set(glob.glob(str(DOWNLOAD_DIR / "*")))
    driver.get(ZACKS_SCREENER_URL)
    wait = WebDriverWait(driver, 30)

    # A consent banner can also overlay the export controls on this page.
    dismiss_overlays(driver)

    # Run the saved query if the button is present, then trigger the export.
    clicked = {}
    for key in ("run_query", "export_excel"):
        try:
            el = wait.until(EC.element_to_be_clickable(SELECTORS[key]))
            el.click()
            clicked[key] = True
            print(f"  clicked '{key}'")
            time.sleep(3)
        except Exception as e:
            clicked[key] = False
            print(f"  note: could not click '{key}' ({e.__class__.__name__})")

    # If the export never fired, capture the real DOM so we can fix the
    # selectors -- and bail out early rather than burning 120s on a download
    # that will never arrive.
    if not clicked.get("export_excel"):
        dump_debug(driver, "screener_page")
        dump_clickables(driver)
        raise RuntimeError(
            "Could not drive the screener export. Saved screener_page debug "
            "artifacts + a clickable-elements list; update "
            "SELECTORS['run_query'] / SELECTORS['export_excel'] from those "
            "(the flow may also need a criterion added before Run appears).")

    # Wait for a new .xls/.xlsx to finish downloading (no .crdownload lock).
    path = _wait_for_download(before, timeout=120)
    print(f"  downloaded: {path}")
    return path


def _wait_for_download(before, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = set(glob.glob(str(DOWNLOAD_DIR / "*.xls"))) | \
                  set(glob.glob(str(DOWNLOAD_DIR / "*.xlsx")))
        new = [p for p in current if p not in before]
        # Make sure nothing is still mid-download.
        in_progress = glob.glob(str(DOWNLOAD_DIR / "*.crdownload"))
        if new and not in_progress:
            return max(new, key=os.path.getmtime)
        time.sleep(1)
    raise TimeoutError("Zacks export did not finish downloading in time")


# --------------------------------------------------------------------------
# Parse + normalise
# --------------------------------------------------------------------------
def _norm(header):
    out = []
    for ch in str(header).strip().lower():
        out.append(ch if ch.isalnum() else "_")
    # collapse repeated underscores
    s = "".join(out)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def parse_excel(path):
    print(f"Parsing {path} ...")
    df = pd.read_excel(path)
    # Rename via the alias table.
    rename = {}
    for col in df.columns:
        key = _norm(col)
        if key in COLUMN_ALIASES:
            rename[col] = COLUMN_ALIASES[key]
    df = df.rename(columns=rename)

    # Keep only columns we know; add any missing DB columns as NaN.
    for c in DB_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df = df[DB_COLUMNS]

    # Clean up: drop rows with no ticker, dedupe on ticker (keep last).
    df = df[df["ticker"].notna()].copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df = df.drop_duplicates(subset=["ticker"], keep="last")

    # Coerce numerics; strip $ , % that sometimes leak into exports.
    numeric_cols = [c for c in DB_COLUMNS if c not in
                    ("ticker", "company_name", "exchange", "sector", "industry")]
    for c in numeric_cols:
        df[c] = (df[c].astype(str)
                 .str.replace(r"[\$,%]", "", regex=True)
                 .str.replace(",", "", regex=False)
                 .str.strip())
        df[c] = pd.to_numeric(df[c], errors="coerce")

    print(f"  parsed {len(df)} rows across {df['sector'].nunique()} sectors")
    return df


# --------------------------------------------------------------------------
# Upsert to Supabase
# --------------------------------------------------------------------------
def _clean_record(rec):
    out = {}
    for k, v in rec.items():
        if isinstance(v, float) and math.isnan(v):
            out[k] = None
        elif pd.isna(v):
            out[k] = None
        else:
            out[k] = v
    return out


def upsert_supabase(df, table="zacks_fundamentals", on_conflict=None, chunk=500):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE_URL / SUPABASE_KEY not set -- skipping upload.")
        return
    endpoint = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:                       # composite-key upsert (history table)
        endpoint += f"?on_conflict={on_conflict}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        # merge-duplicates => upsert on the conflict target (PK by default)
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    records = [_clean_record(r) for r in df.to_dict(orient="records")]
    total = 0
    for i in range(0, len(records), chunk):
        batch = records[i:i + chunk]
        r = requests.post(endpoint, headers=headers, data=json.dumps(batch))
        if r.status_code >= 300:
            print(f"  ERROR {r.status_code}: {r.text[:400]}")
            r.raise_for_status()
        total += len(batch)
        print(f"  upserted {total}/{len(records)} -> {table}")
    print(f"Done. {total} rows in {table}.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    started = datetime.now()
    print(f"=== Zacks sync started {started:%Y-%m-%d %H:%M:%S} ===")

    # Allow re-parsing an already-downloaded file: `python zacks_sync.py file.xlsx`
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        path = sys.argv[1]
    else:
        driver = build_driver()
        try:
            login(driver)
            path = download_export(driver)
        finally:
            driver.quit()

    df = parse_excel(path)

    # a. current snapshot -- overwrite in place (unchanged behavior)
    upsert_supabase(df, "zacks_fundamentals")

    # b. dated historical snapshot -- append a new dated copy. Override the date
    #    with SNAPSHOT_DATE=YYYY-MM-DD when backfilling an older export.
    snap = os.environ.get("SNAPSHOT_DATE") or date.today().isoformat()
    hist = df.copy()
    hist.insert(1, "snapshot_date", snap)
    upsert_supabase(hist, "zacks_fundamentals_history", on_conflict="ticker,snapshot_date")
    print(f"  history snapshot_date = {snap}")

    print(f"=== Finished in {(datetime.now() - started).seconds}s ===")


if __name__ == "__main__":
    main()
