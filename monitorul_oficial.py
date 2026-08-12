#!/usr/bin/env python3
"""
Monitorul Oficial (Romania) scraper for CivilGate.

API discovery:
  POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php
       {today: 'YYYY-MM-DD', rand: 0.xxx}
  → HTML listing Part I/II/III edition links, e.g.:
       /Monitorul-Oficial--PI--666--2026.html   (these actually serve PDFs)

Strategy:
  1. POST the daily index for the last N days
  2. Pick Part I links (laws, decrees, ordinances, emergency ordinances, govt decisions)
  3. Fetch each edition URL → it returns a PDF despite the .html extension
  4. Extract text; split into individual acts on Romanian legal header patterns
  5. Insert one DB row per act; score_pending() in pipeline picks them up

Run standalone:
    venv/bin/python monitorul_oficial.py [--days N] [--max N] [--dry-run]
Integrated via:
    pipeline.py → fetch_monitorul_oficial(conn)

PDF cache: .mo_pdf_cache/  — avoids re-downloading known editions.
"""
import argparse
import io
import logging
import random
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL  = "https://monitoruloficial.ro"
API_URL   = f"{BASE_URL}/ramo_customs/emonitor/get_mo.php"
HEADERS   = {
    "User-Agent": "CivilGate/1.0 (+https://civilgate.org)",
    "Referer":    f"{BASE_URL}/e-monitor/",
    "Origin":     BASE_URL,
}

# Match Part I edition links (PI = Partea I = laws/decrees/ordinances)
# Ignore Bis (supplement) editions — they're usually amending minor acts
_PART_I_RE = re.compile(r"/Monitorul-Oficial--PI--(\d+)(?:Bis)?--(\d{4})\.html", re.IGNORECASE)

# Romanian legal act headers — used to split a full edition PDF into individual acts
_ACT_HEADER_RE = re.compile(
    r"^(LEGE|DECRET|HOTĂRÂRE|ORDONANȚĂ(?:\s+DE\s+URGENȚĂ)?|ORDIN|DECIZIE|REGULAMENT)"
    r"\s+(?:nr\.|Nr\.)\s*[\d/]+",
    re.MULTILINE | re.IGNORECASE,
)

# Map act type → status
_STATUS_MAP = {
    "lege":      "enacted",
    "decret":    "enacted",
    "hotărâre":  "enacted",
    "ordonanță": "enacted",   # covers both regular and OUG
    "ordin":     "enacted",
    "decizie":   "enacted",
    "regulament": "enacted",
    "proiect":   "proposed",
}

MAX_TEXT_CHARS = 7000


# ── HTTP ───────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ── Daily index ────────────────────────────────────────────────────────────

def _fetch_index(session: requests.Session, day: date) -> list[dict]:
    """
    POST the daily index API for `day`.
    Returns list of {url, edition_num, year} for Part I editions.
    """
    try:
        r = session.post(
            API_URL,
            data={"today": day.isoformat(), "rand": random.random()},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("MO index POST failed for %s: %s", day, e)
        return []

    if not r.text.strip():
        log.info("MO: no editions published on %s", day)
        return []

    soup    = BeautifulSoup(r.text, "lxml")
    results = []
    seen    = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m    = _PART_I_RE.search(href)
        if not m:
            continue
        num  = m.group(1)
        year = m.group(2)
        url  = urljoin(BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        results.append({"url": url, "edition_num": num, "year": year})

    log.info("MO index %s: %d Part I edition(s) found", day, len(results))
    return results


# ── PDF download + text extraction ────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes; returns '' on scanned/failure."""
    try:
        from pdfminer.high_level import extract_text as _pdf_text
        text = _pdf_text(io.BytesIO(pdf_bytes)) or ""
        return text.strip()
    except Exception as e:
        log.debug("pdfminer extraction error: %s", e)
        return ""


def _download_edition(session: requests.Session, url: str, cache_dir: Path) -> str:
    """
    Download an edition URL (actually a PDF despite the .html extension).
    Returns extracted text, using disk cache to avoid re-downloading.
    """
    cache_key  = re.sub(r"[^\w.-]", "_", url.split("monitoruloficial.ro/")[-1])
    cache_path = cache_dir / (cache_key + ".txt")

    if cache_path.exists():
        log.debug("MO cache hit: %s", cache_path.name)
        return cache_path.read_text(encoding="utf-8")

    try:
        r = session.get(url, timeout=60)
        r.raise_for_status()
    except Exception as e:
        log.warning("MO edition download failed %s: %s", url, e)
        return ""

    text = _extract_pdf_text(r.content)
    if not text:
        log.warning("MO: no text extracted from %s (possibly scanned PDF)", url)
        return ""

    cache_path.write_text(text, encoding="utf-8")
    log.info("MO: extracted %d chars from %s", len(text), url.rsplit("/", 1)[-1])
    return text


# ── Act splitting ──────────────────────────────────────────────────────────

def _split_acts(full_text: str) -> list[dict]:
    """
    Split a full Part I edition text into individual acts.
    Falls back to a single entry for the whole edition if no headers match.
    Returns list of {title, text, status}.
    """
    splits = list(_ACT_HEADER_RE.finditer(full_text))

    if len(splits) < 2:
        # Can't split — treat the whole PDF as one entry
        first_line = full_text.strip().split("\n")[0][:200].strip()
        return [{"title": first_line or "Monitorul Oficial Part I", "text": full_text[:MAX_TEXT_CHARS], "status": "enacted"}]

    acts = []
    for i, m in enumerate(splits):
        start = m.start()
        end   = splits[i + 1].start() if i + 1 < len(splits) else len(full_text)
        chunk = full_text[start:end].strip()

        # First line of chunk is the act title
        title_raw = chunk.split("\n")[0].strip()
        # Clean up hyphenation artifacts from PDF extraction
        title = re.sub(r"-\s+", "", title_raw)[:300]
        text  = chunk[:MAX_TEXT_CHARS]

        t = title.lower()
        status = next(
            (v for k, v in _STATUS_MAP.items() if k in t),
            "enacted",
        )

        acts.append({"title": title, "text": text, "status": status})

    return acts


# ── Main fetcher (called by pipeline.py) ──────────────────────────────────

def fetch_monitorul_oficial(
    conn: sqlite3.Connection,
    max_new: int = 20,
    lookback_days: int = 3,
    dry_run: bool = False,
) -> int:
    """
    Fetch Part I acts from the last `lookback_days` of Monitorul Oficial.
    Inserts unscored rows into the policies table for the scoring pipeline.
    Returns number of new rows added.
    """
    base_dir  = Path(__file__).parent
    cache_dir = base_dir / ".mo_pdf_cache"
    cache_dir.mkdir(exist_ok=True)

    session = _session()
    added   = 0
    today   = date.today()

    for offset in range(lookback_days):
        if added >= max_new:
            break

        day      = today - timedelta(days=offset)
        editions = _fetch_index(session, day)

        for ed in editions:
            if added >= max_new:
                break

            # Skip if we already have any act from this edition
            edition_prefix = f"ro_mo:PI:{ed['edition_num']}:{ed['year']}:"
            if conn.execute(
                "SELECT 1 FROM policies WHERE external_id LIKE ?",
                (edition_prefix + "%",),
            ).fetchone():
                log.debug("MO: edition %s/%s already in DB", ed["edition_num"], ed["year"])
                continue

            full_text = _download_edition(session, ed["url"], cache_dir)
            if not full_text:
                continue

            acts = _split_acts(full_text)
            log.info("MO edition PI/%s/%s: %d act(s)", ed["edition_num"], ed["year"], len(acts))

            for i, act in enumerate(acts):
                if added >= max_new:
                    break

                ext_id = f"ro_mo:PI:{ed['edition_num']}:{ed['year']}:{i}"
                if conn.execute(
                    "SELECT 1 FROM policies WHERE external_id=?", (ext_id,)
                ).fetchone():
                    continue

                raw = f"Language: Romanian\nSource: Monitorul Oficial al României, Partea I, nr. {ed['edition_num']}/{ed['year']}\nTitle: {act['title']}\n\n{act['text']}"

                if not dry_run:
                    conn.execute(
                        """INSERT OR IGNORE INTO policies
                           (source, country, external_id, title, url,
                            published_date, fetched_date, raw_text, status, level)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "ro_monitorul_oficial", "RO", ext_id,
                            act["title"], ed["url"],
                            day.isoformat(), today.isoformat(),
                            raw, act["status"], "national",
                        ),
                    )
                    conn.commit()

                added += 1
                log.info("MO: %s act %d — %s", "DRY" if dry_run else "added", i, act["title"][:60])

            time.sleep(2)  # polite crawl delay between editions

    log.info("Monitorul Oficial: %d new act(s)%s", added, " (dry-run)" if dry_run else "")
    return added


# ── Standalone entry point ─────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Monitorul Oficial scraper")
    ap.add_argument("--days",    type=int, default=3,  help="Look back N days (default 3)")
    ap.add_argument("--max",     type=int, default=20, help="Max new acts per run (default 20)")
    ap.add_argument("--dry-run", action="store_true",  help="Parse and log without writing to DB")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    from pathlib import Path
    from dotenv import load_dotenv
    base_dir = Path(__file__).parent
    load_dotenv(base_dir / ".env")

    conn = sqlite3.connect(base_dir / "policies.db")
    from pipeline import init_db
    init_db(conn)

    added = fetch_monitorul_oficial(conn, max_new=args.max, lookback_days=args.days, dry_run=args.dry_run)
    print(f"\nDone — {added} act(s) added.")
    conn.close()


if __name__ == "__main__":
    main()
