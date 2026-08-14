#!/usr/bin/env python3
"""
Australia Federal Register of Legislation historical backfill — count-first mode.

Uses the same OData API as pipeline.py (legislation.gov.au).
Acts only — Legislative Instruments (~1700/year) are excluded.

Run without --insert to see the count table; add --insert to populate the DB.
Use --since YYYY to limit how far back to fetch (default 2020).
"""
import argparse
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"
UA       = "CivilGate/1.0 (+https://civilgate.org)"
API      = "https://api.prod.legislation.gov.au/v1/Titles"
PAGE     = 100


def fetch_since(since_year: int) -> list[dict]:
    acts = []
    skip = 0
    since_str = f"{since_year}-01-01"

    while True:
        try:
            r = requests.get(API,
                params={
                    "$top":     PAGE,
                    "$skip":    skip,
                    "$orderby": "makingDate desc",
                    "$select":  "id,name,makingDate,collection,publishComments",
                    "$filter":  "collection eq 'Act' or collection eq 'LegislativeInstrument'",
                },
                timeout=30,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            r.raise_for_status()
            items = r.json().get("value", [])
        except Exception as e:
            print(f"  Error at skip={skip}: {e}", file=sys.stderr)
            break

        if not items:
            break

        for it in items:
            if it.get("collection") != "Act":
                continue
            md = (it.get("makingDate") or "")[:10]
            if md and md < since_str:
                # Passed the cutoff — return what we have
                return acts
            acts.append(it)

        oldest = (items[-1].get("makingDate") or "")[:10]
        print(f"  page skip={skip:5d}  acts so far={len(acts):4d}  oldest={oldest[:7]}",
              file=sys.stderr)

        if oldest[:4] < str(since_year):
            break
        if len(items) < PAGE:
            break
        skip += PAGE
        time.sleep(0.2)

    return acts


def insert_acts(conn: sqlite3.Connection, acts: list) -> int:
    added = 0
    for it in acts:
        leg_id = (it.get("id") or "").strip()
        if not leg_id:
            continue
        ext_id = f"au_leg:{leg_id}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        title    = (it.get("name") or "").strip() or leg_id
        pub_date = (it.get("makingDate") or "")[:10] or date.today().isoformat()
        comments = (it.get("publishComments") or "").strip()
        url      = f"https://www.legislation.gov.au/Details/{leg_id}"
        raw      = "\n".join(filter(None, [
            f"Title: {title}",
            f"Type: Act of Parliament",
            f"Made: {pub_date}",
            f"Notes: {comments}" if comments else None,
        ]))

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date,
                raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("au_legislation", "AU", ext_id, title, url, pub_date,
             date.today().isoformat(), raw, "enacted", "national"),
        )
        added += 1

    conn.commit()
    return added


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=int, default=2020, metavar="YYYY",
                        help="Fetch Acts made on or after this year (default: 2020)")
    parser.add_argument("--insert", action="store_true",
                        help="Insert into DB after counting (default: count only)")
    args = parser.parse_args()

    print(f"\nFetching Australian Acts since {args.since}…", file=sys.stderr)
    acts = fetch_since(args.since)

    # Count by year
    yearly: dict[str, int] = {}
    for it in acts:
        yr = (it.get("makingDate") or "")[:4]
        yearly[yr] = yearly.get(yr, 0) + 1

    print(f"\n{'Year':<6}  {'Acts found':>10}")
    print("-" * 20)
    for yr in sorted(yearly.keys(), reverse=True):
        print(f"{yr:<6}  {yearly[yr]:>10}")
    print("-" * 20)
    print(f"{'Total':<6}  {len(acts):>10}")
    print()

    if not args.insert:
        print("Dry run — pass --insert to populate the DB.")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    n = insert_acts(conn, acts)
    conn.close()
    print(f"Inserted: {n} new rows")


if __name__ == "__main__":
    main()
