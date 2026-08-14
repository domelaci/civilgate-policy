#!/usr/bin/env python3
"""Read-only calibration test: re-score 10 low-scored policies and compare."""
import json, os, re, time, sqlite3
from pathlib import Path
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GROQ_KEY     = os.environ.get("GROQ_API_KEY", "")
CEREBRAS_KEY = os.environ.get("CEREBRAS_API_KEY", "")

# Pull the patched prompt straight from pipeline.py so this always stays in sync
_src = (BASE_DIR / "pipeline.py").read_text()
PROMPT = _src.split('SCORE_PROMPT = """\\\n')[1].split('\n\n\ndef _parse_llm_response')[0].rstrip('"""')


def parse(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw).strip()
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group()))
        raise


def call_gemini(text: str) -> dict:
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={GEMINI_KEY}",
        json={"contents": [{"role": "user", "parts": [{"text": PROMPT.format(text=text[:6000])}]}],
              "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}},
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return parse(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def call_groq(text: str) -> dict:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": "llama-3.3-70b-versatile",
              "messages": [{"role": "user", "content": PROMPT.format(text=text[:6000])}],
              "temperature": 0.3, "max_tokens": 1024},
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return parse(r.json()["choices"][0]["message"]["content"])


def score(text: str) -> dict:
    for name, fn in [("Gemini", call_gemini), ("Groq", call_groq)]:
        try:
            result = fn(text)
            print(f"  [{name}]", end=" ", flush=True)
            return result
        except RuntimeError as e:
            if "RATE_LIMIT" in str(e):
                print(f"  [{name} rate-limited, trying next]", end=" ", flush=True)
            else:
                raise
        except Exception as e:
            print(f"  [{name} error: {e}, trying next]", end=" ", flush=True)
    raise RuntimeError("All providers exhausted")


conn = sqlite3.connect(BASE_DIR / "policies.db")
rows = conn.execute("""
    SELECT title, raw_text, environmental_score, social_score, source, country
    FROM policies
    WHERE summary IS NOT NULL
      AND (environmental_score BETWEEN -1 AND 2 OR social_score BETWEEN -1 AND 2)
      AND LENGTH(raw_text) > 80
      AND raw_text NOT LIKE '%budget line%'
      AND raw_text NOT LIKE '%corrigendum%'
    ORDER BY RANDOM()
    LIMIT 10
""").fetchall()

print(f"\n{'─'*100}")
print(f"{'TITLE':<50} {'SRC':<12} {'OLD env':>7} {'NEW env':>7} {'OLD soc':>7} {'NEW soc':>7}")
print(f"{'─'*100}")

for title, raw_text, old_env, old_soc, source, country in rows:
    short = (title or raw_text[:80]).strip()[:48]
    print(f"\n{short:<50} {(source+'/'+country):<12}", end=" ", flush=True)
    try:
        r = score(raw_text or title)
        new_env = r.get("environmental_score", "?")
        new_soc = r.get("social_score", "?")
        env_arrow = "▲" if isinstance(new_env, int) and isinstance(old_env, int) and new_env > old_env else ("▼" if isinstance(new_env, int) and isinstance(old_env, int) and new_env < old_env else " ")
        soc_arrow = "▲" if isinstance(new_soc, int) and isinstance(old_soc, int) and new_soc > old_soc else ("▼" if isinstance(new_soc, int) and isinstance(old_soc, int) and new_soc < old_soc else " ")
        print(f"{str(old_env):>7} {str(new_env)+env_arrow:>8} {str(old_soc):>7} {str(new_soc)+soc_arrow:>8}")
        time.sleep(2)
    except Exception as e:
        print(f"  ERROR: {e}")

print(f"\n{'─'*100}")
conn.close()
