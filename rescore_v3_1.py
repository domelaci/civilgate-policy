#!/usr/bin/env python3
"""
Re-score v3 items where governance_score = 1 using the v3.1 prompt.

Targets the ~11 000 miscalibrated items where the model assigned governance_score=1
to routine legislation that passes through normal parliamentary process.
v3.1 adds DIMENSION-SPECIFIC CALIBRATION to suppress that bias.

Usage:
  python3 rescore_v3_1.py                     # single worker, batches of 500
  python3 rescore_v3_1.py --batch 200         # smaller batches
  python3 rescore_v3_1.py --no-push           # skip export+push (parallel workers)

  # 5 parallel workers:
  for i in 1 2 3 4 5; do
    nohup python3 rescore_v3_1.py --no-push >> rescore_v3_1_w${i}.log 2>&1 &
  done
"""
import argparse
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
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCORER_VERSION = "v3.1"

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

TARGET_FILTER = "scorer_version = 'v3' AND governance_score = 1"


def _parse(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw).strip()
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group()))
        raise


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
    return _parse(r.json()["choices"][0]["message"]["content"])


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
        f"SELECT {','.join(cols)} FROM policies WHERE summary IS NOT NULL "
        "ORDER BY published_date DESC LIMIT 5000"
    ).fetchall()
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)
    data = {"last_updated": datetime.utcnow().isoformat() + "Z", "policies": out}
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Exported %d policies to policies.json", len(out))


def push_to_github() -> None:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    import base64
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/policies.json"
    r = requests.get(api_url, headers=headers, timeout=15)
    sha = r.json()["sha"] if r.status_code == 200 else None
    payload = {
        "message": f"chore: v3.1 rescore update {date.today().isoformat()}",
        "content": base64.b64encode(OUT_FILE.read_bytes()).decode(),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(api_url, headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        log.info("GitHub push OK")
    else:
        log.error("GitHub push FAILED: %s %s", r.status_code, r.text[:200])


def rescore_batch(conn: sqlite3.Connection, batch_size: int) -> int:
    rows = conn.execute(
        f"""SELECT id, raw_text FROM policies
            WHERE {TARGET_FILTER}
            ORDER BY RANDOM()
            LIMIT ?""",
        (batch_size,),
    ).fetchall()

    if not rows:
        return 0

    done = 0
    for row_id, raw_text in rows:
        retries = 0
        while True:
            try:
                result = call_deepseek(raw_text or "")
                break
            except RuntimeError as e:
                if "RATE_LIMIT" in str(e):
                    wait = 60 * (2 ** retries)
                    log.warning("DeepSeek rate limited — sleeping %ds", wait)
                    time.sleep(wait)
                    retries += 1
                else:
                    raise
            except Exception as e:
                log.error("DeepSeek error id=%d: %s — skipping", row_id, e)
                result = None
                break

        if result is None:
            continue

        conn.execute(
            """UPDATE policies SET
               summary=?, social_score=?, social_reason=?,
               environmental_score=?, environmental_reason=?,
               economic_score=?, economic_reason=?,
               human_rights_score=?, human_rights_reason=?,
               governance_score=?, governance_reason=?,
               tags=?, scope=?, scope_reason=?,
               scored_at=?, scorer_version=?, scored_by=?, score_failed=0
               WHERE id=?""",
            (
                result.get("summary"),
                result.get("social_score"),        result.get("social_reason"),
                result.get("environmental_score"), result.get("environmental_reason"),
                result.get("economic_score"),      result.get("economic_reason"),
                result.get("human_rights_score"),  result.get("human_rights_reason"),
                result.get("governance_score"),    result.get("governance_reason"),
                json.dumps(result.get("tags", []), ensure_ascii=False),
                result.get("scope"), result.get("scope_reason"),
                datetime.utcnow().isoformat(),
                SCORER_VERSION, "DeepSeek",
                row_id,
            ),
        )
        conn.commit()
        done += 1
        if done % 50 == 0:
            remaining = conn.execute(
                f"SELECT COUNT(*) FROM policies WHERE {TARGET_FILTER}"
            ).fetchone()[0]
            log.info("Rescored %d this batch — %d items remaining", done, remaining)
        time.sleep(1)

    return done


if __name__ == "__main__":
    if not DEEPSEEK_API_KEY:
        raise SystemExit("Missing DEEPSEEK_API_KEY in .env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--batch",   type=int, default=500)
    parser.add_argument("--no-push", action="store_true", help="Skip export+push (parallel workers)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    total = conn.execute(
        f"SELECT COUNT(*) FROM policies WHERE {TARGET_FILTER}"
    ).fetchone()[0]
    log.info("v3.1 rescore — %d items to reprocess (scorer_version=v3, governance_score=1)", total)

    grand_total = 0
    while True:
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM policies WHERE {TARGET_FILTER}"
        ).fetchone()[0]
        if remaining == 0:
            log.info("All v3/governance_score=1 items upgraded to v3.1. Done.")
            break

        log.info("=== Batch — %d remaining ===", remaining)
        done = rescore_batch(conn, args.batch)
        grand_total += done
        log.info("Batch complete: %d rescored, %d total so far", done, grand_total)

        if not args.no_push:
            export_json(conn)
            push_to_github()
