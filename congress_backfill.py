#!/usr/bin/env python3
"""
Congress.gov historical backfill — Public Laws from the 110th–119th Congress.
Uses /v3/law/{congress} endpoint (enacted bills only).
Run once; safe to re-run (INSERT OR IGNORE).
"""
import os
import sqlite3
import time
import logging
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"

load_dotenv(BASE_DIR / ".env")
CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONGRESSES = {
    110: "2007-2009", 111: "2009-2011", 112: "2011-2013",
    113: "2013-2015", 114: "2015-2017", 115: "2017-2019",
    116: "2019-2021", 117: "2021-2023", 118: "2023-2025",
    119: "2025-2027",
}

# Map bill type codes to congress.gov URL path segments
_TYPE_PATH = {
    "HR": "house-bill", "S": "senate-bill",
    "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
    "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution",
    "HRES": "house-resolution", "SRES": "senate-resolution",
}


def public_url(congress: int, bill_type: str, number: str) -> str:
    path = _TYPE_PATH.get(bill_type.upper(), f"{bill_type.lower()}-bill")
    return f"https://www.congress.gov/bill/{congress}th-congress/{path}/{number}"


def fetch_congress_laws(conn: sqlite3.Connection, congress: int) -> int:
    offset = 0
    limit  = 250
    added  = 0
    today  = date.today().isoformat()

    while True:
        try:
            r = requests.get(
                f"https://api.congress.gov/v3/law/{congress}",
                params={"limit": limit, "offset": offset, "api_key": CONGRESS_API_KEY},
                timeout=30,
                headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("Congress %d offset %d: %s", congress, offset, e)
            break

        bills = data.get("bills", [])
        total = data.get("pagination", {}).get("count", 0)

        for bill in bills:
            congress_num = bill.get("congress", congress)
            bill_type    = (bill.get("type") or "").strip()
            bill_num     = (bill.get("number") or "").strip()
            title        = (bill.get("title") or "").strip()
            action_text  = (bill.get("latestAction") or {}).get("text", "")
            action_date  = (bill.get("latestAction") or {}).get("actionDate", "")
            law_numbers  = ", ".join(l.get("number", "") for l in (bill.get("laws") or []))
            chamber      = bill.get("originChamber", "")

            ext_id = f"congress:{congress_num}:{bill_type}:{bill_num}"

            if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
                continue

            url = public_url(congress_num, bill_type, bill_num)
            raw = "\n".join(filter(None, [
                f"Title: {title}",
                f"Type: {bill_type} {bill_num} ({congress_num}th Congress)",
                f"Chamber: {chamber}",
                f"Public Law: {law_numbers}" if law_numbers else "",
                f"Enacted: {action_date} — {action_text}",
            ]))

            conn.execute(
                """INSERT OR IGNORE INTO policies
                   (source, country, external_id, title, url,
                    published_date, fetched_date, raw_text, status, level)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("congress", "US", ext_id, title, url,
                 action_date, today, raw, "enacted", "national"),
            )
            added += 1

        conn.commit()
        offset += limit

        log.info("Congress %d (%s): fetched offset %d/%d, new so far: %d",
                 congress, CONGRESSES[congress], min(offset, total), total, added)

        if offset >= total:
            break

        time.sleep(0.25)

    return added


def main():
    if not CONGRESS_API_KEY:
        raise SystemExit("Missing CONGRESS_API_KEY in .env")

    conn = sqlite3.connect(DB_FILE)

    total_added = 0
    for congress in sorted(CONGRESSES):
        log.info("=== Congress %d (%s) ===", congress, CONGRESSES[congress])
        n = fetch_congress_laws(conn, congress)
        log.info("Congress %d: %d new laws inserted", congress, n)
        total_added += n
        time.sleep(0.5)

    log.info("Done. Total inserted: %d", total_added)
    conn.close()


if __name__ == "__main__":
    main()
