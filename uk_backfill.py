#!/usr/bin/env python3
"""
UK legislation.gov.uk historical backfill.

Count mode (default): prints a table of Acts per year for ukpga and uksi.
Insert mode (--insert): inserts ukpga Acts into the DB (uksi excluded — ~1300/year).
Year range: 2016–2026. Use --since YYYY to limit how far back to go.
"""
import argparse
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"
ATOM_NS  = "http://www.w3.org/2005/Atom"
YEARS    = range(2016, 2027)
UA       = "CivilGate/1.0 (+https://civilgate.org)"


def _fetch_year_feed(year: int, doc_type: str) -> list:
    entries = []
    page    = 1
    while True:
        url = f"https://www.legislation.gov.uk/{doc_type}/{year}/data.feed?page={page}"
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
            if r.status_code == 404:
                break
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"  Error {doc_type}/{year} page {page}: {e}", file=sys.stderr)
            break
        page_entries = root.findall(f"{{{ATOM_NS}}}entry")
        if not page_entries:
            break
        entries.extend(page_entries)
        next_link = root.find(f"{{{ATOM_NS}}}link[@rel='next']")
        if next_link is None:
            break
        page += 1
        time.sleep(0.3)
    return entries


def count_mode() -> None:
    print(f"\n{'Year':<6}  {'ukpga (Acts)':>14}  {'uksi (SIs)':>12}")
    print("-" * 38)
    totals = [0, 0]
    for year in YEARS:
        ukpga = len(_fetch_year_feed(year, "ukpga"))
        time.sleep(0.3)
        uksi  = len(_fetch_year_feed(year, "uksi"))
        time.sleep(0.3)
        print(f"{year:<6}  {ukpga:>14}  {uksi:>12}")
        totals[0] += ukpga
        totals[1] += uksi
    print("-" * 38)
    print(f"{'Total':<6}  {totals[0]:>14}  {totals[1]:>12}")
    print()
    print("Recommendation: insert ukpga only (manageable volume).")
    print("Run with --insert to populate the DB with ukpga Acts.")


def insert_mode(since_year: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    grand = 0
    for year in YEARS:
        if year < since_year:
            continue
        entries = _fetch_year_feed(year, "ukpga")
        added   = 0
        for entry in entries:
            link_el = (entry.find(f"{{{ATOM_NS}}}link[@rel='alternate']") or
                       entry.find(f"{{{ATOM_NS}}}link"))
            url = (link_el.get("href", "") if link_el is not None else "").strip()
            if not url:
                continue

            ext_id = f"leg_gov_uk:{url}"
            if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
                continue

            title_el   = entry.find(f"{{{ATOM_NS}}}title")
            title      = (title_el.text or "").strip() if title_el is not None else url

            updated_el = entry.find(f"{{{ATOM_NS}}}updated")
            pub_date   = ((updated_el.text or "")[:10]) if updated_el is not None else str(year)

            raw = f"Title: {title}\nType: UK Public General Act\nYear: {year}"

            conn.execute(
                """INSERT OR IGNORE INTO policies
                   (source, country, external_id, title, url, published_date, fetched_date,
                    raw_text, status, level, is_live)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("legislation_gov_uk", "GB", ext_id, title, url, pub_date,
                 date.today().isoformat(), raw, "enacted", "national", 0),
            )
            added += 1
            time.sleep(0.1)

        conn.commit()
        print(f"  {year}: {added} new Acts inserted")
        grand += added
        time.sleep(0.5)

    conn.close()
    print(f"\nTotal inserted: {grand}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insert", action="store_true",
                        help="Insert ukpga Acts into DB (default: count only)")
    parser.add_argument("--since", type=int, default=2016, metavar="YYYY",
                        help="Only insert from this year onward (default: 2016)")
    args = parser.parse_args()

    if args.insert:
        print(f"Inserting ukpga Acts since {args.since}…")
        insert_mode(args.since)
    else:
        count_mode()


if __name__ == "__main__":
    main()
