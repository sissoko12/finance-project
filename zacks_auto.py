"""
zacks_auto.py
=============
Automates the Zacks (free) Stock Screener end-to-end and loads the result into Supabase.

Flow (fundamentals only)
------------------------
1. Log into zacks.com (Playwright / Chromium).
2. Open the Stock Screener (https://www.zacks.com/screening/stock-screener) which is
   embedded as an iframe from screener-api.zacks.com.
3. SINGLE SCREEN, ALL COLUMNS, ONE EXPORT -- FILTER and DISPLAY kept separate:
   Adding a metric as a New-Screen CRITERION filters out companies that lack it, even with a
   permissive operator (an all-45-criteria screen returned only the ~700 companies having
   EVERY metric -- 0% blanks). Zacks' Edit View is the correct tool: it selects DISPLAY columns
   without filtering. So every run rebuilds the screen fresh:
     a) New Screen -> open ONLY the "Size & Share Volume" category (matched by name) and add
        ONE permissive filter: Market Cap (mil) >= 0 (keeps ~all companies). No other category
        is opened or scanned.
     b) Edit View  -> tick all 45 wanted line items as DISPLAY columns (no filtering);
     c) run once, export once via screener-api.zacks.com/export.php.
   Companies missing a metric are kept and show blank for that column (verified real export:
   PEG blank ~52%, Inventory Turnover ~44%, P/E TTM ~27%). Everything is matched by VISIBLE
   LABEL (IDs discovered from the live DOM, never hardcoded).
   Diagnostics (no screen run/export/load):
     `--discover-view` opens the filter category, switches to Edit View, and dumps every
                       selectable column + a ✓/✗ match report for the 45 wanted labels.
     `--verify-view`   actually TICKS all 45 Edit View columns, then re-reads each one's
                       persisted .checked state and prints a per-column pass/fail table --
                       proving the ticks registered/saved before any full run. No export.
4. Keep only NYSE + Nasdaq (~5,800 companies).
5. Load the single table into Supabase:
     * `zacks_fundamentals`         - the LATEST snapshot (upsert on ticker; overwritten).
     * `zacks_fundamentals_history` - APPEND-ONLY weekly history, one row per
                                       (ticker, snapshot_date), building a time series.

The old 34-download per-metric merge (UNIVERSE run + one screen per metric) has been retired.

Notes
-----
* The free Zacks screener has no literal "Download" link in the page chrome; the CSV comes
  from the DataTables export endpoint (export.php) which uses the logged-in session cookies.
* The Supabase key here cannot run DDL (CREATE/ALTER TABLE). The script writes the matching
  `create_zacks_fundamentals*.sql` files; run them once in the Supabase SQL editor if the
  table schema does not yet match the CSV columns.
* ADAPTED FOR finance-project: reads SUPABASE_URL / SUPABASE_KEY (not REACT_APP_*) from our
  .env, and the generated SQL is NON-DESTRUCTIVE (create-if-not-exists + add-column-if-not-
  exists, never DROP) so it can never wipe our existing zacks_fundamentals* tables.

Requires: playwright, pandas, supabase, python-dotenv  (and `playwright install chromium`).
"""

import datetime
import os
import re
import sys
import time

import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ZACKS_EMAIL = os.getenv("ZACKS_EMAIL")
ZACKS_PASSWORD = os.getenv("ZACKS_PASSWORD")

if not all([SUPABASE_URL, SUPABASE_KEY, ZACKS_EMAIL, ZACKS_PASSWORD]):
    raise SystemExit(
        "Missing credentials. Set SUPABASE_URL, SUPABASE_KEY, "
        "ZACKS_EMAIL and ZACKS_PASSWORD in a .env file.")

HERE = os.path.dirname(os.path.abspath(__file__))
FUND_CSV_PATH = os.path.join(HERE, "zacks_export_fundamentals.csv")
FUND_SQL_PATH = os.path.join(HERE, "create_zacks_fundamentals.sql")
FUND_HISTORY_SQL_PATH = os.path.join(HERE, "create_zacks_fundamentals_history.sql")

SCREENER_URL = "https://www.zacks.com/screening/stock-screener"
EXPORT_URL = "https://screener-api.zacks.com/export.php"

# Where --discover-view dumps the Edit View selectable columns (label -> control id).
EDITVIEW_DUMP_PATH = os.path.join(HERE, "zacks_editview_columns.txt")

# The screen is built FRESH every run, and crucially separates FILTER from DISPLAY:
#   * New Screen criteria = FILTERS (they drop non-matching companies). We add exactly
#     ONE, permissive: Market Cap (mil) >= 0, so virtually every company matches.
#   * Edit View = DISPLAY COLUMNS only (no filtering). We tick all 45 wanted line items
#     there, so companies missing a metric are KEPT and show blank for that column.
# Everything is matched by VISIBLE LABEL (not internal ID), discovered from the live
# DOM at runtime. Company Name + Ticker are always in the export, so not listed here.
WANTED_LABELS = [
    # dropdowns (classification) -- added permissively so every value is kept
    "Exchange", "Sector", "Industry",
    # size
    "Market Cap (mil)", "Shares Outstanding (mil)", "Last Close", "Beta",
    # estimates
    "F0 Consensus Est.", "F1 Consensus Est.", "F2 Consensus Est.",
    "F(1) Consensus Sales Est. ($mil)",
    # valuation
    "P/E (Trailing 12 Months)", "P/E (F1)", "P/E (F2)", "PEG Ratio",
    "Price/Cash Flow", "Price/Book", "Price/Sales",
    # returns
    "Current ROI (TTM)", "Current ROE (TTM)", "Current ROA (TTM)",
    # income statement
    "Annual Sales ($mil)", "Cost of Goods Sold ($mil)", "EBITDA ($mil)", "EBIT ($mil)",
    "Pretax Income ($mil)", "Net Income ($mil)", "Cash Flow ($mil)",
    "Dividend",
    # balance sheet
    "Inventory Turnover", "Inventory ($mil)", "Receivables ($mil)", "Intangibles ($mil)",
    "Current Assets ($mil)", "Current Liabilities ($mil)", "Long Term Debt ($mil)",
    "Preferred Equity ($mil)", "Common Equity ($mil)", "Book Value",
    # leverage / liquidity
    "Debt/Total Capital", "Debt/Equity Ratio", "Current Ratio", "Quick Ratio", "Cash Ratio",
    "Div. Yield %",
]

# The ONE permissive filter criterion added under New Screen. Market Cap (mil) >= 0
# keeps virtually every company (only negative/blank market caps drop out). It lives
# in the "Size & Share Volume" category -- we open ONLY that category (matched by its
# visible name, not a hardcoded id), never scanning the other 17.
FILTER_CATEGORY_NAME = "Size & Share Volume"
FILTER_LABEL = "Market Cap (mil)"
FILTER_VALUE = "0"

KEEP_EXCHANGES = {"NYSE", "NSDQ"}


def norm_label(text):
    """Normalise a criterion label for tolerant matching between the export headers
    and the builder's on-page text: lowercase, drop $, unify separators, collapse
    all runs of whitespace/underscores to a single space. So 'Net Income  ($mil)'
    and 'Net Income ($mil)' -> the same key, and 'Debt/Total Capital' ignores the
    slash spacing."""
    t = str(text).lower().replace("$", "")
    # safety net: strip the builder's accessible-label boilerplate if it survives
    t = re.sub(r"^change select box content for\s*", "", t)
    t = re.sub(r"[/()]", " ", t)
    t = re.sub(r"[\s_]+", " ", t)
    return t.strip()

# Locked to the real zacks_overall_market.xlsx export (47 columns, in export order).
# Every name is sanitize_column() applied to the ground-truth header; verified
# collision-free. Any column the export gains later still loads (it's just appended
# after this list) -- update here to re-pin the order.
FUND_COLUMN_ORDER = [
    "company_name", "ticker", "market_cap_mil", "exchange", "sector", "industry",
    "shares_outstanding_mil", "last_close", "beta",
    "f0_consensus_est", "f1_consensus_est", "f2_consensus_est", "f1_consensus_sales_est_mil",
    "pe_trailing_12_months", "pe_f1", "pe_f2", "peg_ratio",
    "price_cash_flow", "price_book", "price_sales",
    "current_roi_ttm", "current_roe_ttm", "current_roa_ttm",
    "annual_sales_mil", "cost_of_goods_sold_mil", "ebitda_mil", "ebit_mil",
    "pretax_income_mil", "net_income_mil", "cash_flow_mil",
    "dividend", "inventory_turnover", "inventory_mil", "receivables_mil", "intangibles_mil",
    "current_assets_mil", "current_liabilities_mil", "long_term_debt_mil",
    "preferred_equity_mil", "common_equity_mil", "book_value",
    "debt_total_capital", "debt_equity_ratio",
    "current_ratio", "quick_ratio", "cash_ratio", "div_yield_pct",
]

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def dismiss_consent(page):
    for sel in ['button:has-text("Agree")', 'button:has-text("Accept")',
                '.fc-cta-consent', 'button:has-text("Consent")']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def login(page):
    print("→ Logging into Zacks...")
    page.goto("https://www.zacks.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    dismiss_consent(page)
    visible = False
    for _ in range(5):
        try:
            page.click("#log_me_in", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        visible = page.evaluate(
            "() => { const f = document.querySelector(\"#login_form input[name='username']\");"
            "return !!(f && (f.offsetWidth || f.offsetHeight || f.getClientRects().length)); }")
        if visible:
            break
        dismiss_consent(page)
    if not visible:
        raise RuntimeError("Could not reveal the Zacks login form (#login_form).")
    page.fill("#login_form input[name='username']", ZACKS_EMAIL)
    page.fill("#login_form input[name='password']", ZACKS_PASSWORD)
    page.click("#login_form input[type='submit']")
    page.wait_for_timeout(6000)
    if page.query_selector("a[href*='logout']") is None:
        raise RuntimeError("Zacks login failed - no logout link found. Check credentials.")
    print("  ✓ Logged in.")


def get_screener_frame(page):
    print("→ Opening the Stock Screener...")
    page.goto(SCREENER_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(10000)
    dismiss_consent(page)
    page.wait_for_timeout(2000)
    for f in page.frames:
        if f.name == "screenerContent" or "screener-api" in (f.url or ""):
            print("  ✓ Screener iframe loaded.")
            return f
    raise RuntimeError("Could not find the screener iframe.")


def run_screen(page, frame):
    print("→ Running the screen...")
    responses = []
    page.on("response",
            lambda r: responses.append(r) if "getrunscreendata.php" in r.url else None)
    frame.evaluate(
        """() => {
            try { document.querySelector('#is_only_matches').value = '0'; } catch (e) {}
            const bs = [...document.querySelectorAll('#run_screen_result')];
            const visible = bs.find(b => b.offsetParent !== null) || bs[bs.length - 1];
            if (visible) visible.click();
        }""")
    page.wait_for_timeout(15000)

    body = ""
    for r in responses:
        try:
            body = r.text()
        except Exception:
            pass
    m = re.search(r'([\d,]+)\s*Matches', body)
    matches = m.group(1) if m else "unknown"
    print(f"  ✓ Screen run complete - {matches} matches.")
    return matches


def _read_category_criteria(frame):
    """After a category is loaded, list every criterion it offers as {pid, label}.
    The pid comes from the Add link's `add_criteria_<pid>` class; the label is the
    visible row text with the operator/value controls and the word 'Add' stripped."""
    return frame.evaluate(
        r"""() => {
            const out = [];
            document.querySelectorAll('[class*="add_criteria_"]').forEach(link => {
                const m = /add_criteria_(\d+)/.exec(link.className);
                if (!m) return;
                const pid = m[1];
                const row = link.closest('tr, li, .criteria_row, div') || link.parentElement;
                // Prefer an explicit label cell; else the row text minus controls.
                let label = '';
                if (row) {
                    const lab = row.querySelector('label, .criteria_name, td');
                    label = (lab ? lab.innerText : row.innerText) || '';
                }
                label = label.replace(/\s+/g, ' ')
                             // the row's accessible text prefixes the criterion
                             // name with this boilerplate -- strip it to the name
                             .replace(/^change select box content for\s*/i, '')
                             .replace(/\bAdd\b/gi, '')
                             .trim();
                out.push({ pid, label });
            });
            return out;
        }""")


def _open_category_by_name(page, frame, name):
    """Open exactly one criteria category, matched by its visible NAME (not a hardcoded
    id). Returns the numeric category id from its getCriteriaAll(<id>) handler, or ''."""
    cat = frame.evaluate(
        r"""(name) => {
            const want = name.toLowerCase();
            const els = [...document.querySelectorAll('[onclick*="getCriteriaAll"]')];
            const el = els.find(e => (e.textContent||'').replace(/\s+/g,' ').trim()
                                      .toLowerCase() === want)
                    || els.find(e => (e.textContent||'').toLowerCase().includes(want));
            if (!el) return '';
            const m = /getCriteriaAll\(\s*(\d+)/.exec(el.getAttribute('onclick')||'');
            el.click();
            return m ? m[1] : '?';
        }""", name)
    page.wait_for_timeout(1400)
    return cat


def add_market_cap_filter(page, frame):
    """Add the SINGLE permissive filter criterion (Market Cap (mil) >= 0). Opens ONLY
    the 'Size & Share Volume' category and finds Market Cap there by label -- no
    all-category scan. This is the only real filter; Edit View supplies the columns."""
    print(f"→ New Screen: opening '{FILTER_CATEGORY_NAME}' for the sole filter...")
    cat = _open_category_by_name(page, frame, FILTER_CATEGORY_NAME)
    if not cat:
        raise RuntimeError(f"Could not find category {FILTER_CATEGORY_NAME!r} in the builder.")
    pid = None
    for item in _read_category_criteria(frame):
        if norm_label(item["label"]) == norm_label(FILTER_LABEL):
            pid = item["pid"]
            break
    if not pid:
        raise RuntimeError(
            f"{FILTER_LABEL!r} not found in category {FILTER_CATEGORY_NAME!r} (cat {cat}).")
    print(f"→ Adding sole filter: {FILTER_LABEL} >= {FILTER_VALUE} (pid {pid}, cat {cat})")
    frame.evaluate(
        """(args) => {
            const { pid, val } = args;
            const op = document.querySelector('#op_'+pid);
            if (op) { const ge=[...op.options].find(o=>o.value=='6');
                      op.value = ge ? '6' : (op.options[0] && op.options[0].value); }
            const v = document.querySelector('#val_'+pid);
            if (v) v.value = val;
        }""", {"pid": pid, "val": FILTER_VALUE})
    link = frame.query_selector(f"a.add_criteria_{pid}")
    if not link:
        raise RuntimeError(f"No add link a.add_criteria_{pid} for the filter criterion.")
    link.click()
    page.wait_for_timeout(800)
    print("  ✓ Filter added.")


def open_edit_view(page, frame):
    """Switch the screener to Edit View and WAIT for its column panel to populate.
    The tab's <li> has no handler -- the real one is the inner <a onclick="openTab
    ('edit-view')">, and openTab() calls getEditview(...) to fill #edit_view via AJAX.
    So we invoke openTab('edit-view') directly (fallback: click that anchor), then poll
    until the panel has content. Returns True once populated."""
    print("→ Switching to Edit View...")
    switched = frame.evaluate(
        r"""() => {
            // the site's own tab fn populates the panel (calls getEditview)
            if (typeof openTab === 'function') { try { openTab('edit-view'); return 'openTab()'; } catch(e){} }
            // fallback: click the anchor that actually calls openTab('edit-view')
            const a = [...document.querySelectorAll('a[onclick]')].find(
                e => /openTab\(\s*['"]edit-view['"]\s*\)/.test(e.getAttribute('onclick') || ''));
            if (a) { a.click(); return 'click-anchor'; }
            return '';
        }""")
    if not switched:
        print("  ! Could not trigger the Edit View switch (openTab not found).")
        return False

    # Poll until #edit_view / #edit-view fills or checkboxes appear (AJAX is async).
    populated = False
    for _ in range(24):
        page.wait_for_timeout(750)
        state = frame.evaluate(
            r"""() => {
                const box = document.querySelector('#edit_view, #edit-view');
                const len = box ? box.innerText.replace(/\s+/g,'').length : 0;
                return len + '|' + document.querySelectorAll('input[type=checkbox]').length;
            }""")
        length, cbs = (int(x) for x in state.split("|"))
        if cbs > 0 or length > 40:
            populated = True
            break
    print(f"  {'✓' if populated else '!'} Edit View via {switched} "
          f"({'populated' if populated else 'STILL EMPTY -- inspect the dump'}).")
    return populated


def _read_category_nav(frame):
    """Read the shared left-hand category list once (no category is loaded here).
    Each li#criteria_cat_<id> carries getCriteriaAll(<id>,'A','<name>'); in Edit View
    mode clicking it (or calling getEditview(<id>,<name>)) renders that category's
    display columns. Returns [{cat, name}, ...] in page order."""
    return frame.evaluate(
        r"""() => {
            const out = [], seen = new Set();
            document.querySelectorAll('li[id^="criteria_cat_"]').forEach(li => {
                const m = /getCriteriaAll\(\s*(\d+)\s*,\s*'[^']*'\s*,\s*'([^']*)'/
                          .exec(li.getAttribute('onclick') || '');
                // the category list is rendered twice (e.g. desktop + mobile copies);
                // de-dup by cat id so each category is walked once.
                if (m && !seen.has(m[1])) {
                    seen.add(m[1]);
                    out.push({ cat: m[1], name: m[2].replace(/&amp;/g, '&') });
                }
            });
            return out;
        }""")


def _read_edit_view_checkboxes(frame):
    """Read the Edit View columns for the CURRENTLY loaded category. Each is an
    <input id="editView<pid>" ... name="check_value[]"> inside a <fieldset> whose
    <legend class="criteria-name"><h3> holds the visible label (tooltip text is
    hidden, so innerText gives just the name). Returns [{pid, id, label, checked}]."""
    return frame.evaluate(
        r"""() => {
            const out = [];
            document.querySelectorAll('input[id^="editView"]').forEach(cb => {
                const fs = cb.closest('fieldset');
                let label = '';
                if (fs) { const h = fs.querySelector('legend .criteria-name h3, legend h3, h3');
                          if (h) label = h.innerText; }
                if (!label) { const tr = cb.closest('tr'); if (tr) label = tr.innerText; }
                label = (label || '').replace(/\s+/g, ' ').trim();
                out.push({ pid: cb.id.replace('editView', ''), id: cb.id,
                           label, checked: cb.checked });
            });
            return out;
        }""")


def _load_edit_view_category(page, frame, cat, name):
    """Render one category's Edit View columns by invoking the site's own
    getEditview(<cat>, <name>) (fallback: click li#criteria_cat_<cat>). We first rename
    any existing editView<pid> ids to 'stale_...' so the fresh AJAX render is always
    detectable -- even when re-selecting the already-open category. Returns True once
    fresh editView checkboxes appear."""
    frame.evaluate(
        r"""() => document.querySelectorAll('input[id^="editView"]')
                    .forEach(c => { c.id = 'stale_' + c.id; })""")
    frame.evaluate(
        r"""(args) => {
            if (typeof getEditview === 'function') {
                try { getEditview(parseInt(args.cat, 10), args.name); return; } catch (e) {}
            }
            const li = document.getElementById('criteria_cat_' + args.cat);
            if (li) li.click();
        }""", {"cat": cat, "name": name})
    for _ in range(20):
        page.wait_for_timeout(400)
        n = frame.evaluate(r"""() => document.querySelectorAll('input[id^="editView"]').length""")
        if n > 0:
            return True
    return False


def discover_edit_view(page, frame, dump=False):
    """DIAGNOSTIC: open Edit View, walk EVERY category, and aggregate every offered
    display column into {normalised_label -> {pid, id, cat, label, checked}}. With
    dump=True, write the full list to EDITVIEW_DUMP_PATH and a ✓/✗ report for the 45."""
    open_edit_view(page, frame)
    nav = _read_category_nav(frame)
    print(f"  · walking {len(nav)} categories to catalogue every Edit View column...")
    view_map, rows_dump = {}, []
    for entry in nav:
        cat, name = entry["cat"], entry["name"]
        if not _load_edit_view_category(page, frame, cat, name):
            # first category is already loaded by open_edit_view; a no-change is fine there
            pass
        cols = _read_edit_view_checkboxes(frame)
        for c in cols:
            if not c["label"]:
                continue
            key = norm_label(c["label"])
            if key not in view_map:
                view_map[key] = {**c, "cat": cat, "cat_name": name}
                rows_dump.append({**c, "cat": cat, "cat_name": name})
        print(f"    · {name:32} (cat {cat}): {len(cols)} columns  [total {len(view_map)}]")

    print(f"  ✓ Edit View exposes {len(view_map)} distinct columns across {len(nav)} categories.")

    if dump:
        lines = [f"{c['cat']}\t{c['pid']}\t{c['id']}\t{int(c['checked'])}\t{c['label']}"
                 for c in rows_dump]
        with open(EDITVIEW_DUMP_PATH, "w") as fh:
            fh.write("cat\tpid\tid\tchecked\tlabel\n" + "\n".join(lines) + "\n")
        print(f"  · wrote {len(rows_dump)} Edit View columns -> {EDITVIEW_DUMP_PATH}")
        print("\n--- WANTED column match report (Edit View) ---")
        hit = miss = 0
        for want in WANTED_LABELS:
            c = view_map.get(norm_label(want))
            if c:
                hit += 1
                print(f"  ✓ {want:42} -> {c['id']} (cat {c['cat']} {c['cat_name']}) "
                      f"checked={c['checked']}")
            else:
                miss += 1
                print(f"  ✗ {want:42} -> NO MATCH")
        print(f"  {hit} matched, {miss} unmatched of {len(WANTED_LABELS)}.")
    return view_map


def select_edit_view_columns(page, frame):
    """Tick all 45 WANTED_LABELS columns in Edit View, then VERIFY each one really
    registered. Edit View is category-paginated and each tick auto-saves via
    save_checkbox(pid) (there is NO separate Apply button). Two passes:
      1. TICK   -- walk categories, click each wanted checkbox, confirm .checked right
                   after the click (catches clicks that silently don't register).
      2. VERIFY -- reload every category that holds a wanted column and re-read each
                   checkbox's .checked from the FRESH server render (catches saves that
                   didn't persist across category navigation).
    Prints a per-item table and returns the set of verified-checked labels. Raises if
    any wanted column ends up NOT verified, so a full run never proceeds on a stale view."""
    open_edit_view(page, frame)
    wanted = {norm_label(w): w for w in WANTED_LABELS}
    nav = _read_category_nav(frame)

    # ---- Pass 1: tick (and immediate post-click check) ----
    tick = {}   # key -> {"status", "pid", "label", "cat"}
    print("  Pass 1 — ticking checkboxes:")
    for entry in nav:
        cat, name = entry["cat"], entry["name"]
        _load_edit_view_category(page, frame, cat, name)
        for c in _read_edit_view_checkboxes(frame):
            key = norm_label(c["label"])
            if key not in wanted or key in tick:
                continue
            if c["checked"]:
                status = "already"
            else:
                frame.evaluate(
                    r"""(id) => { const cb = document.getElementById(id);
                                  if (cb && !cb.checked) cb.click(); }""", c["id"])
                page.wait_for_timeout(300)   # let save_checkbox() AJAX persist
                now = frame.evaluate(
                    r"""(id) => { const cb = document.getElementById(id);
                                  return cb ? cb.checked : false; }""", c["id"])
                status = "ticked" if now else "CLICK-FAILED"
            tick[key] = {"status": status, "pid": c["pid"], "label": c["label"], "cat": cat}
        print(f"    · {name:32} (cat {cat}): {len(tick)}/{len(wanted)} handled")
        if len(tick) == len(wanted):
            break

    # ---- Pass 2: verify persistence via a fresh render of each relevant category ----
    need_cats = sorted({v["cat"] for v in tick.values()}, key=int)
    verified = {}   # key -> bool (.checked after reload)
    print(f"  Pass 2 — re-reading persisted state across {len(need_cats)} categories:")
    for cat in need_cats:
        name = next((e["name"] for e in nav if e["cat"] == cat), cat)
        _load_edit_view_category(page, frame, cat, name)
        for c in _read_edit_view_checkboxes(frame):
            key = norm_label(c["label"])
            if key in wanted:
                verified[key] = c["checked"]

    # ---- Per-item report ----
    print("\n  --- Edit View tick verification (per column) ---")
    ok_count = 0
    for w in WANTED_LABELS:
        key = norm_label(w)
        t = tick.get(key)
        vchecked = verified.get(key)
        good = vchecked is True
        ok_count += 1 if good else 0
        st = t["status"] if t else "NO-CHECKBOX"
        pid = t["pid"] if t else "-"
        print(f"    {'✓' if good else '✗'} {w:40} pid={pid:<6} click={st:<12} "
              f"verified_checked={vchecked}")
    failed = [w for w in WANTED_LABELS if verified.get(norm_label(w)) is not True]
    print(f"\n  {ok_count}/{len(WANTED_LABELS)} columns VERIFIED checked after re-render.")
    if failed:
        print(f"  ✗ NOT verified ({len(failed)}): {failed}")
        raise RuntimeError(
            f"{len(failed)} Edit View column(s) did not verify as checked; aborting so "
            "the screen is not run/exported with an incomplete view. See the table above.")
    print("  ✓ All 45 columns verified checked — the view is correctly configured.")
    return verified


def download_csv(context, path=FUND_CSV_PATH):
    print(f"→ Downloading CSV export -> {os.path.basename(path)} ...")
    resp = context.request.get(EXPORT_URL)
    if resp.status != 200:
        raise RuntimeError(f"export.php returned HTTP {resp.status}")
    data = resp.body()
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"  ✓ Saved {len(data):,} bytes -> {path}")
    return path


def sanitize_column(name):
    n = name.strip().lower()
    n = re.sub(r"\b([fq])\((\d+)\)", r"\1\2", n)
    n = n.replace("p/e", "pe").replace("p/b", "pb").replace("p/s", "ps")
    n = n.replace("p/cf", "pcf").replace("%", " pct ").replace("#", " num ")
    n = n.replace("&", " and ").replace("/", " ").replace("$", " ")
    n = re.sub(r"[()\.\-,'\"]", " ", n)
    n = re.sub(r"\s+", "_", n.strip())
    n = re.sub(r"_+", "_", n).strip("_")
    if not n:
        n = "col"
    if n[0].isdigit():
        n = "c_" + n
    return n


def load_dataframe(csv_path):
    df = pd.read_csv(csv_path, dtype=str)
    df.columns = _unique([sanitize_column(c) for c in df.columns])
    if "ticker" not in df.columns:
        raise RuntimeError(f"No 'ticker' column found. Columns: {list(df.columns)}")
    df = df.dropna(subset=["ticker"])
    df = df.drop_duplicates(subset=["ticker"], keep="first")
    return df


def _unique(cols):
    seen, out = {}, []
    for c in cols:
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


def infer_sql_type(series):
    vals = series.dropna()
    vals = vals[vals.str.strip().str.upper().isin(["", "NA", "N/A", "--"]) == False]
    if len(vals) == 0:
        return "text"
    cleaned = vals.str.replace(",", "", regex=False).str.replace("$", "", regex=False)
    ok = pd.to_numeric(cleaned, errors="coerce").notna().mean()
    return "double precision" if ok >= 0.9 else "text"


def to_records(df, types):
    out = df.copy()
    for col in out.columns:
        if types.get(col) == "double precision":
            cleaned = (out[col].astype(str)
                       .str.replace(",", "", regex=False)
                       .str.replace("$", "", regex=False))
            out[col] = pd.to_numeric(cleaned, errors="coerce")
    records = out.where(pd.notnull(out), None).to_dict(orient="records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, float) and pd.isna(v):
                rec[k] = None
    return records


def _write_table_sql(df, types, table, path, pk_cols):
    # NON-DESTRUCTIVE (adapted): never DROP our existing table. Emit CREATE TABLE
    # IF NOT EXISTS (so a fresh project still bootstraps) followed by ADD COLUMN
    # IF NOT EXISTS for every column. Run against our existing zacks_fundamentals
    # this only ADDS missing columns -- it never touches existing data, column
    # types, indexes, RLS policies or the updated_at trigger.
    composite = len(pk_cols) > 1
    lines = [f"create table if not exists public.{table} ("]
    defs = []
    for col in df.columns:
        t = types.get(col, "text")
        if not composite and col == pk_cols[0]:
            defs.append(f'    "{col}" {t} primary key')
        else:
            defs.append(f'    "{col}" {t}')
    if composite:
        defs.append("    primary key (" + ", ".join(f'"{c}"' for c in pk_cols) + ")")
    lines.append(",\n".join(defs))
    lines.append(");")
    lines.append("")
    lines.append(f"-- Additive: add any columns missing from an existing {table}.")
    for col in df.columns:
        t = types.get(col, "text")
        lines.append(f'alter table public.{table} add column if not exists "{col}" {t};')
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"  · wrote schema SQL -> {path}")


def _upsert(records, table, on_conflict, sql_path, types=None):
    types = types or {}
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    records = [dict(r) for r in records]
    dropped = []

    for _ in range(80):
        try:
            sb.table(table).upsert(records[0], on_conflict=on_conflict).execute()
            break
        except Exception as e:
            msg = str(e)
            m = re.search(r"Could not find the '([^']+)' column", msg)
            if m:
                col = m.group(1)
                for r in records:
                    r.pop(col, None)
                dropped.append(col)
                continue
            print(f"\n‼ Could not write to {table} (table missing).")
            print(f"  Error: {msg[:300]}")
            print(f"  Fix: run {sql_path} in the Supabase SQL editor, then re-run with --load-only.\n")
            return False
    if dropped:
        print(f"\n  ! {table} is MISSING {len(dropped)} column(s); their data was NOT loaded: {dropped}")
        print("    Run these in the Supabase SQL editor, then re-run with --load-only:")
        for col in dropped:
            print(f'      alter table public.{table} add column if not exists "{col}" {types.get(col, "double precision")};')
        print()

    n, B = 0, 500
    for i in range(0, len(records), B):
        batch = records[i:i + B]
        sb.table(table).upsert(batch, on_conflict=on_conflict).execute()
        n += len(batch)
        print(f"  · upserted {n}/{len(records)} into {table}")
    print(f"  ✓ Loaded {n:,} rows into {table}.")
    return True


def _write_history_table_sql(df, types, table, path):
    # NON-DESTRUCTIVE (adapted): mirrors _write_table_sql -- create-if-not-exists
    # plus additive add-column-if-not-exists, never DROP. Preserves our existing
    # (ticker, snapshot_date) PK, captured_at default and RLS on the real table.
    cols_text = ("ticker", "company_name", "sector", "exchange", "industry")
    lines = [f"create table if not exists public.{table} (",
             '    "snapshot_date" date not null,']
    defs = []
    for col in df.columns:
        t = "text" if col in cols_text else types.get(col, "text")
        defs.append(f'    "{col}" {t}')
    defs.append('    primary key ("ticker", "snapshot_date")')
    lines.append(",\n".join(defs))
    lines.append(");")
    lines.append("")
    lines.append(f'create index if not exists idx_{table}_ticker on public.{table} ("ticker");')
    lines.append("")
    lines.append(f"-- Additive: add any columns missing from an existing {table}.")
    for col in df.columns:
        t = "text" if col in cols_text else types.get(col, "text")
        lines.append(f'alter table public.{table} add column if not exists "{col}" {t};')
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"  · wrote history schema SQL -> {path}")


def load_fundamentals_df(df):
    ordered = [c for c in FUND_COLUMN_ORDER if c in df.columns]
    df = df[ordered + [c for c in df.columns if c not in ordered]]
    types = {}
    for col in df.columns:
        types[col] = "text" if col in ("ticker", "company_name", "sector", "exchange", "industry") \
            else infer_sql_type(df[col])
    _write_table_sql(df, types, "zacks_fundamentals", FUND_SQL_PATH, ["ticker"])
    _write_history_table_sql(df, types, "zacks_fundamentals_history", FUND_HISTORY_SQL_PATH)
    records = to_records(df, types)
    _upsert(records, "zacks_fundamentals", "ticker", FUND_SQL_PATH, types)
    today = datetime.date.today().isoformat()
    hist = [{**r, "snapshot_date": today} for r in records]
    print(f"→ Appending fundamentals snapshot to zacks_fundamentals_history (date: {today})...")
    _upsert(hist, "zacks_fundamentals_history", "ticker,snapshot_date", FUND_HISTORY_SQL_PATH, types)


def load_fundamentals_from_csv():
    if not os.path.exists(FUND_CSV_PATH):
        print(f"  (no fundamentals CSV at {FUND_CSV_PATH} - run a full scrape first)")
        return
    df = load_dataframe(FUND_CSV_PATH)
    print(f"→ Parsed fundamentals CSV: {len(df):,} rows × {len(df.columns)} columns")
    load_fundamentals_df(df)


def build_fundamentals_df(page, context):
    print("\n--- Filter (Market Cap>=0) + Edit View columns → run → export ---")
    frame = get_screener_frame(page)
    add_market_cap_filter(page, frame)          # New Screen: sole filter (Size cat only)
    select_edit_view_columns(page, frame)       # Edit View: tick all 45 across categories
    run_screen(page, frame)
    download_csv(context, FUND_CSV_PATH)
    df = load_dataframe(FUND_CSV_PATH)
    print(f"  ✓ export: {len(df):,} companies × {len(df.columns)} columns")

    if "exchange" in df.columns:
        before = len(df)
        df = df[df["exchange"].isin(KEEP_EXCHANGES)].copy()
        print(f"  ✓ filtered to NYSE+Nasdaq: {len(df):,} of {before:,}")
    return df


def _new_context(p, headless):
    browser = p.chromium.launch(
        headless=headless, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context(
        user_agent=USER_AGENT, viewport={"width": 1400, "height": 900},
        accept_downloads=True, locale="en-US")
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return browser, context, page


def discover_view(headless=True):
    # Diagnostic only: add the Market Cap filter (Edit View needs a built screen to
    # populate its column list), switch to Edit View, dump every selectable column +
    # the WANTED match report, then stop. No run, no export, no Supabase writes.
    print("=== Zacks Edit View discovery (no run, no load) ===")
    with sync_playwright() as p:
        browser, _, page = _new_context(p, headless)
        login(page)
        frame = get_screener_frame(page)
        add_market_cap_filter(page, frame)          # Size & Share Volume only
        discover_edit_view(page, frame, dump=True)  # straight to Edit View
        browser.close()
    print("=== Edit View discovery done. Review the match report above + "
          f"{os.path.basename(EDITVIEW_DUMP_PATH)}. ===")


def verify_view(headless=True):
    # Dry run: add the Market Cap filter, open Edit View, actually TICK all 45 columns,
    # then verify each one's persisted .checked state and print a per-column table.
    # Does NOT run the screen, export, or write to Supabase -- it only configures the
    # (server-saved) view, which is exactly what a real run would do before exporting.
    print("=== Zacks Edit View tick + verify (no screen run, no export, no load) ===")
    with sync_playwright() as p:
        browser, _, page = _new_context(p, headless)
        login(page)
        frame = get_screener_frame(page)
        add_market_cap_filter(page, frame)
        select_edit_view_columns(page, frame)   # ticks + two-pass verification + report
        browser.close()
    print("=== Verify done. If all 45 verified checked, a full run is safe. ===")


def run(headless=True):
    print("=== Zacks Screener → Supabase (fundamentals, all companies) ===")
    with sync_playwright() as p:
        browser, context, page = _new_context(p, headless)
        login(page)
        df = build_fundamentals_df(page, context)
        browser.close()
    df.to_csv(FUND_CSV_PATH, index=False)
    print(f"\n→ Saved result: {len(df):,} rows × {len(df.columns)} cols -> {FUND_CSV_PATH}")
    load_fundamentals_df(df)
    print("=== Done. ===")


if __name__ == "__main__":
    headed = "--headed" in sys.argv
    if "--load-only" in sys.argv:
        print("=== Load-only: loading existing fundamentals CSV into Supabase ===")
        load_fundamentals_from_csv()
        print("=== Done. ===")
    elif "--discover-view" in sys.argv:
        discover_view(headless=not headed)
    elif "--verify-view" in sys.argv:
        verify_view(headless=not headed)
    else:
        run(headless=not headed)
