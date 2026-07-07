#!/usr/bin/env python3
"""
Phase 4: Fetch latest news for all 13 bank tickers via TickerTick API.
Saves: data/news/news.json
On failure: logs error and leaves existing news.json intact.
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE     = Path(__file__).resolve().parent.parent
NEWS_DIR = BASE / "data" / "news"
NEWS_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = NEWS_DIR / "news.json"

TICKERS = ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF","STT","BK","SCHW"]

# Build TickerTick OR query (lowercase ticker symbols)
TT_TICKERS = " ".join(f"tt:{t.lower()}" for t in TICKERS)
API_URL = f"https://api.tickertick.com/feed?q=(or {TT_TICKERS})&n=40"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def similar(a, b):
    """Simple similarity check to deduplicate near-identical headlines."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    # Jaccard similarity on word sets
    wa = set(re.findall(r'\w+', a))
    wb = set(re.findall(r'\w+', b))
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) > 0.75


def deduplicate(stories):
    seen = []
    for s in stories:
        title = s.get('title', '')
        if not any(similar(title, prev.get('title', '')) for prev in seen):
            seen.append(s)
    return seen


def extract_tickers_from_tags(tags):
    """Pick which of our 13 tickers are mentioned in the story's tags."""
    found = []
    if not tags:
        return found
    tag_str = ' '.join(str(t) for t in tags).upper()
    for ticker in TICKERS:
        if ticker in tag_str or f"TT:{ticker}" in tag_str:
            found.append(ticker)
    return found


def parse_story(raw):
    """Convert TickerTick story object to our schema."""
    # TickerTick response fields: id, title, url, site, time, tickers, tags, description
    published_ts = raw.get('time', 0) or 0
    if isinstance(published_ts, (int, float)):
        # TickerTick returns Unix ms
        if published_ts > 1e12:
            published_ts /= 1000
        try:
            pub_dt = datetime.fromtimestamp(published_ts, tz=timezone.utc)
            published_str = pub_dt.isoformat()
        except Exception:
            published_str = ''
    else:
        published_str = str(published_ts)

    tickers_in_story = extract_tickers_from_tags(
        raw.get('tickers', []) or raw.get('tags', [])
    )
    # Also try the top-level 'tickers' list if it's a list of dicts
    if not tickers_in_story:
        raw_tickers = raw.get('tickers') or []
        if isinstance(raw_tickers, list):
            for t in raw_tickers:
                if isinstance(t, str):
                    if t.upper() in TICKERS:
                        tickers_in_story.append(t.upper())
                elif isinstance(t, dict):
                    sym = (t.get('ticker') or t.get('symbol') or '').upper()
                    if sym in TICKERS:
                        tickers_in_story.append(sym)

    return {
        "title":     raw.get('title', '').strip(),
        "url":       raw.get('url', ''),
        "source":    raw.get('site', raw.get('source', 'Unknown')),
        "published": published_str,
        "tickers":   list(dict.fromkeys(tickers_in_story)),  # deduplicate, preserve order
    }


def fetch():
    log.info("Fetching from TickerTick …")
    log.info("URL: %s", API_URL)

    try:
        resp = requests.get(API_URL, timeout=20, headers={"User-Agent": "bank-dashboard/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Request failed: %s", exc)
        return None

    try:
        data = resp.json()
    except Exception as exc:
        log.error("JSON parse failed: %s", exc)
        log.error("Response text (first 500): %s", resp.text[:500])
        return None

    # TickerTick returns {"stories": [...]} or just a list
    if isinstance(data, dict):
        stories_raw = data.get('feed', data.get('stories', data.get('articles', [])))
    elif isinstance(data, list):
        stories_raw = data
    else:
        log.error("Unexpected response shape: %s", type(data))
        return None

    log.info("Raw stories fetched: %d", len(stories_raw))

    stories = [parse_story(s) for s in stories_raw if s.get('title')]
    stories = [s for s in stories if s['title']]                # drop empties
    stories = deduplicate(stories)
    stories.sort(key=lambda s: s['published'], reverse=True)    # newest first
    stories = stories[:30]

    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "headlines":    stories,
    }


def main():
    result = fetch()
    if result is None:
        log.error("Fetch failed — leaving existing news.json intact.")
        sys.exit(1)

    OUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    log.info("✓ Saved %d headlines to %s", len(result['headlines']), OUT_FILE)

    log.info("\nFirst 5 headlines:")
    for h in result['headlines'][:5]:
        tickers_str = f" [{','.join(h['tickers'])}]" if h['tickers'] else ''
        log.info("  %s%s  (%s)", h['title'][:70], tickers_str, h['source'])


if __name__ == '__main__':
    main()
