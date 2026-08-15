"""French legislation backfill from Assemblée Nationale open data.

No authentication required. Downloads bulk JSON exports directly.

Coverage:
  Legislature 16 (2022–2024) — ~1 800 law dossiers
  Legislature 17 (2024–now)  — ~2 100 law dossiers
  Legislature 15 (2017–2022) — NOT available via this method;
                               swap to fr_backfill_piste.py once PISTE access is approved.

Usage:
  python fr_backfill.py --dry-run
  python fr_backfill.py --since 2022
  python fr_backfill.py
"""
import argparse
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import requests

DB_PATH  = Path(__file__).parent / "policies.db"
COUNTRY  = "FR"
LEVEL    = "eu"
SOURCE   = "assemblee_nationale"
HEADERS  = {"User-Agent": "CivilGate-backfill/1.0 (civic research; non-commercial)"}
YEAR_NOW = datetime.now().year

AN_BASE = "https://data.assemblee-nationale.fr/static/openData/repository"
LEGS = [16, 17]

LAW_PROCS = {
    "Proposition de loi ordinaire",
    "Projet de loi ordinaire",
    "Projet ou proposition de loi organique",
    "Projet ou proposition de loi constitutionnelle",
    "Projet de ratification des traités et conventions",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def find_prom_date(obj):
    """Return the date of the PROM (promulgation/JORF publication) step."""
    if isinstance(obj, dict):
        if "PROM" in (obj.get("codeActe") or "") and obj.get("dateActe"):
            return obj["dateActe"][:10]
        for v in obj.values():
            d = find_prom_date(v)
            if d:
                return d
    elif isinstance(obj, list):
        for item in obj:
            d = find_prom_date(item)
            if d:
                return d
    return None


def find_first_date(obj):
    """Return the earliest dateActe in the legislative acts tree."""
    if isinstance(obj, dict):
        dates = [obj["dateActe"][:10]] if obj.get("dateActe") else []
        for v in obj.values():
            d = find_first_date(v)
            if d:
                dates.append(d)
        return min(dates) if dates else None
    elif isinstance(obj, list):
        dates = [find_first_date(i) for i in obj if i]
        dates = [d for d in dates if d]
        return min(dates) if dates else None
    return None


# ── Download & parse ─────────────────────────────────────────────────────────

def download_zip(leg):
    url = f"{AN_BASE}/{leg}/loi/dossiers_legislatifs/Dossiers_Legislatifs.json.zip"
    print(f"  Downloading legislature {leg} … ", end="", flush=True)
    try:
        r = requests.get(url, headers=HEADERS, timeout=120)
        r.raise_for_status()
    except Exception as e:
        print(f"FAILED: {e}")
        return None
    print(f"OK ({len(r.content) // 1024} KB)")
    return io.BytesIO(r.content)


def parse_zip(buf, since_year, seen):
    rows = []
    with zipfile.ZipFile(buf) as z:
        for name in z.namelist():
            dp = json.loads(z.read(name)).get("dossierParlementaire", {})
            proc = (dp.get("procedureParlementaire") or {}).get("libelle", "")
            if proc not in LAW_PROCS:
                continue

            uid = dp.get("uid", "")
            eid = f"fr_an_{uid}"
            if eid in seen:
                continue

            titre_obj  = dp.get("titreDossier") or {}
            title      = (titre_obj.get("titre") or "").strip()
            slug       = titre_obj.get("titreChemin") or uid
            legislature = dp.get("legislature", "")

            if not title:
                continue

            acts      = dp.get("actesLegislatifs")
            prom_date  = find_prom_date(acts)
            first_date = find_first_date(acts)
            pub_date   = prom_date or first_date or ""

            if since_year and pub_date and int(pub_date[:4]) < since_year:
                continue

            status = "enacted" if prom_date else "proposed"
            url    = f"https://www.assemblee-nationale.fr/dyn/{legislature}/dossiers/{slug}"

            rows.append((SOURCE, COUNTRY, LEVEL, eid, title, url, pub_date, status))
            seen.add(eid)
    return rows


# ── DB helpers ────────────────────────────────────────────────────────────────

def existing_ids(conn):
    cur = conn.execute("SELECT external_id FROM policies WHERE source=?", (SOURCE,))
    return {r[0] for r in cur.fetchall()}


def insert(conn, rows):
    conn.executemany(
        """INSERT OR IGNORE INTO policies
           (source, country, level, external_id, title, url,
            published_date, fetched_date, status)
           VALUES (?,?,?,?,?,?,?,date('now'),?)""",
        rows,
    )
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since",   type=int, default=0, help="Skip dossiers before this year")
    args = ap.parse_args()

    conn  = sqlite3.connect(str(DB_PATH))
    seen  = existing_ids(conn)
    print(f"Existing FR entries: {len(seen)}")

    total_found = total_insert = 0

    for leg in LEGS:
        print(f"\n=== Legislature {leg} ===")
        buf = download_zip(leg)
        if not buf:
            continue

        rows    = parse_zip(buf, args.since, seen)
        enacted = sum(1 for r in rows if r[-1] == "enacted")
        print(f"  {len(rows)} new  ({enacted} enacted, {len(rows) - enacted} proposed)")
        total_found += len(rows)

        if args.dry_run:
            for r in rows[:3]:
                print(f"  SAMPLE [{r[-1]}] {r[6]}  {r[4][:70]}")
        else:
            insert(conn, rows)
            total_insert += len(rows)

    conn.close()
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Total: {total_found}")
    if not args.dry_run:
        print(f"Inserted: {total_insert}")
    print("\nNOTE: Legislature 15 (2017-2022) gap — swap to fr_backfill_piste.py once PISTE approved.")


if __name__ == "__main__":
    main()
