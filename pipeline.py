#!/usr/bin/env python3
"""
CivilGate policy pipeline.
Fetches from Federal Register API, scores with Gemini, exports to policies.json.
Cron: 0 7 * * * /home/domelaci/projects/automate/civilgate/venv/bin/python /home/domelaci/projects/automate/civilgate/pipeline.py >> /home/domelaci/projects/automate/civilgate/pipeline.log 2>&1
"""
import base64
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"
OUT_FILE = BASE_DIR / "policies.json"

load_dotenv(BASE_DIR / ".env")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "")
SITE_URL       = os.environ.get("SITE_URL", "https://civilgate.org")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── Database ───────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS policies (
        id                   INTEGER PRIMARY KEY,
        source               TEXT NOT NULL,
        country              TEXT NOT NULL,
        external_id          TEXT UNIQUE NOT NULL,
        title                TEXT,
        url                  TEXT,
        published_date       DATE,
        fetched_date         DATE,
        raw_text             TEXT,
        summary              TEXT,
        social_score         INTEGER,
        social_reason        TEXT,
        environmental_score  INTEGER,
        environmental_reason TEXT,
        economic_score       INTEGER,
        economic_reason      TEXT,
        tags                 TEXT,
        scored_at            TIMESTAMP,
        score_failed         INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS users (
        id         TEXT PRIMARY KEY,
        email      TEXT UNIQUE,
        tier       TEXT DEFAULT 'free',
        created_at TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id         INTEGER PRIMARY KEY,
        user_id    TEXT,
        keywords   TEXT,
        countries  TEXT,
        active     BOOLEAN DEFAULT TRUE
    );
    """)
    conn.commit()


# ── Federal Register fetcher ───────────────────────────────────────────────

def fetch_federal_register(conn: sqlite3.Connection, max_new: int = 20) -> int:
    params = {
        "per_page": 40,
        "order": "newest",
        "fields[]": [
            "document_number", "title", "abstract",
            "html_url", "publication_date", "type", "agencies", "action",
        ],
        "conditions[type][]": ["RULE", "PRESDOCU"],
    }
    try:
        r = requests.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params=params, timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        log.error("Federal Register fetch failed: %s", e)
        return 0

    added = 0
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
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("Federal Register: %d new documents", added)
    return added


# ── EC Press Corner fetcher ────────────────────────────────────────────────

def fetch_ec_press(conn: sqlite3.Connection, max_new: int = 10) -> int:
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(
            "https://ec.europa.eu/commission/presscorner/api/rss",
            timeout=30,
            headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)"},
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.error("EC Press Corner fetch failed: %s", e)
        return 0

    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    added = 0
    for item in root.iter("item"):
        link  = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()

        ext_id = f"ec:{link}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        # Parse date — RSS pubDate format: "Wed, 12 Aug 2026 12:00:00 +0000"
        try:
            from email.utils import parsedate_to_datetime
            pub_date = parsedate_to_datetime(pub).date().isoformat()
        except Exception:
            pub_date = date.today().isoformat()

        raw = f"Title: {title}\nSummary: {desc}"

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("ec_press", "EU", ext_id, title, link, pub_date,
             date.today().isoformat(), raw),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("EC Press Corner: %d new documents", added)
    return added


# ── Gemini scorer ──────────────────────────────────────────────────────────

SCORE_PROMPT = """\
You are a policy analyst. Given the following government policy document, \
return a JSON object with exactly these keys:
- "summary": 2-3 sentence plain English summary for someone with no political background
- "social_score": integer 1-10 (magnitude of social impact — high = large impact, not good or bad)
- "social_reason": one sentence explaining the social score
- "environmental_score": integer 1-10
- "environmental_reason": one sentence
- "economic_score": integer 1-10
- "economic_reason": one sentence
- "tags": array of up to 5 lowercase topic keywords

Return ONLY valid JSON. No markdown, no code fences.

Document:
{text}"""


def call_gemini(text: str) -> dict:
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-flash-lite-latest:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": SCORE_PROMPT.format(text=text[:6000])}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024},
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def score_pending(conn: sqlite3.Connection, limit: int = 15) -> int:
    rows = conn.execute(
        """SELECT id, raw_text FROM policies
           WHERE summary IS NULL AND score_failed = 0
           ORDER BY published_date DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    scored = 0
    for row_id, raw_text in rows:
        try:
            result = call_gemini(raw_text or "")
            conn.execute(
                """UPDATE policies SET
                   summary=?, social_score=?, social_reason=?,
                   environmental_score=?, environmental_reason=?,
                   economic_score=?, economic_reason=?,
                   tags=?, scored_at=?
                   WHERE id=?""",
                (
                    result.get("summary"),
                    result.get("social_score"), result.get("social_reason"),
                    result.get("environmental_score"), result.get("environmental_reason"),
                    result.get("economic_score"), result.get("economic_reason"),
                    json.dumps(result.get("tags", []), ensure_ascii=False),
                    datetime.utcnow().isoformat(),
                    row_id,
                ),
            )
            conn.commit()
            scored += 1
            log.info("Scored policy id=%d", row_id)
            time.sleep(1)
        except Exception as e:
            log.error("Scoring failed for id=%d: %s", row_id, e)
            conn.execute("UPDATE policies SET score_failed=1 WHERE id=?", (row_id,))
            conn.commit()

    log.info("Scored %d policies this run", scored)
    return scored


# ── Export ─────────────────────────────────────────────────────────────────

def export_json(conn: sqlite3.Connection) -> None:
    cols = [
        "source", "country", "external_id", "title", "url", "published_date",
        "summary", "social_score", "social_reason",
        "environmental_score", "environmental_reason",
        "economic_score", "economic_reason", "tags",
    ]
    rows = conn.execute(
        f"""SELECT {','.join(cols)} FROM policies
            WHERE summary IS NOT NULL
            ORDER BY published_date DESC LIMIT 200""",
    ).fetchall()

    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)

    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Exported %d policies to policies.json", len(out))


# ── GitHub push ────────────────────────────────────────────────────────────

def push_to_github(files: dict) -> None:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.info("No GITHUB_TOKEN/REPO set — skipping push")
        return
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for path, content_bytes in files.items():
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        sha = None
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            sha = r.json()["sha"]
        payload = {
            "message": f"chore: policy update {date.today().isoformat()}",
            "content": base64.b64encode(content_bytes).decode(),
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha
        r = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            log.info("GitHub push OK: %s", path)
        else:
            log.error("GitHub push FAILED %s: %s %s", path, r.status_code, r.text[:200])


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    if not GEMINI_API_KEY:
        raise SystemExit("Missing GEMINI_API_KEY in .env")

    conn = sqlite3.connect(DB_FILE)
    init_db(conn)

    fetch_federal_register(conn)
    fetch_ec_press(conn)
    score_pending(conn)
    export_json(conn)

    conn.close()

    push_to_github({"policies.json": OUT_FILE.read_bytes()})
    log.info("Pipeline complete.")


if __name__ == "__main__":
    main()
