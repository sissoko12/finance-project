import requests
import time
from pathlib import Path

USER_AGENT = "Eliran Cohen eliran.eliran35@gmail.com"

TICKER = "JPM"
CIK = "0000019617"  # JPMorgan Chase
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
    """Pulls recent filings PLUS all older paginated filing files."""
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

    # Older filings live in separate files, listed under filings.files
    older_files = data["filings"].get("files", [])
    print(f"Found {len(older_files)} additional older filing-history file(s) to fetch...")
    for entry in older_files:
        older_url = f"https://data.sec.gov/submissions/{entry['name']}"
        print(f"  fetching {older_url}")
        older_data = get_json(older_url)
        add_block(older_data)

    return all_filings


print(f"Fetching FULL filing list for {TICKER}...")
all_filings = collect_filings(CIK)
print(f"Total filings retrieved (all types, all history): {len(all_filings)}")

matched = [
    f for f in all_filings
    if f["form"] in FORMS and f["date"] >= START_DATE
]
matched.sort(key=lambda f: f["date"])

print(f"Found {len(matched)} matching 10-K/8-K filings since {START_DATE}")

out_root = Path("data") / TICKER
downloaded = 0
skipped = 0
failed = 0

for f in matched:
    accession_nodash = f["accession"].replace("-", "")
    doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accession_nodash}/{f['doc']}"
    dest_dir = out_root / f["form"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(f["doc"]).suffix or ".htm"
    dest_path = dest_dir / f"{f['date']}_{f['accession']}{ext}"

    if dest_path.exists():
        skipped += 1
        continue

    r = session.get(doc_url)
    time.sleep(0.2)
    if r.status_code == 200:
        dest_path.write_bytes(r.content)
        downloaded += 1
        print(f"  saved: {dest_path}")
    else:
        failed += 1
        print(f"  FAILED ({r.status_code}): {doc_url}")

print(f"\nDone. Downloaded={downloaded} Skipped(existing)={skipped} Failed={failed}")
