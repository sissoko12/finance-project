import os
import requests
import time
from pathlib import Path

USER_AGENT = os.environ.get('SEC_USER_AGENT', 'Eliran Cohen eliran.eliran35@gmail.com')

TICKERS = {
    "JPM":  "0000019617",  # JPMorgan Chase
    "BAC":  "0000070858",  # Bank of America
    "WFC":  "0000072971",  # Wells Fargo
    "C":    "0000831001",  # Citigroup
    "GS":   "0000886982",  # Goldman Sachs
    "MS":   "0000895421",  # Morgan Stanley
    "USB":  "0000036104",  # U.S. Bancorp
    "PNC":  "0000713676",  # PNC Financial
    "TFC":  "0000092230",  # Truist Financial
    "COF":  "0000927628",  # Capital One
    "STT":  "0000093751",  # State Street
    "BK":   "0001390777",  # BNY Mellon
    "SCHW": "0000316709",  # Charles Schwab
}

FORMS = ["10-K"]
START_DATE = "1998-01-01"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def get_json(url):
    r = session.get(url)
    time.sleep(0.2)
    r.raise_for_status()
    return r.json()


def collect_filings(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = get_json(url)

    all_filings = []

    def add_block(block):
        n = len(block["form"])
        for i in range(n):
            all_filings.append({
                "form": block["form"][i],
                "date": block["filingDate"][i],
                "accession": block["accessionNumber"][i],
                "doc": block["primaryDocument"][i],
            })

    add_block(data["filings"]["recent"])

    older_files = data["filings"].get("files", [])
    for entry in older_files:
        older_url = f"https://data.sec.gov/submissions/{entry['name']}"
        older_data = get_json(older_url)
        add_block(older_data)

    return all_filings


summary = {}

for ticker, cik in TICKERS.items():
    print(f"\n=== {ticker} (CIK={cik}) ===")
    try:
        all_filings = collect_filings(cik)
    except Exception as e:
        print(f"  ERROR fetching filing list: {e}")
        summary[ticker] = {"error": str(e)}
        continue

    matched = [
        f for f in all_filings
        if f["form"] in FORMS and f["date"] >= START_DATE
    ]
    matched.sort(key=lambda f: f["date"])
    print(f"  {len(matched)} matching 10-K filings since {START_DATE}")

    out_root = Path("data") / ticker
    downloaded = 0
    skipped = 0
    failed = 0

    for f in matched:
        accession_nodash = f["accession"].replace("-", "")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{f['doc']}"
        dest_dir = out_root / f["form"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(f["doc"]).suffix or ".htm"
        dest_path = dest_dir / f"{f['date']}_{f['accession']}{ext}"

        if dest_path.exists():
            skipped += 1
            continue

        try:
            r = session.get(doc_url)
            time.sleep(0.2)
            if r.status_code == 200:
                dest_path.write_bytes(r.content)
                downloaded += 1
                print(f"    saved: {dest_path}")
            else:
                failed += 1
                print(f"    FAILED ({r.status_code}): {doc_url}")
        except Exception as e:
            failed += 1
            print(f"    ERROR: {e}")

    summary[ticker] = {"matched": len(matched), "downloaded": downloaded, "skipped": skipped, "failed": failed}

print("\n\n=== FINAL SUMMARY ===")
for ticker, stats in summary.items():
    print(f"{ticker}: {stats}")
