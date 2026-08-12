#!/usr/bin/env python3
"""
One-shot Federal Register historical backfill — fetches rules back to 2020.
Run once overnight: python3 backfill.py
Safe to re-run (INSERT OR IGNORE skips existing records).
"""
import json
import logging
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

FIELDS = [
    "document_number", "title", "abstract",
    "html_url", "publication_date", "type", "agencies", "action",
]


def fetch_page(params: dict) -> dict:
    r = requests.get(
        "https://www.federalregister.gov/api/v1/documents.json",
        params=params, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def backfill(conn: sqlite3.Connection, start_year: int = 2020, end_year: int = 2024) -> None:
    """Fetch one month at a time to stay within API pagination limits."""
    current = date(start_year, 1, 1)
    end     = date(end_year, 12, 31)

    total_added = 0

    while current <= end:
        month_end = (current.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if month_end > end:
            month_end = end

        log.info("Fetching %s to %s …", current, month_end)

        params = {
            "per_page": 100,
            "order": "newest",
            "fields[]": FIELDS,
            "conditions[type][]": ["RULE", "PRESDOCU"],
            "conditions[publication_date][gte]": current.isoformat(),
            "conditions[publication_date][lte]": month_end.isoformat(),
        }

        page = 1
        month_added = 0

        while True:
            params["page"] = page
            try:
                data = fetch_page(params)
            except Exception as e:
                log.error("Fetch failed page %d: %s", page, e)
                break

            results = data.get("results", [])
            if not results:
                break

            for doc in results:
                ext_id = f"fr:{doc.get('document_number', '')}"
                if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
                    continue

                agencies = ", ".join(a.get("name", "") for a in doc.get("agencies", []))
                raw = "\n".join(filter(None, [
                    f"Title: {doc.get('title', '')}",
                    f"Type: {doc.get('type', '')}",
                    f"Agencies: {agencies}",
                    f"Action: {doc.get('action', '')}",
                    f"Summary: {doc.get('abstract', '')}",
                ]))

                conn.execute(
                    """INSERT OR IGNORE INTO policies
                       (source, country, external_id, title, url, published_date, fetched_date, raw_text)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("federal_register", "US", ext_id,
                     doc.get("title"), doc.get("html_url"),
                     doc.get("publication_date"), date.today().isoformat(), raw),
                )
                month_added += 1

            conn.commit()
            log.info("  Page %d: +%d (total this month: %d)", page, len(results), month_added)

            total_pages = data.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.5)  # gentle rate limiting

        total_added += month_added
        log.info("Month %s complete — %d new records (running total: %d)", current.strftime("%Y-%m"), month_added, total_added)

        current = month_end + timedelta(days=1)
        time.sleep(1)

    log.info("Backfill complete. Total new records: %d", total_added)


if __name__ == "__main__":
    conn = sqlite3.connect(DB_FILE)
    backfill(conn, start_year=2020, end_year=2024)
    conn.close()
