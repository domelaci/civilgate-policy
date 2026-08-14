#!/usr/bin/env python3
"""
EU historical backfill — fetches EUR-Lex items month-by-month from 2024-01-01.
Safe to re-run (INSERT OR IGNORE skips existing records).

Usage:
  python3 eu_backfill.py                      # fetch + score up to 100 pending, then export
  python3 eu_backfill.py --fetch-only         # only fetch/insert, skip scoring
  python3 eu_backfill.py --score-limit 200    # score up to 200 items instead of 100
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
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
MISTRAL_API_KEY  = os.environ.get("MISTRAL_API_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCORER_VERSION = "v1"

EURLEX_SPARQL = """\
SELECT DISTINCT ?work ?date ?title ?celex WHERE {{
  ?work <http://publications.europa.eu/ontology/cdm#resource_legal_id_celex> ?celex ;
        <http://publications.europa.eu/ontology/cdm#work_date_document> ?date .
  OPTIONAL {{
    ?expr <http://publications.europa.eu/ontology/cdm#expression_belongs_to_work> ?work ;
          <http://publications.europa.eu/ontology/cdm#expression_uses_language>
            <http://publications.europa.eu/resource/authority/language/ENG> ;
          <http://publications.europa.eu/ontology/cdm#expression_title> ?title .
  }}
  FILTER(
    REGEX(STR(?celex), "^3{year}[RLD]") &&
    ?date >= "{since}"^^xsd:date &&
    ?date <= "{until}"^^xsd:date &&
    !REGEX(STR(?celex), "R\\\\(")
  )
}}
ORDER BY DESC(?date) LIMIT 200
"""


def fetch_eurlex_month(conn: sqlite3.Connection, since: str, until: str) -> int:
    year = since[:4]
    query = EURLEX_SPARQL.format(since=since, until=until, year=year)
    try:
        r = requests.get(
            "https://publications.europa.eu/webapi/rdf/sparql",
            params={"query": query, "format": "application/sparql-results+json"},
            timeout=60,
            headers={"User-Agent": "CivilGate/1.0 (+https://civilgate.org)"},
        )
        r.raise_for_status()
        bindings = r.json()["results"]["bindings"]
    except Exception as e:
        log.error("EUR-Lex SPARQL failed (%s – %s): %s", since, until, e)
        return 0

    added = 0
    for b in bindings:
        celex = (b.get("celex", {}).get("value") or "").strip()
        if not celex:
            continue
        ext_id = f"eurlex:{celex}"
        if conn.execute("SELECT 1 FROM policies WHERE external_id=?", (ext_id,)).fetchone():
            continue

        status = "enacted"  # CELEX prefix 3YYYY[RLD] = adopted legislation
        title  = (b.get("title", {}).get("value") or celex).strip()
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

    conn.commit()
    return added


def backfill_eurlex(conn: sqlite3.Connection, start_year: int = 2024) -> int:
    current  = date(start_year, 1, 1)
    end      = date.today()
    total    = 0

    while current <= end:
        # Last day of month
        next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end  = next_month - timedelta(days=1)
        if month_end > end:
            month_end = end

        added = fetch_eurlex_month(conn, current.isoformat(), month_end.isoformat())
        total += added
        log.info("EUR-Lex %s – %s: +%d (running total: %d)", current, month_end, added, total)

        current = next_month
        time.sleep(2)  # gentle rate limit

    log.info("EUR-Lex backfill complete: %d new records", total)
    return total


SCORE_PROMPT = """\
You are a policy analyst. Given the following government policy document, \
return a JSON object with exactly these keys:
- "summary": 2-3 sentence plain English summary for someone with no political background
- "social_score": integer from -10 to +10. Positive = net benefit to people (rights, welfare, equality, health, education). Negative = net harm. 0 = neutral or negligible. Score direction AND magnitude together.
- "social_reason": one sentence explaining the social score, starting with the direction (e.g. "Expands access to..." or "Restricts...")
- "environmental_score": integer from -10 to +10. Positive = environmental benefit (emissions cut, habitat protection). Negative = environmental harm (pollution, deforestation, fossil fuel expansion).
- "environmental_reason": one sentence
- "economic_score": integer from -10 to +10. Positive = economic benefit (jobs, growth, fair trade). Negative = economic harm (costs, market distortion, inequality). Score from a public-interest perspective.
- "economic_reason": one sentence
- "tags": array of up to 5 lowercase topic keywords
- "scope": one of "global", "regional", "national", "local". global = affects multiple countries or international relations (trade deals, climate agreements, sanctions, tariffs). regional = affects a multi-country bloc (EU single market rules, NATO, G7, Schengen). national = affects one country only — an EU fund payment or enforcement decision directed at a single member state is national, not regional. local = affects a specific region, city, or locality within one country.
- "scope_reason": one sentence explaining the scope classification

Return ONLY valid JSON. No markdown, no code fences.

Document:
{text}"""


def _parse_llm_response(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw).strip()
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


def call_mistral(text: str) -> dict:
    r = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "open-mistral-nemo",
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
    """Try Gemini → Cerebras → Groq → Mistral; raise if all exhausted."""
    providers = []
    if GEMINI_API_KEY:
        providers.append(("Gemini", call_gemini))
    if CEREBRAS_API_KEY:
        providers.append(("Cerebras", call_cerebras))
    if GROQ_API_KEY:
        providers.append(("Groq", call_groq))
    if MISTRAL_API_KEY:
        providers.append(("Mistral", call_mistral))

    last_err = None
    for name, fn in providers:
        try:
            return fn(text)
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


def score_pending(conn: sqlite3.Connection, limit: int = 100) -> int:
    rows = conn.execute(
        """SELECT id, raw_text FROM policies
           WHERE summary IS NULL AND score_failed = 0
           ORDER BY published_date DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    pending_total = conn.execute(
        "SELECT COUNT(*) FROM policies WHERE summary IS NULL AND score_failed = 0"
    ).fetchone()[0]
    log.info("Pending scoring: %d total, scoring up to %d this run", pending_total, limit)

    scored = 0
    for row_id, raw_text in rows:
        if not raw_text or len(raw_text.strip()) < 20:
            conn.execute("UPDATE policies SET score_failed=1 WHERE id=?", (row_id,))
            conn.commit()
            continue
        try:
            result = call_llm(raw_text or "")
            conn.execute(
                """UPDATE policies SET
                   summary=?, social_score=?, social_reason=?,
                   environmental_score=?, environmental_reason=?,
                   economic_score=?, economic_reason=?,
                   tags=?, scored_at=?, scorer_version=?,
                   scope=?, scope_reason=?, score_failed=0
                   WHERE id=?""",
                (
                    result.get("summary"),
                    result.get("social_score"), result.get("social_reason"),
                    result.get("environmental_score"), result.get("environmental_reason"),
                    result.get("economic_score"), result.get("economic_reason"),
                    json.dumps(result.get("tags", []), ensure_ascii=False),
                    datetime.utcnow().isoformat(),
                    SCORER_VERSION,
                    result.get("scope"),
                    result.get("scope_reason"),
                    row_id,
                ),
            )
            conn.commit()
            scored += 1
            if scored % 10 == 0:
                log.info("Scored %d / %d so far…", scored, limit)
            time.sleep(1)
        except Exception as e:
            log.error("Scoring failed for id=%d: %s", row_id, e)
            conn.execute("UPDATE policies SET score_failed=1 WHERE id=?", (row_id,))
            conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) FROM policies WHERE summary IS NULL AND score_failed = 0"
    ).fetchone()[0]
    log.info("Scored %d this run. Still pending: %d", scored, remaining)
    return scored


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EU historical backfill for CivilGate")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch/insert, skip scoring")
    parser.add_argument("--score-only", action="store_true", help="Skip fetch, only score pending")
    parser.add_argument("--score-limit", type=int, default=100, help="Max items to score per run (default: 100)")
    parser.add_argument("--start-year", type=int, default=2015, help="Start year for backfill (default: 2015)")
    args = parser.parse_args()

    if not any([GEMINI_API_KEY, GROQ_API_KEY, CEREBRAS_API_KEY]) and not args.fetch_only:
        raise SystemExit("Missing GEMINI_API_KEY in .env")

    conn = sqlite3.connect(DB_FILE)

    # Ensure schema is up to date (adds scorer_version and any other new columns)
    for col, definition in [
        ("scorer_version", "TEXT"), ("level", "TEXT"), ("status", "TEXT"),
        ("scope", "TEXT"), ("scope_reason", "TEXT"),
        ("human_rights_score", "INTEGER"), ("human_rights_reason", "TEXT"),
        ("governance_score", "INTEGER"), ("governance_reason", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE policies ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            pass

    if not args.score_only:
        log.info("Starting EUR-Lex backfill from %d-01-01 (regulations + directives, no corrigenda)…", args.start_year)
        backfill_eurlex(conn, start_year=args.start_year)

    if not args.fetch_only:
        conn.execute("UPDATE policies SET score_failed=0 WHERE score_failed=1 AND summary IS NULL")
        conn.commit()
        log.info("Scoring pending items (limit=%d)…", args.score_limit)
        score_pending(conn, limit=args.score_limit)
        log.info("Exporting policies.json…")
        export_json(conn)

    conn.close()
    log.info("Done.")
