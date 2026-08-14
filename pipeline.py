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
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")
SITE_URL         = os.environ.get("SITE_URL", "https://civilgate.org")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCORER_VERSION = "v2"


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
        score_failed         INTEGER DEFAULT 0,
        status               TEXT DEFAULT 'unknown',
        scorer_version       TEXT
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
    # Migration: add status column to existing databases
    try:
        conn.execute("ALTER TABLE policies ADD COLUMN status TEXT DEFAULT 'unknown'")
        conn.commit()
        log.info("Migrated: added status column")
    except Exception:
        pass  # already exists
    # Migration: add level column to existing databases
    try:
        conn.execute("ALTER TABLE policies ADD COLUMN level TEXT DEFAULT 'national'")
        conn.commit()
        log.info("Migrated: added level column")
    except Exception:
        pass  # already exists
    # Migration: add scorer_version column
    try:
        conn.execute("ALTER TABLE policies ADD COLUMN scorer_version TEXT")
        conn.commit()
        log.info("Migrated: added scorer_version column")
    except Exception:
        pass
    for _col in ("scope TEXT", "scope_reason TEXT",
                 "human_rights_score INTEGER", "human_rights_reason TEXT",
                 "governance_score INTEGER", "governance_reason TEXT"):
        try:
            conn.execute(f"ALTER TABLE policies ADD COLUMN {_col}")
            conn.commit()
        except Exception:
            pass  # already exists
    # Backfill level for existing records
    conn.executescript("""
    UPDATE policies SET level='eu'
      WHERE level IS NULL OR level='national'
        AND source IN ('eurlex','ec_press');
    UPDATE policies SET level='national'
      WHERE (level IS NULL OR level NOT IN ('eu','national'))
        AND source IN ('federal_register','uk_gov','canada_gazette','au_legislation','uk_parliament');
    """)
    conn.commit()
    # Backfill status for existing records where deterministic
    conn.executescript("""
    UPDATE policies SET status='enacted'
      WHERE status='unknown' AND source IN ('eurlex','au_legislation');
    UPDATE policies SET status='enacted'
      WHERE status='unknown' AND source='canada_gazette'
        AND (external_id LIKE '%/p2/%' OR external_id NOT LIKE '%/p1/%');
    UPDATE policies SET status='decision'
      WHERE source='ec_press'
        AND NOT (LOWER(raw_text) LIKE '%proposal%'
              OR LOWER(raw_text) LIKE '%consultation%'
              OR LOWER(raw_text) LIKE '%draft%'
              OR LOWER(raw_text) LIKE '% open for comment%');
    UPDATE policies SET status='enacted'
      WHERE status='unknown' AND source='federal_register'
        AND (raw_text LIKE '%Type: RULE%' OR raw_text LIKE '%Type: PRESDOCU%'
             OR raw_text LIKE '%Type: Presidential Document%'
             OR raw_text LIKE '%Type: Executive Order%');
    UPDATE policies SET status='proposed'
      WHERE status='unknown' AND source='federal_register'
        AND (raw_text LIKE '%Type: PROPOSED_RULE%' OR raw_text LIKE '%Type: Proposed Rule%');
    UPDATE policies SET status='proposed'
      WHERE status='unknown' AND source='uk_gov'
        AND raw_text LIKE '%policy_paper%';
    """)
    conn.commit()


# ── Federal Register fetcher ───────────────────────────────────────────────

_FR_STATUS = {"RULE": "enacted", "PRESDOCU": "enacted", "PROPOSED_RULE": "proposed"}


def fetch_federal_register(conn: sqlite3.Connection, max_new: int = 20) -> int:
    params = {
        "per_page": 40,
        "order": "newest",
        "fields[]": [
            "document_number", "title", "abstract",
            "html_url", "publication_date", "type", "agencies", "action",
        ],
        "conditions[type][]": ["RULE", "PRESDOCU", "PROPOSED_RULE"],
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

        doc_type = doc.get("type", "")
        agencies = ", ".join(a.get("name", "") for a in doc.get("agencies", []))
        raw = "\n".join(filter(None, [
            f"Title: {doc.get('title', '')}",
            f"Type: {doc_type}",
            f"Agencies: {agencies}",
            f"Action: {doc.get('action', '')}",
            f"Summary: {doc.get('abstract', '')}",
        ]))
        status = _FR_STATUS.get(doc_type, "unknown")

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("federal_register", "US", ext_id,
             doc.get("title"), doc.get("html_url"),
             doc.get("publication_date"), date.today().isoformat(), raw, status, "national"),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("Federal Register: %d new documents", added)
    return added


# ── EUR-Lex CELLAR fetcher ─────────────────────────────────────────────────

EURLEX_SPARQL = """\
SELECT DISTINCT ?work ?date ?title ?celex ?type WHERE {{
  VALUES ?type {{
    <http://publications.europa.eu/ontology/cdm#regulation>
    <http://publications.europa.eu/ontology/cdm#directive>
    <http://publications.europa.eu/ontology/cdm#decision>
    <http://publications.europa.eu/ontology/cdm#proposal_for_regulation>
    <http://publications.europa.eu/ontology/cdm#proposal_for_directive>
    <http://publications.europa.eu/ontology/cdm#proposal_for_decision>
  }}
  ?work a ?type ;
        <http://publications.europa.eu/ontology/cdm#work_date_document> ?date ;
        <http://publications.europa.eu/ontology/cdm#resource_legal_id_celex> ?celex .
  OPTIONAL {{
    ?expr <http://publications.europa.eu/ontology/cdm#expression_belongs_to_work> ?work ;
          <http://publications.europa.eu/ontology/cdm#expression_uses_language>
            <http://publications.europa.eu/resource/authority/language/ENG> ;
          <http://publications.europa.eu/ontology/cdm#expression_title> ?title .
  }}
  FILTER(?date >= "{since}"^^xsd:date)
}}
ORDER BY DESC(?date) LIMIT {limit}
"""


def fetch_eurlex(conn: sqlite3.Connection, max_new: int = 20) -> int:
    from datetime import timedelta
    since = (date.today() - timedelta(days=60)).isoformat()
    query = EURLEX_SPARQL.format(since=since, limit=max_new * 3)

    try:
        r = requests.get(
            "https://publications.europa.eu/webapi/rdf/sparql",
            params={"query": query, "format": "application/sparql-results+json"},
            timeout=30,
            headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)"},
        )
        r.raise_for_status()
        bindings = r.json()["results"]["bindings"]
    except Exception as e:
        log.error("EUR-Lex CELLAR fetch failed: %s", e)
        return 0

    added = 0
    for b in bindings:
        celex = (b.get("celex", {}).get("value") or "").strip()
        if not celex:
            continue
        ext_id = f"eurlex:{celex}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        type_uri = (b.get("type", {}).get("value") or "")
        status   = "proposed" if "proposal_for" in type_uri else "enacted"
        title    = (b.get("title", {}).get("value") or celex).strip()
        pub_date = (b.get("date", {}).get("value") or "")[:10]
        url      = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
        raw      = f"Title: {title}\nCELEX: {celex}"

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("eurlex", "EU", ext_id, title, url, pub_date, date.today().isoformat(), raw, status, "eu"),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("EUR-Lex: %d new documents", added)
    return added


# ── UK GOV.UK fetcher ─────────────────────────────────────────────────────

_GOVUK_STATUS = {
    "legislation":    "enacted",
    "statutory_instrument": "enacted",
    "act":            "enacted",
    "policy_paper":   "proposed",
    "consultation":   "proposed",
    "press_release":  "unknown",
}


def fetch_uk_gov(conn: sqlite3.Connection, max_new: int = 10) -> int:
    try:
        r = requests.get(
            "https://www.gov.uk/api/search.json",
            params={
                "filter_content_store_document_type[]": [
                    "legislation", "statutory_instrument", "act",
                    "policy_paper", "consultation", "press_release",
                ],
                "count": 40,
                "order": "-public_timestamp",
                "fields[]": ["title", "description", "link", "public_timestamp",
                             "content_store_document_type"],
            },
            timeout=30,
            headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)"},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        log.error("GOV.UK fetch failed: %s", e)
        return 0

    added = 0
    for doc in results:
        rel_link = doc.get("link", "")
        ext_id = f"govuk:{rel_link}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        doc_type = (doc.get("content_store_document_type") or "").strip()
        title    = (doc.get("title") or "").strip()
        desc     = (doc.get("description") or "").strip()
        pub      = (doc.get("public_timestamp") or "")[:10]
        url      = "https://www.gov.uk" + rel_link
        raw      = f"Title: {title}\nType: {doc_type}\nSummary: {desc}"
        status   = _GOVUK_STATUS.get(doc_type, "unknown")

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("uk_gov", "GB", ext_id, title, url, pub, date.today().isoformat(), raw, status, "national"),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("GOV.UK: %d new documents", added)
    return added


# ── Canada Gazette fetcher ─────────────────────────────────────────────────

def fetch_canada_gazette(conn: sqlite3.Connection, max_new: int = 10) -> int:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime as _parse_date

    feeds = [
        ("https://www.gazette.gc.ca/rss/p2-eng.xml", "enacted"),   # Part II = final regulations
        ("https://www.gazette.gc.ca/rss/p1-eng.xml", "proposed"),  # Part I  = proposed regulations
    ]
    total_added = 0

    for feed_url, status in feeds:
        try:
            r = requests.get(feed_url, timeout=30,
                             headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            log.error("Canada Gazette fetch failed (%s): %s", feed_url, e)
            continue

        added = 0
        for item in root.iter("item"):
            link  = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()

            ext_id = f"ca_gazette:{link}"
            if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
                continue

            try:
                pub_date = _parse_date(pub).date().isoformat()
            except Exception:
                pub_date = date.today().isoformat()

            conn.execute(
                """INSERT OR IGNORE INTO policies
                   (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("canada_gazette", "CA", ext_id, title, link, pub_date,
                 date.today().isoformat(), f"Title: {title}\nSummary: {desc}", status, "national"),
            )
            added += 1
            if added >= max_new:
                break

        conn.commit()
        log.info("Canada Gazette (%s): %d new documents", status, added)
        total_added += added

    return total_added


# ── Australia legislation fetcher ──────────────────────────────────────────

def fetch_australia_legislation(conn: sqlite3.Connection, max_new: int = 10) -> int:
    # Federal Register of Legislation OData API — Acts and Legislative Instruments
    try:
        r = requests.get(
            "https://api.prod.legislation.gov.au/v1/Titles",
            params={
                "$top": 30,
                "$orderby": "makingDate desc",
                "$select": "id,name,makingDate,collection,publishComments",
                "$filter": "collection eq 'Act' or collection eq 'LegislativeInstrument'",
            },
            timeout=30,
            headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)", "Accept": "application/json"},
        )
        r.raise_for_status()
        items = r.json().get("value", [])
    except Exception as e:
        log.error("Australia legislation fetch failed: %s", e)
        return 0

    added = 0
    for item in items:
        leg_id = item.get("id", "")
        ext_id = f"au_leg:{leg_id}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        title    = (item.get("name") or "").strip()
        pub_date = (item.get("makingDate") or "")[:10]
        comments = (item.get("publishComments") or "").strip()
        url      = f"https://www.legislation.gov.au/Details/{leg_id}"
        raw      = f"Title: {title}\nType: {item.get('collection','')}\nNotes: {comments}"

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("au_legislation", "AU", ext_id, title, url, pub_date,
             date.today().isoformat(), raw, "enacted", "national"),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("Australia legislation: %d new documents", added)
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
        proposal_words = ("proposal", "consultation", "draft", "open for comment")
        haystack = (title + " " + desc).lower()
        ec_status = "proposed" if any(w in haystack for w in proposal_words) else "decision"

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("ec_press", "EU", ext_id, title, link, pub_date,
             date.today().isoformat(), raw, ec_status, "eu"),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("EC Press Corner: %d new documents", added)
    return added


# ── UK Parliament Bills fetcher ────────────────────────────────────────────

def fetch_uk_parliament(conn: sqlite3.Connection, max_new: int = 15) -> int:
    try:
        r = requests.get(
            "https://bills-api.parliament.uk/api/v1/Bills",
            params={"take": 50, "skip": 0},
            timeout=30,
            headers={
                "User-Agent": "CivilGate/1.0 (+https://civilgate.org)",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        log.error("UK Parliament Bills fetch failed: %s", e)
        return 0

    added = 0
    for bill in items:
        bill_id = bill.get("billId")
        if not bill_id:
            continue
        ext_id = f"ukparl:{bill_id}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        title       = (bill.get("shortTitle") or "").strip()
        long_title  = (bill.get("longTitle") or "").strip()
        last_update = (bill.get("lastUpdate") or "")[:10]
        stage       = bill.get("currentStage") or {}
        stage_desc  = (stage.get("description") or "").strip()
        house       = (bill.get("currentHouse") or "").strip()
        bill_type   = (bill.get("billType", {}).get("name") or "").strip()

        status = "enacted" if "royal assent" in stage_desc.lower() else "proposed"
        url    = f"https://bills.parliament.uk/bills/{bill_id}"
        raw    = "\n".join(filter(None, [
            f"Title: {title}",
            f"Full title: {long_title}",
            f"Type: {bill_type}",
            f"Stage: {stage_desc}",
            f"House: {house}",
        ]))

        conn.execute(
            """INSERT OR IGNORE INTO policies
               (source, country, external_id, title, url, published_date, fetched_date, raw_text, status, level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("uk_parliament", "GB", ext_id, title, url, last_update,
             date.today().isoformat(), raw, status, "national"),
        )
        added += 1
        if added >= max_new:
            break

    conn.commit()
    log.info("UK Parliament: %d new bills", added)
    return added


# ── Gemini scorer ──────────────────────────────────────────────────────────

SCORE_PROMPT = """\
You are a policy analyst. Given the following government policy document, \
return a JSON object with exactly these fields:

Primary scores (always required):
- "summary": 2-3 sentence plain English summary for someone with no political background
- "social_score": integer -10 to +10 (negative = harm to people, positive = benefit; 0 = negligible)
- "social_reason": one sentence, starting with the direction ("Expands access to..." or "Restricts...")
- "environmental_score": integer -10 to +10 (negative = environmental harm, positive = benefit)
- "environmental_reason": one sentence
- "economic_score": integer -10 to +10 (negative = economic harm, positive = benefit; public-interest perspective)
- "economic_reason": one sentence

Secondary scores (required — use 0 if not applicable):
- "human_rights_score": integer -10 to +10 (effect on civil liberties, freedoms, minority rights, due process, press freedom)
- "human_rights_reason": one sentence (use "No significant human rights impact" if score is 0)
- "governance_score": integer -10 to +10 (effect on rule of law, transparency, anti-corruption, democratic institutions, institutional independence)
- "governance_reason": one sentence (use "No significant governance impact" if score is 0)

Classification:
- "tags": array of up to 5 lowercase topic keywords
- "status": one of "enacted", "proposed", "open_for_comment", "decision"
- "scope": one of "global", "regional", "national", "local"
- "scope_reason": one sentence

Calibration examples:
- Oil drilling expansion → environmental_score: -8, economic_score: +4
- Healthcare expansion → social_score: +9, economic_score: -3
- Surveillance law without oversight → human_rights_score: -7, governance_score: -5
- Anti-corruption agency established → governance_score: +8
- Press freedom restriction → human_rights_score: -8
- EC Press Corner funding disbursement (no vote pending) → status: "decision"
- Proposed regulation open for public comment → status: "open_for_comment"
- EU NextGenerationEU payment to Poland → scope: "national"
- EU-US trade agreement → scope: "global"
- New EU single market regulation → scope: "regional"

Return ONLY valid JSON. No markdown, no code fences.

Document:
{text}"""


def _parse_llm_response(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw).strip()
    # Remove trailing commas before } or ] (common LLM mistake)
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m:
            candidate = re.sub(r",\s*([}\]])", r"\1", m.group())
            return json.loads(candidate)
        raise


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
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_llm_response(raw)


def call_cerebras(text: str) -> dict:
    r = requests.post(
        "https://api.cerebras.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": SCORE_PROMPT.format(text=text[:6000])}],
            "temperature": 0.3,
            "max_tokens": 1024,
        },
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return _parse_llm_response(r.json()["choices"][0]["message"]["content"])


def call_groq(text: str) -> dict:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": SCORE_PROMPT.format(text=text[:6000])}],
            "temperature": 0.3,
            "max_tokens": 1024,
        },
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return _parse_llm_response(r.json()["choices"][0]["message"]["content"])


def call_llm(text: str) -> dict:
    """Try Gemini → Cerebras → Groq; raise if all exhausted."""
    providers = []
    if GEMINI_API_KEY:
        providers.append(("Gemini", call_gemini))
    if CEREBRAS_API_KEY:
        providers.append(("Cerebras", call_cerebras))
    if GROQ_API_KEY:
        providers.append(("Groq", call_groq))

    last_err = None
    for name, fn in providers:
        try:
            result = fn(text)
            return result
        except RuntimeError as e:
            if "RATE_LIMIT" in str(e):
                log.warning("%s rate limited, trying next provider…", name)
                last_err = e
                continue
            raise
        except Exception as e:
            log.warning("%s failed (%s), trying next provider…", name, e)
            last_err = e
            continue

    raise RuntimeError(f"All LLM providers exhausted. Last error: {last_err}")


def score_pending(conn: sqlite3.Connection, limit: int = 30) -> int:
    rows = conn.execute(
        """SELECT id, raw_text FROM policies
           WHERE summary IS NULL AND score_failed = 0
           ORDER BY published_date DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    scored = 0
    for row_id, raw_text in rows:
        try:
            result = call_llm(raw_text or "")
            _VALID_STATUSES = {"enacted", "proposed", "open_for_comment", "decision"}
            llm_status = result.get("status")
            update_status = llm_status if llm_status in _VALID_STATUSES else None
            conn.execute(
                """UPDATE policies SET
                   summary=?, social_score=?, social_reason=?,
                   environmental_score=?, environmental_reason=?,
                   economic_score=?, economic_reason=?,
                   human_rights_score=?, human_rights_reason=?,
                   governance_score=?, governance_reason=?,
                   tags=?, scored_at=?, scorer_version=?,
                   scope=?, scope_reason=?,
                   status=COALESCE(?, status),
                   score_failed=0
                   WHERE id=?""",
                (
                    result.get("summary"),
                    result.get("social_score"), result.get("social_reason"),
                    result.get("environmental_score"), result.get("environmental_reason"),
                    result.get("economic_score"), result.get("economic_reason"),
                    result.get("human_rights_score"), result.get("human_rights_reason"),
                    result.get("governance_score"), result.get("governance_reason"),
                    json.dumps(result.get("tags", []), ensure_ascii=False),
                    datetime.utcnow().isoformat(),
                    SCORER_VERSION,
                    result.get("scope"),
                    result.get("scope_reason"),
                    update_status,
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
        "economic_score", "economic_reason",
        "human_rights_score", "human_rights_reason",
        "governance_score", "governance_reason",
        "tags", "status", "level", "scope", "scope_reason",
    ]
    rows = conn.execute(
        f"""SELECT {','.join(cols)} FROM policies
            WHERE summary IS NOT NULL
            ORDER BY published_date DESC LIMIT 500""",
    ).fetchall()

    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)

    export = {
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "policies": out,
    }
    OUT_FILE.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
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

    # Reset transient failures so they get retried on the next daily run
    conn.execute(
        "UPDATE policies SET score_failed=0 WHERE score_failed=1 AND summary IS NULL"
    )
    conn.commit()

    fetch_federal_register(conn)
    fetch_ec_press(conn)
    fetch_eurlex(conn)
    fetch_uk_gov(conn)
    fetch_uk_parliament(conn)
    fetch_canada_gazette(conn)
    fetch_australia_legislation(conn)

    try:
        from monitorul_oficial import fetch_monitorul_oficial
        fetch_monitorul_oficial(conn)
    except Exception as e:
        log.error("Monitorul Oficial fetch failed: %s", e)

    score_pending(conn)
    export_json(conn)

    conn.close()

    push_to_github({"policies.json": OUT_FILE.read_bytes()})
    log.info("Pipeline complete.")


if __name__ == "__main__":
    main()
