#!/usr/bin/env python3
"""
One-shot script: backfill scope + scope_reason for already-scored policies
that are missing it. Uses title + summary only (fast, short prompts).
Prioritises the 500 most-recently-published rows that will appear in the export.
"""
import json, os, re, sqlite3, time, logging
from pathlib import Path
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROMPT = """\
Classify the geographic scope of this government policy.

Return ONLY a JSON object with exactly two keys:
- "scope": one of "global", "regional", "national", "local"
  global = affects multiple countries or international relations (trade deals, climate agreements, sanctions)
  regional = affects a multi-country bloc (EU single market rules, NATO, G7, Schengen area)
  national = affects one country only — an EU fund payment or enforcement action directed at a single member state is national
  local = affects a specific region, city, or locality within one country
- "scope_reason": one sentence explaining the classification

Title: {title}
Summary: {summary}"""


def _parse(raw: str) -> dict:
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
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={GEMINI_API_KEY}",
        json={"contents": [{"role": "user", "parts": [{"text": text}]}],
              "generationConfig": {"temperature": 0.1, "maxOutputTokens": 128}},
        timeout=30,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return _parse(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def call_cerebras(text: str) -> dict:
    r = requests.post(
        "https://api.cerebras.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}"},
        json={"model": "gpt-oss-120b", "messages": [{"role": "user", "content": text}],
              "temperature": 0.1, "max_tokens": 128},
        timeout=30,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return _parse(r.json()["choices"][0]["message"]["content"])


def call_groq(text: str) -> dict:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": text}],
              "temperature": 0.1, "max_tokens": 128},
        timeout=30,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return _parse(r.json()["choices"][0]["message"]["content"])


def classify(title: str, summary: str) -> dict:
    text = PROMPT.format(title=title, summary=summary)
    providers = []
    if GEMINI_API_KEY:   providers.append(("Gemini",   call_gemini))
    if CEREBRAS_API_KEY: providers.append(("Cerebras", call_cerebras))
    if GROQ_API_KEY:     providers.append(("Groq",     call_groq))
    last = None
    for name, fn in providers:
        try:
            return fn(text)
        except RuntimeError as e:
            if "RATE_LIMIT" in str(e):
                log.warning("%s rate limited, trying next…", name)
                last = e
            else:
                raise
        except Exception as e:
            log.warning("%s failed (%s), trying next…", name, e)
            last = e
    raise RuntimeError(f"All providers exhausted. Last: {last}")


def main():
    conn = sqlite3.connect(BASE_DIR / "policies.db")

    # Fetch the 500 most recent scored rows missing scope (same slice as export)
    rows = conn.execute("""
        SELECT id, title, summary FROM policies
        WHERE summary IS NOT NULL AND scope IS NULL
        ORDER BY published_date DESC
        LIMIT 500
    """).fetchall()

    log.info("Backfilling scope for %d policies…", len(rows))
    done = 0
    for row_id, title, summary in rows:
        try:
            result = classify(title or "", summary or "")
            conn.execute(
                "UPDATE policies SET scope=?, scope_reason=? WHERE id=?",
                (result.get("scope"), result.get("scope_reason"), row_id),
            )
            conn.commit()
            done += 1
            if done % 20 == 0:
                log.info("%d / %d done…", done, len(rows))
            time.sleep(0.5)
        except Exception as e:
            log.error("id=%d failed: %s", row_id, e)

    log.info("Scope backfill complete: %d classified.", done)

    # Re-export policies.json
    cols = [
        "source", "country", "external_id", "title", "url", "published_date",
        "summary", "social_score", "social_reason",
        "environmental_score", "environmental_reason",
        "economic_score", "economic_reason", "tags", "status", "level",
        "scope", "scope_reason",
    ]
    out_rows = conn.execute(
        f"SELECT {','.join(cols)} FROM policies WHERE summary IS NOT NULL ORDER BY published_date DESC LIMIT 500"
    ).fetchall()
    out = []
    for row in out_rows:
        d = dict(zip(cols, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)

    import datetime as dt
    total_count = conn.execute("SELECT count(*) FROM policies").fetchone()[0]
    export = {"last_updated": dt.datetime.utcnow().isoformat() + "Z", "total_count": total_count, "policies": out}
    (BASE_DIR / "policies.json").write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Exported %d policies to policies.json (%d total in DB)", len(out), total_count)
    conn.close()


if __name__ == "__main__":
    main()
