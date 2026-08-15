"""German Bundesgesetzblatt (Federal Law Gazette) backfill.

Sources:
  2017-2022 — offenegesetze.de API (free, no key)
  2023+     — recht.bund.de BGBl Part I volume pages (scraping)

Usage:
  python de_backfill.py --dry-run          # count without inserting
  python de_backfill.py --since 2022       # only 2022+
  python de_backfill.py                    # full backfill 2017-now
"""
import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

DB_PATH   = Path(__file__).parent / "policies.db"
COUNTRY   = "DE"
LEVEL     = "eu"      # Germany is an EU member — shows in EU feed
SOURCE    = "bundestag"
STATUS    = "law"
HEADERS   = {"User-Agent": "CivilGate-backfill/1.0 (civic research; non-commercial)"}

YEAR_NOW  = datetime.now().year

# ── Helpers ───────────────────────────────────────────────────────────────────

def get(url, **kw):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  WARN {url}: {e}", file=sys.stderr)
        return None


def existing_ids(conn):
    cur = conn.execute("SELECT external_id FROM policies WHERE source=?", (SOURCE,))
    return {row[0] for row in cur.fetchall()}


def insert(conn, rows, dry_run=False):
    if dry_run or not rows:
        return 0
    conn.executemany(
        """INSERT OR IGNORE INTO policies
           (source, country, level, external_id, title, url,
            published_date, fetched_date, status, raw_text)
           VALUES (?,?,?,?,?,?,?,date('now'),?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


# ── Source A: offenegesetze.de (2017-2022) ────────────────────────────────────

OFFENE_URL = "https://api.offenegesetze.de/v1/veroeffentlichung/"

def fetch_offenegesetze(year, seen):
    """Yield (external_id, title, url, date) for each BGBl I item in the year."""
    page = 1
    limit = 100
    while True:
        r = get(OFFENE_URL, params={
            "format": "json",
            "kind":   "bgbl1",
            "year":   year,
            "ordering": "date",
            "limit":  limit,
            "offset": (page - 1) * limit,
        })
        if not r:
            break
        data = r.json()
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            eid = f"de_bgbl_{item['id']}"
            if eid in seen:
                continue
            date_str = item.get("date", "")[:10]
            yield eid, item.get("title", "").strip(), item.get("url", ""), date_str
        if not data.get("next"):
            break
        page += 1
        time.sleep(0.3)


# ── Source B: recht.bund.de (2023+) ──────────────────────────────────────────

RECHT_VOL = "https://www.recht.bund.de/bgbl/1/{year}/{vol}/VO.html?nn=197276"
TITLE_RE  = re.compile(
    r"<title>Bundesgesetzblatt Teil I - (.+?)(?:\s+-\s+Bundesgesetzblatt)?</title>",
    re.DOTALL,
)
DATE_RE   = re.compile(r"vom\s+(\d{2}\.\d{2}\.\d{4})")


def discover_max_vol(year):
    """Return the highest known BGBl volume number for the given year by
    scraping the listing page (shows up to 10 recent items)."""
    r = get(
        "https://www.recht.bund.de/de/bundesgesetzblatt/bgbl-1/bgbl-1_node.html",
        params={"cl2Categories_year": year},
    )
    if not r:
        return 500
    vols = re.findall(rf"bgbl/1/{year}/(\d+)/VO\.html", r.text)
    return max((int(v) for v in vols), default=500)


def fetch_recht_bund(year, seen):
    """Yield (external_id, title, url, date) for each BGBl I volume in the year."""
    max_vol   = discover_max_vol(year)
    miss_run  = 0

    for vol in range(1, max_vol + 1):
        eid = f"de_bgbl1_{year}_{vol}"
        if eid in seen:
            miss_run = 0
            continue

        url = f"https://www.recht.bund.de/bgbl/1/{year}/{vol}/VO.html?nn=197276"
        r   = get(url)
        time.sleep(0.4)

        if not r or r.status_code == 404:
            miss_run += 1
            if miss_run >= 10:
                print(f"    10 consecutive misses at vol {vol} — stopping {year}")
                break
            continue
        miss_run = 0

        title_m = TITLE_RE.search(r.text)
        title   = title_m.group(1).strip() if title_m else None
        if not title:
            continue

        date_m = DATE_RE.search(r.text)
        date_str = ""
        if date_m:
            d = date_m.group(1)
            try:
                date_str = datetime.strptime(d, "%d.%m.%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass

        yield eid, title, url, date_str


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",  action="store_true", help="Count without inserting")
    ap.add_argument("--since",    type=int, default=2017, help="Start year (default 2017)")
    ap.add_argument("--until",    type=int, default=YEAR_NOW)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    seen = existing_ids(conn)
    print(f"Existing DE entries: {len(seen)}")

    total_found  = 0
    total_insert = 0

    for year in range(args.since, args.until + 1):
        rows = []
        print(f"\n── {year} ", end="", flush=True)

        if year <= 2022:
            for eid, title, url, date_str in fetch_offenegesetze(year, seen):
                if not title:
                    continue
                raw_text = f"Titel: {title}\nQuelle: Bundesgesetzblatt Teil I, Deutschland\nJahr: {year}"
                rows.append((SOURCE, COUNTRY, LEVEL, eid, title, url, date_str, STATUS, raw_text))
                print(".", end="", flush=True)
        else:
            for eid, title, url, date_str in fetch_recht_bund(year, seen):
                raw_text = f"Titel: {title}\nQuelle: Bundesgesetzblatt Teil I, Deutschland\nJahr: {year}"
                rows.append((SOURCE, COUNTRY, LEVEL, eid, title, url, date_str, STATUS, raw_text))
                print(".", end="", flush=True)

        print(f" → {len(rows)} new")
        total_found += len(rows)

        if args.dry_run:
            for _, _, _, eid, title, url, date_str, _, _ in rows[:3]:
                print(f"  SAMPLE: {date_str}  {title[:70]}")
        else:
            inserted = insert(conn, rows)
            total_insert += inserted

    conn.close()

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Total found: {total_found}")
    if not args.dry_run:
        print(f"Inserted: {total_insert}")
    else:
        print("Run without --dry-run to insert.")


if __name__ == "__main__":
    main()
