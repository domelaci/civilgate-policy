#!/usr/bin/env python3
"""
Canada LEGISinfo historical backfill — count-first mode.

Counts bills per parliament session from LEGISinfo JSON API.
Run without --insert to see the table; add --insert to populate the DB.

Sessions covered: 44-1, 43-2, 43-1, 42-1 (~2015–present)
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

SESSIONS = ["44-1", "43-2", "43-1", "42-1"]

_ENACTED  = {"royal assent received", "royal assent"}
_DEFEATED = {"bill defeated", "bill not proceeded with", "outside the order of precedence"}


def _status(bill: dict) -> str | None:
    s = (bill.get("StatusNameEn") or "").lower()
    if s in _ENACTED:
        return "enacted"
    if s in _DEFEATED:
        return None  # skip defeated/procedural bills
    return "proposed"


def fetch_session(session: str) -> list:
    try:
        r = requests.get(
            "https://www.parl.ca/legisinfo/en/bills/json",
            params={"parlSession": session},
            timeout=30,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  Error {session}: {e}", file=sys.stderr)
        return []


def insert_session(conn: sqlite3.Connection, session: str, bills: list) -> int:
    added = 0
    for bill in bills:
        status = _status(bill)
        if status is None:
            continue

        bill_id = str(bill.get("Id") or "").strip()
        if not bill_id:
            continue
        num_code = (bill.get("NumberCode") or bill_id).strip()
        ext_id   = f"legisinfo:{session}:{bill_id}"

        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        title = ((bill.get("ShortTitleEn") or "").strip()
                 or (bill.get("LongTitleEn") or "").strip()
                 or num_code)
        raw_dt = (bill.get("ReceivedRoyalAssentDateTime") or
                  bill.get("LatestBillEventDateTime") or "")
        pub_date = raw_dt[:10] if raw_dt and raw_dt[:4].isdigit() and raw_dt[:4] != "0001" \
                   else date.today().isoformat()

        url = f"https://www.parl.ca/LegisInfo/en/bill/{session}/{num_code}"
        raw = "\n".join(filter(None, [
            f"Title: {title}",
            f"Long title: {(bill.get('LongTitleEn') or '').strip()}" if bill.get("LongTitleEn") else None,
            f"Session: {session}",
            f"Bill: {num_code}",
            f"Status: {bill.get('StatusNameEn', '')}",
        ]))

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date,
                raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("legisinfo", "CA", ext_id, title, url, pub_date,
             date.today().isoformat(), raw, status, "national"),
        )
        added += 1

    conn.commit()
    return added


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insert", action="store_true",
                        help="Insert into DB after counting (default: count only)")
    args = parser.parse_args()

    print(f"\n{'Session':<10}  {'Bills found':>12}  {'Enacted':>8}  {'Active':>8}  {'Skipped':>8}")
    print("-" * 56)

    all_data = {}
    for session in SESSIONS:
        bills    = fetch_session(session)
        enacted  = sum(1 for b in bills if _status(b) == "enacted")
        active   = sum(1 for b in bills if _status(b) == "proposed")
        skipped  = len(bills) - enacted - active
        print(f"{session:<10}  {len(bills):>12}  {enacted:>8}  {active:>8}  {skipped:>8}")
        all_data[session] = bills
        time.sleep(0.5)

    total_usable = sum(
        sum(1 for b in bills if _status(b) is not None)
        for bills in all_data.values()
    )
    print("-" * 56)
    print(f"{'Usable':<10}  {total_usable:>12}")
    print()

    if not args.insert:
        print("Dry run — pass --insert to populate the DB.")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    print("\nInserting...")
    grand = 0
    for session, bills in all_data.items():
        n = insert_session(conn, session, bills)
        print(f"  {session}: {n} new rows inserted")
        grand += n
    conn.close()
    print(f"\nTotal inserted: {grand}")


if __name__ == "__main__":
    main()
