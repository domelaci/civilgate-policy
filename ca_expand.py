#!/usr/bin/env python3
"""
One-shot CA data fix + expansion:
1. Re-fetches sessions 42-1, 43-1, 43-2, 44-1 and corrects wrong-dated rows
   (proposed bills that got date.today() fallback) using first-reading dates.
2. Backfills sessions 41-2, 41-1, 40-3, 40-2 as new rows.
3. Skips any bill with no usable date (no phantom dates anymore).
"""
import json, sqlite3, sys, time
from datetime import date
from pathlib import Path
import requests

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"
UA       = "CivilGate/1.0 (+https://civilgate.org)"

EXISTING_SESSIONS = ["44-1", "43-2", "43-1", "42-1"]
NEW_SESSIONS      = ["41-2", "41-1", "40-3", "40-2"]
ALL_SESSIONS      = EXISTING_SESSIONS + NEW_SESSIONS

_ENACTED  = {"royal assent received", "royal assent"}
_SKIP     = {"bill defeated", "bill not proceeded with",
             "outside the order of precedence", "introduced as pro forma bill"}


def _best_date(bill: dict) -> str | None:
    """Return the earliest valid date for a bill, or None."""
    candidates = [
        bill.get("ReceivedRoyalAssentDateTime"),
        bill.get("PassedHouseFirstReadingDateTime"),
        bill.get("PassedSenateFirstReadingDateTime"),
        bill.get("LatestCompletedBillStageDateTime"),
        bill.get("LatestBillEventDateTime"),
    ]
    for dt in candidates:
        if dt and isinstance(dt, str) and dt[:4].isdigit() and dt[:4] != "0001":
            return dt[:10]
    return None


def fetch_session(session: str) -> list:
    try:
        r = requests.get(
            "https://www.parl.ca/legisinfo/en/bills/json",
            params={"parlSession": session}, timeout=40,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  Error fetching {session}: {e}", file=sys.stderr)
        return []


def process_session(conn: sqlite3.Connection, session: str, bills: list) -> dict:
    updated = fixed = added = skipped_no_date = 0
    today = date.today().isoformat()

    for bill in bills:
        status_raw = (bill.get("StatusNameEn") or "").lower()
        if status_raw in _SKIP:
            skipped_no_date += 1
            continue
        status = "enacted" if status_raw in _ENACTED else "proposed"

        bill_id = str(bill.get("Id") or "").strip()
        if not bill_id:
            continue
        num_code = (bill.get("NumberCode") or bill_id).strip()
        ext_id   = f"legisinfo:{session}:{bill_id}"

        pub_date = _best_date(bill)
        if pub_date is None:
            skipped_no_date += 1
            continue

        title  = ((bill.get("ShortTitleEn") or "").strip()
                  or (bill.get("LongTitleEn") or "").strip()
                  or num_code)
        long_t = (bill.get("LongTitleEn") or "").strip()
        url    = f"https://www.parl.ca/LegisInfo/en/bill/{session}/{num_code}"
        raw    = "\n".join(filter(None, [
            f"Title: {title}",
            f"Long title: {long_t}" if long_t else None,
            f"Session: {session}",
            f"Bill: {num_code}",
            f"Status: {bill.get('StatusNameEn', '')}",
        ]))

        existing = conn.execute(
            "SELECT id, published_date, fetched_date FROM policies WHERE external_id=?",
            (ext_id,)
        ).fetchone()

        if existing:
            row_id, row_pub, row_fetched = existing
            # Fix if published_date == fetched_date (fallback date)
            if row_pub == row_fetched and row_pub != pub_date:
                conn.execute(
                    "UPDATE policies SET published_date=?, status=? WHERE id=?",
                    (pub_date, status, row_id)
                )
                fixed += 1
            else:
                updated += 1
        else:
            conn.execute(
                """INSERT OR IGNORE INTO policies
                   (source, country, external_id, title, url, published_date, fetched_date,
                    raw_text, status, level)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("legisinfo", "CA", ext_id, title, url, pub_date,
                 today, raw, status, "national"),
            )
            added += 1

    conn.commit()
    return dict(fixed=fixed, added=added, skipped=skipped_no_date, unchanged=updated)


def main():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    print(f"\n{'Session':<10}  {'Bills':>6}  {'Fixed':>6}  {'Added':>6}  {'Skip':>6}")
    print("-" * 48)

    for session in ALL_SESSIONS:
        bills = fetch_session(session)
        if not bills:
            print(f"{session:<10}  {'N/A':>6}")
            time.sleep(0.5)
            continue
        r = process_session(conn, session, bills)
        print(f"{session:<10}  {len(bills):>6}  {r['fixed']:>6}  {r['added']:>6}  {r['skipped']:>6}")
        time.sleep(0.6)

    # Re-export policies.json
    print("\nRe-exporting policies.json...")
    cols = [
        "source", "country", "external_id", "title", "url", "published_date",
        "summary", "social_score", "social_reason",
        "environmental_score", "environmental_reason",
        "economic_score", "economic_reason",
        "human_rights_score", "human_rights_reason",
        "governance_score", "governance_reason",
        "tags", "status", "level", "scope", "scope_reason", "is_live",
    ]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM policies WHERE summary IS NOT NULL "
        "ORDER BY published_date DESC LIMIT 5000"
    ).fetchall()
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)
    total_count = conn.execute("SELECT count(*) FROM policies").fetchone()[0]
    from datetime import datetime as _dt
    data = {"last_updated": _dt.utcnow().isoformat() + "Z", "total_count": total_count, "policies": out}
    (BASE_DIR / "policies.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(out)} policies (total in DB: {total_count})")

    # Quick check
    ca = conn.execute("SELECT count(*), min(published_date), max(published_date) FROM policies WHERE country='CA'").fetchone()
    print(f"\nCA rows after fix: {ca[0]}, range {ca[1]} – {ca[2]}")
    conn.close()


if __name__ == "__main__":
    main()
