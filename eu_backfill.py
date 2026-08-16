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
import base64
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
MISTRAL_API_KEY  = os.environ.get("MISTRAL_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCORER_VERSION = "v3.1"


def push_to_github() -> None:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    path = "policies.json"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    sha = None
    r = requests.get(api_url, headers=headers, timeout=15)
    if r.status_code == 200:
        sha = r.json()["sha"]
    payload = {
        "message": f"chore: scoring update {date.today().isoformat()}",
        "content": base64.b64encode(OUT_FILE.read_bytes()).decode(),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(api_url, headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        log.info("GitHub push OK: policies.json")
    else:
        log.error("GitHub push FAILED: %s %s", r.status_code, r.text[:200])

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
You are a policy analyst scoring government policies for a public tracking tool. \
Your job is to signal what matters — scores that cluster near zero are useless.

SCORING RULES — read before assigning any number:
1. 0 means "this policy has no meaningful effect on this dimension." Reserve it for \
genuinely neutral items: budget line corrections, technical amendments, routine approvals.
2. ±1 to ±2 means minor or indirect effect only.
3. ±3 to ±5 means moderate, real-world impact on a meaningful subset of people or systems.
4. ±6 to ±8 means significant impact: a major law, a large programme, a serious rights change.
5. ±9 to ±10 means transformative or extreme: landmark legislation, mass-scale harm or benefit, \
irreversible change.
If 80% of policies score between -3 and +3, the calibration is wrong. Most real legislation \
has clear winners and losers — score that reality.

DIMENSION-SPECIFIC CALIBRATION:

- governance_score 0 = no meaningful change to accountability, transparency, rule of law, or \
institutional integrity. Routine legislation, budget line corrections, technical amendments, \
administrative reorganisations, and standard regulatory updates all score 0. Only score ±1 or \
above if there is a genuine, direct change to democratic process, oversight mechanisms, \
anti-corruption measures, judicial independence, or institutional transparency. Do not reward \
a law simply for existing or for being passed through normal parliamentary process.

- human_rights_score 0 = no meaningful change to civil liberties, individual rights, or \
protections for vulnerable groups. Administrative and economic laws with no rights implications \
score 0. Only score positive if rights are meaningfully expanded, only score negative if rights \
are meaningfully restricted.

- environmental_score 0 = no meaningful environmental implication. The majority of legislation \
(economic, social, administrative) scores 0. Only score if the law directly affects land use, \
emissions, pollution, biodiversity, energy, water, or natural resources.

Fields to return as JSON:
- "summary": 2-3 sentence plain English summary for someone with no political background
- "social_score": integer -10 to +10
- "social_reason": one sentence starting with the direction ("Expands...", "Restricts...", "Cuts...")
- "environmental_score": integer -10 to +10
- "environmental_reason": one sentence
- "economic_score": integer -10 to +10 (public-interest perspective — who gains, who loses)
- "economic_reason": one sentence
- "human_rights_score": integer -10 to +10 (civil liberties, freedoms, minority rights, due process, \
press freedom); use 0 only if genuinely no rights dimension
- "human_rights_reason": one sentence
- "governance_score": integer -10 to +10 (rule of law, transparency, anti-corruption, democratic \
institutions, judicial independence); use 0 only if genuinely no governance dimension
- "governance_reason": one sentence
- "tags": array of up to 5 lowercase topic keywords
- "scope": one of "global", "regional", "national", "local"
- "scope_reason": one sentence

CALIBRATION — anchor your scores to these:
Social:
  +9  Universal healthcare covering 50 million people who had none before
  +7  Paid parental leave introduced for all workers nationally
  +5  Major public housing programme (tens of thousands of new units)
  -5  Benefit cuts affecting 2 million low-income households
  -8  Forced displacement of a minority population
  -10 Apartheid-style segregation law

Environmental:
  +8  Full national coal phase-out by 2030
  +8  $369 billion clean energy investment (Inflation Reduction Act scale)
  +5  National ban on single-use plastics
  -5  Opening protected marine areas to commercial fishing
  -8  Oil drilling approved in a major protected ecosystem
  -9  Deforestation of millions of hectares of primary rainforest legalised

Economic:
  +6  Free trade agreement opening major new export markets
  +4  Significant minimum wage increase benefiting millions of low-paid workers
  -3  Tariff increases raising costs for consumers and businesses
  -6  Austerity cuts reducing public investment by 10%+ of GDP

Human rights:
  +7  Legalising same-sex marriage nationally
  +6  Abolishing the death penalty
  -6  Comprehensive surveillance law with no judicial oversight
  -8  Press censorship law or forced closure of independent media
  -10 Law criminalising political opposition or banning minority groups

Governance:
  +7  Independent anti-corruption agency established with real investigative powers
  +5  Freedom of information law expanding public access to government records
  -5  Gerrymandering law entrenching one party's electoral dominance
  -8  Executive takeover of judicial appointments, removing court independence
  -9  Suspension of parliament or constitution during non-emergency

Scope calibration:
  - EU fund payment to a single member state → scope: "national"
  - EU-US trade agreement → scope: "global"
  - New EU single market regulation → scope: "regional"
  - Minor technical budget amendment → all scores 0 or ±1

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


def call_deepseek(text: str) -> dict:
    r = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
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


_cooldown: dict[str, float] = {}  # provider -> unix timestamp when it can be retried
COOLDOWN_SECS = 3600  # 1 hour


def call_llm(text: str) -> dict:
    """Try Gemini → Groq → DeepSeek → Mistral; return (result, provider_name).
    Skips providers that are in cooldown after a 429."""
    providers = []
    if GEMINI_API_KEY:
        providers.append(("Gemini", call_gemini))
    if GROQ_API_KEY:
        providers.append(("Groq", call_groq))
    if DEEPSEEK_API_KEY:
        providers.append(("DeepSeek", call_deepseek))
    if MISTRAL_API_KEY:
        providers.append(("Mistral", call_mistral))

    now = time.time()
    last_err = None
    for name, fn in providers:
        retry_after = _cooldown.get(name, 0)
        if now < retry_after:
            mins = int((retry_after - now) / 60)
            log.debug("%s in cooldown for ~%d more min, skipping", name, mins)
            continue
        try:
            result = fn(text)
            _cooldown.pop(name, None)  # clear cooldown on success
            return result, name
        except RuntimeError as e:
            if "RATE_LIMIT" in str(e):
                _cooldown[name] = now + COOLDOWN_SECS
                log.warning("%s rate limited — cooling down for %d min", name, COOLDOWN_SECS // 60)
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
            result, provider = call_llm(raw_text or "")
            conn.execute(
                """UPDATE policies SET
                   summary=?, social_score=?, social_reason=?,
                   environmental_score=?, environmental_reason=?,
                   economic_score=?, economic_reason=?,
                   tags=?, scored_at=?, scorer_version=?,
                   scope=?, scope_reason=?, scored_by=?, score_failed=0
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
                    provider,
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
        "tags", "status", "level", "scope", "scope_reason", "is_live",
    ]
    rows = conn.execute(
        f"""SELECT {','.join(cols)} FROM policies
            WHERE summary IS NOT NULL
            ORDER BY published_date DESC LIMIT 5000""",
    ).fetchall()

    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)

    total_count = conn.execute("SELECT count(*) FROM policies").fetchone()[0]
    export = {
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "total_count": total_count,
        "policies": out,
    }
    OUT_FILE.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Exported %d policies to policies.json (%d total in DB)", len(out), total_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EU historical backfill for CivilGate")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch/insert, skip scoring")
    parser.add_argument("--score-only", action="store_true", help="Skip fetch, only score pending")
    parser.add_argument("--score-limit", type=int, default=100, help="Max items to score per run (default: 100)")
    parser.add_argument("--start-year", type=int, default=2015, help="Start year for backfill (default: 2015)")
    args = parser.parse_args()

    if not any([GEMINI_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY, MISTRAL_API_KEY]) and not args.fetch_only:
        raise SystemExit("Missing GEMINI_API_KEY in .env")

    conn = sqlite3.connect(DB_FILE)

    # Ensure schema is up to date (adds scorer_version and any other new columns)
    for col, definition in [
        ("scorer_version", "TEXT"), ("level", "TEXT"), ("status", "TEXT"),
        ("scope", "TEXT"), ("scope_reason", "TEXT"),
        ("human_rights_score", "INTEGER"), ("human_rights_reason", "TEXT"),
        ("governance_score", "INTEGER"), ("governance_reason", "TEXT"),
        ("scored_by", "TEXT"), ("is_live", "INTEGER DEFAULT 0"),
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
        push_to_github()

    conn.close()
    log.info("Done.")
