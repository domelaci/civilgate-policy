"""French Legislation (JORF) backfill via PISTE / Légifrance API.

Requires a free API key from: https://piste.gouv.fr/
  1. Create account at https://piste.gouv.fr/
  2. Subscribe to "Légifrance Beta" API
  3. Copy client_id and client_secret to .env:
       PISTE_CLIENT_ID=your_client_id
       PISTE_CLIENT_SECRET=your_client_secret

Usage:
  python fr_backfill.py --dry-run            # count without inserting
  python fr_backfill.py --since 2020         # from 2020 onwards
  python fr_backfill.py --nature LOI,ORDONNANCE  # law types (default: LOI)
  python fr_backfill.py                      # full backfill from 2017

Law natures available: LOI, ORDONNANCE, DECRET, ARRETE
For a policy tracker, LOI (Acts of Parliament) is the most relevant.
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH  = Path(__file__).parent / "policies.db"
COUNTRY  = "FR"
LEVEL    = "eu"      # France is EU member — shows in EU feed
SOURCE   = "assemblee_nationale"
STATUS   = "law"

OAUTH_URL  = "https://oauth.piste.gouv.fr/api/oauth/token"
API_BASE   = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
JO_SEARCH  = f"{API_BASE}/jorf/search"
JO_ARTICLE = f"{API_BASE}/jorf/id"

YEAR_NOW   = datetime.now().year

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token():
    client_id     = os.getenv("PISTE_CLIENT_ID", "")
    client_secret = os.getenv("PISTE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print(
            "\nERROR: PISTE credentials missing.\n"
            "  1. Register at https://piste.gouv.fr/\n"
            "  2. Subscribe to 'Légifrance Beta' API\n"
            "  3. Add to .env:\n"
            "       PISTE_CLIENT_ID=...\n"
            "       PISTE_CLIENT_SECRET=...\n",
            file=sys.stderr,
        )
        sys.exit(1)

    r = requests.post(
        OAUTH_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
            "scope":         "openid",
        },
        timeout=15,
    )
    if not r.ok:
        print(f"Auth failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
        sys.exit(1)
    return r.json()["access_token"]


# ── API helpers ────────────────────────────────────────────────────────────────

def jorf_search(token, nature, date_start, date_end, page=1, page_size=20):
    """Search JORF for legislation of the given nature and date range."""
    payload = {
        "recherche": {
            "nature":          nature,
            "dateSignatureStart": date_start,
            "dateSignatureEnd":   date_end,
            "pageNumber":      page,
            "pageSize":        page_size,
            "sort":            "SIGNATURE_DATE_DESC",
        }
    }
    r = requests.post(
        JO_SEARCH,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=20,
    )
    if not r.ok:
        print(f"  WARN search: {r.status_code}", file=sys.stderr)
        return None
    return r.json()


# ── DB helpers ────────────────────────────────────────────────────────────────

def existing_ids(conn):
    cur = conn.execute("SELECT external_id FROM policies WHERE source=?", (SOURCE,))
    return {row[0] for row in cur.fetchall()}


def insert(conn, rows):
    conn.executemany(
        """INSERT OR IGNORE INTO policies
           (source, country, level, external_id, title, url,
            published_date, fetched_date, status)
           VALUES (?,?,?,?,?,?,?,date('now'),?)""",
        rows,
    )
    conn.commit()
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--since",    type=int, default=2017)
    ap.add_argument("--until",    type=int, default=YEAR_NOW)
    ap.add_argument("--nature",   default="LOI",
                    help="Comma-separated JORF natures (default: LOI)")
    args = ap.parse_args()

    natures = [n.strip().upper() for n in args.nature.split(",")]
    print(f"Fetching JORF natures: {natures} for {args.since}-{args.until}")

    token = get_token()
    print("PISTE token obtained.")

    conn     = sqlite3.connect(str(DB_PATH))
    seen     = existing_ids(conn)
    print(f"Existing FR entries: {len(seen)}")

    total_found  = 0
    total_insert = 0

    for year in range(args.since, args.until + 1):
        date_start = f"{year}-01-01"
        date_end   = f"{year}-12-31"

        for nature in natures:
            rows = []
            page = 1
            print(f"\n── {year} {nature} ", end="", flush=True)

            while True:
                data = jorf_search(token, nature, date_start, date_end, page=page)
                if not data:
                    break

                items = data.get("results", [])
                if not items:
                    break

                for item in items:
                    eid   = f"fr_jorf_{item.get('id', item.get('cid',''))}"
                    if eid in seen:
                        continue
                    title = item.get("title", "").strip()
                    if not title:
                        continue

                    # Date: prefer signatureDate, fallback to publicationDate
                    raw_date = item.get("signatureDate") or item.get("publicationDate", "")
                    try:
                        pub_date = datetime.fromtimestamp(raw_date / 1000).strftime("%Y-%m-%d") if isinstance(raw_date, (int, float)) else str(raw_date)[:10]
                    except Exception:
                        pub_date = str(raw_date)[:10]

                    url = f"https://www.legifrance.gouv.fr/jorf/id/{item.get('cid', '')}"
                    rows.append((SOURCE, COUNTRY, LEVEL, eid, title, url, pub_date, STATUS))
                    print(".", end="", flush=True)

                total_pages = data.get("pageCount", 1)
                if page >= total_pages:
                    break
                page += 1
                time.sleep(0.3)

            print(f" → {len(rows)} new")
            total_found += len(rows)

            if args.dry_run:
                for _, _, _, eid, title, url, pub_date, _ in rows[:3]:
                    print(f"  SAMPLE: {pub_date}  {title[:70]}")
            else:
                inserted = insert(conn, rows)
                total_insert += inserted

            # Re-auth every 20 years-of-data (token expires after 1h)
            if (year - args.since) % 20 == 19:
                token = get_token()

    conn.close()
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Total found: {total_found}")
    if not args.dry_run:
        print(f"Inserted: {total_insert}")
    else:
        print("Run without --dry-run to insert.")


if __name__ == "__main__":
    main()
