#!/usr/bin/env python3
"""CivilGate backfill admin — port 5051."""
import json, os, secrets, sqlite3, subprocess, time
from datetime import timedelta
from pathlib import Path
from flask import Flask, jsonify, request, session, redirect, url_for
from werkzeug.security import check_password_hash
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"

USERNAME      = "domelaci"
PASSWORD_HASH = "scrypt:32768:8:1$n67wx2JX6GeD7mJu$c5065d74a5aba1d39832701702b7576d7fed0406b871fdff96769aec7b3c2a9d8b0c932fed05b59197790d80be0e38fb47ad53d5bf5a634557363cdf6f7cf8a2"

app = Flask(__name__)
app.secret_key = os.environ["ADMIN_SECRET_KEY"]
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
ADMIN_API_TOKEN = os.environ["ADMIN_API_TOKEN"]


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def logged_in():
    if session.get("user") == USERNAME:
        return True
    token = request.headers.get("X-Admin-Token") or request.args.get("t")
    return token == ADMIN_API_TOKEN


def get_stats():
    conn = db()
    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN summary IS NOT NULL THEN 1 ELSE 0 END) AS scored,
            SUM(CASE WHEN summary IS NULL AND score_failed = 0 THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN score_failed = 1 THEN 1 ELSE 0 END) AS failed
        FROM policies
    """).fetchone()

    by_source = conn.execute("""
        SELECT source, country,
            COUNT(*) AS total,
            SUM(CASE WHEN summary IS NOT NULL THEN 1 ELSE 0 END) AS scored
        FROM policies GROUP BY source, country ORDER BY total DESC
    """).fetchall()

    by_version = conn.execute("""
        SELECT COALESCE(scorer_version,'—') AS ver, COUNT(*) AS n
        FROM policies WHERE summary IS NOT NULL
        GROUP BY scorer_version ORDER BY ver
    """).fetchall()

    recent = conn.execute("""
        SELECT title, source, country, social_score, environmental_score,
               economic_score, scorer_version, scored_at
        FROM policies WHERE summary IS NOT NULL AND scored_at IS NOT NULL
        ORDER BY scored_at DESC LIMIT 20
    """).fetchall()

    dist = conn.execute("""
        SELECT social_score, environmental_score, economic_score,
               human_rights_score, governance_score FROM policies
        WHERE summary IS NOT NULL
          AND social_score IS NOT NULL
    """).fetchall()

    by_provider = conn.execute("""
        SELECT COALESCE(scored_by, 'unknown') AS provider, COUNT(*) AS n
        FROM policies WHERE summary IS NOT NULL
        GROUP BY scored_by ORDER BY n DESC
    """).fetchall()

    buckets_48h = conn.execute("""
        SELECT
          strftime('%Y-%m-%dT%H:', scored_at) ||
          printf('%02d', (CAST(strftime('%M', scored_at) AS INTEGER) / 10) * 10) AS bucket,
          COUNT(*) AS n
        FROM policies
        WHERE summary IS NOT NULL
          AND datetime(scored_at) >= datetime('now', '-48 hours')
        GROUP BY bucket
        ORDER BY bucket
    """).fetchall()

    # Items added per day per source (last 30 days)
    daily_ingest = conn.execute("""
        SELECT fetched_date AS day, source, COUNT(*) AS n
        FROM policies
        WHERE fetched_date >= date('now', '-30 days')
        GROUP BY fetched_date, source
        ORDER BY fetched_date
    """).fetchall()

    # per-minute rate: items scored in the last 5 minutes
    rate = conn.execute("""
        SELECT COUNT(*) FROM policies
        WHERE datetime(scored_at) >= datetime('now', '-5 minutes')
    """).fetchone()[0]

    by_country = conn.execute("""
        SELECT country,
            COUNT(*) AS total,
            SUM(CASE WHEN summary IS NOT NULL THEN 1 ELSE 0 END) AS scored
        FROM policies GROUP BY country ORDER BY total DESC
    """).fetchall()

    conn.close()

    social_dist = {str(i): 0 for i in range(-10, 11)}
    env_dist    = {str(i): 0 for i in range(-10, 11)}
    eco_dist    = {str(i): 0 for i in range(-10, 11)}
    hr_dist     = {str(i): 0 for i in range(-10, 11)}
    gov_dist    = {str(i): 0 for i in range(-10, 11)}
    for r in dist:
        for bucket, key in (
            (social_dist, "social_score"), (env_dist, "environmental_score"),
            (eco_dist, "economic_score"), (hr_dist, "human_rights_score"),
            (gov_dist, "governance_score"),
        ):
            k = str(r[key])
            if k in bucket:
                bucket[k] += 1

    # Check if scoring process is active
    try:
        result = subprocess.run(["pgrep", "-f", "score_new.py|rescore_v3.py|eu_backfill.py|pipeline.py"],
                                capture_output=True, text=True)
        scoring_active = bool(result.stdout.strip())
    except Exception:
        scoring_active = False

    try:
        bf = subprocess.run(["pgrep", "-f", "de_backfill.py|fr_backfill.py"],
                            capture_output=True, text=True)
        backfill_workers = len([l for l in bf.stdout.strip().split("\n") if l])
    except Exception:
        backfill_workers = 0

    result = {
        "total": row["total"], "scored": row["scored"],
        "pending": row["pending"], "failed": row["failed"],
        "pct": round(100 * row["scored"] / row["total"], 1) if row["total"] else 0,
        "scoring_active": scoring_active,
        "by_country": [dict(r) for r in by_country],
        "backfill_workers": backfill_workers,
        "by_source": [dict(r) for r in by_source],
        "by_version": [dict(r) for r in by_version],
        "by_provider": [dict(r) for r in by_provider],
        "buckets_48h": [dict(r) for r in buckets_48h],
        "daily_ingest": [dict(r) for r in daily_ingest],
        "recent": [dict(r) for r in recent],
        "social_dist": social_dist,
        "env_dist": env_dist,
        "eco_dist": eco_dist,
        "hr_dist": hr_dist,
        "gov_dist": gov_dist,
        "rate_5min": rate,
    }
    result.update(get_deep_stats())
    return result


_deep_cache: dict = {"data": None, "at": 0.0}

def get_deep_stats() -> dict:
    global _deep_cache
    if _deep_cache["data"] is not None and time.time() - _deep_cache["at"] < 600:
        return _deep_cache["data"]
    conn = db()

    score_drift = conn.execute("""
        SELECT strftime('%Y', published_date) AS year,
               ROUND(AVG(social_score), 2)         AS social,
               ROUND(AVG(environmental_score), 2)  AS env,
               ROUND(AVG(economic_score), 2)        AS econ,
               ROUND(AVG(human_rights_score), 2)   AS hr,
               ROUND(AVG(governance_score), 2)      AS gov,
               COUNT(*) AS n
        FROM policies
        WHERE published_date IS NOT NULL
          AND scorer_version IS NOT NULL
          AND strftime('%Y', published_date) >= '2007'
        GROUP BY year ORDER BY year
    """).fetchall()

    country_cmp = conn.execute("""
        SELECT country, COUNT(*) AS total,
               ROUND(AVG(social_score), 2)         AS social,
               ROUND(AVG(environmental_score), 2)  AS env,
               ROUND(AVG(economic_score), 2)        AS econ,
               ROUND(AVG(human_rights_score), 2)   AS hr,
               ROUND(AVG(governance_score), 2)      AS gov
        FROM policies WHERE scorer_version IS NOT NULL
        GROUP BY country ORDER BY total DESC
    """).fetchall()

    status_cmp = conn.execute("""
        SELECT status, COUNT(*) AS total,
               ROUND(AVG(social_score), 2)         AS social,
               ROUND(AVG(environmental_score), 2)  AS env,
               ROUND(AVG(economic_score), 2)        AS econ,
               ROUND(AVG(human_rights_score), 2)   AS hr,
               ROUND(AVG(governance_score), 2)      AS gov
        FROM policies WHERE scorer_version IS NOT NULL AND status IS NOT NULL
        GROUP BY status ORDER BY total DESC
    """).fetchall()

    top_tags = conn.execute("""
        SELECT je.value AS tag, COUNT(*) AS n,
               ROUND(AVG(ABS(p.social_score + p.environmental_score + p.economic_score +
                             p.human_rights_score + p.governance_score) / 5.0), 2) AS avg_abs,
               ROUND(AVG((p.social_score + p.environmental_score + p.economic_score +
                          p.human_rights_score + p.governance_score) / 5.0), 2) AS avg_signed
        FROM policies p, json_each(p.tags) je
        WHERE p.scorer_version IS NOT NULL AND p.tags IS NOT NULL AND p.tags != '[]'
        GROUP BY je.value HAVING n >= 10
        ORDER BY avg_abs DESC LIMIT 20
    """).fetchall()

    best_month = conn.execute("""
        SELECT strftime('%Y-%m', published_date) AS month,
               ROUND(AVG((social_score + environmental_score + economic_score +
                          human_rights_score + governance_score) / 5.0), 2) AS avg_score,
               COUNT(*) AS n
        FROM policies
        WHERE scorer_version IS NOT NULL AND published_date IS NOT NULL
        GROUP BY month HAVING n >= 20 ORDER BY avg_score DESC LIMIT 1
    """).fetchone()

    worst_month = conn.execute("""
        SELECT strftime('%Y-%m', published_date) AS month,
               ROUND(AVG((social_score + environmental_score + economic_score +
                          human_rights_score + governance_score) / 5.0), 2) AS avg_score,
               COUNT(*) AS n
        FROM policies
        WHERE scorer_version IS NOT NULL AND published_date IS NOT NULL
        GROUP BY month HAVING n >= 20 ORDER BY avg_score ASC LIMIT 1
    """).fetchone()

    conn.close()
    result = {
        "score_drift":  [dict(r) for r in score_drift],
        "country_cmp":  [dict(r) for r in country_cmp],
        "status_cmp":   [dict(r) for r in status_cmp],
        "top_tags":     [dict(r) for r in top_tags],
        "best_month":   dict(best_month)  if best_month  else None,
        "worst_month":  dict(worst_month) if worst_month else None,
    }
    _deep_cache = {"data": result, "at": time.time()}
    return result


def tail_log(lines: int = 80) -> str:
    for path in [BASE_DIR / "scoring.log", BASE_DIR / "pipeline.log", BASE_DIR / "eu_bulk2.log"]:
        if path.exists():
            try:
                r = subprocess.run(["tail", f"-{lines}", str(path)],
                                   capture_output=True, text=True, timeout=5)
                return r.stdout
            except Exception as e:
                return f"(error reading log: {e})"
    return "(no log found)"


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CivilGate Admin — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e18;color:#e5e7eb;font-family:'JetBrains Mono',monospace;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:40px;width:320px}
h1{font-size:13px;letter-spacing:2px;text-transform:uppercase;color:#7F77DD;margin-bottom:28px}
label{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#4b5563;display:block;margin-bottom:6px}
input{width:100%;background:#0a0e18;border:1px solid #1f2937;border-radius:6px;color:#e5e7eb;
      font-family:inherit;font-size:13px;padding:10px 12px;margin-bottom:16px;outline:none}
input:focus{border-color:#7F77DD}
button{width:100%;background:#7F77DD;border:none;border-radius:6px;color:#fff;
       font-family:inherit;font-size:12px;letter-spacing:1px;padding:11px;cursor:pointer;margin-top:4px}
button:hover{background:#6b66cc}
.err{color:#f87171;font-size:11px;margin-bottom:14px}
</style>
</head>
<body>
<div class="box">
  <h1>&#9632; CivilGate admin</h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="post">
    <label>Username</label>
    <input name="username" type="text" autocomplete="username" autofocus>
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""

MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CivilGate — Backfill Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e18;color:#e5e7eb;font-family:'JetBrains Mono',monospace;font-size:13px;padding:24px}
header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:24px}
h1{font-size:16px;font-weight:500;letter-spacing:2px;text-transform:uppercase;color:#7F77DD}
.logout{font-size:10px;color:#4b5563;text-decoration:none}
.logout:hover{color:#e5e7eb}
h2{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#4b5563;margin:24px 0 10px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.card{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px}
.card .num{font-size:36px;font-weight:500;line-height:1;margin-bottom:4px}
.card .label{font-size:10px;color:#4b5563;text-transform:uppercase;letter-spacing:1px}
.scored .num{color:#1D9E75}.pending .num{color:#EF9F27}
.failed .num{color:#f87171}.total .num{color:#7F77DD}
.progress-bar{background:#1f2937;border-radius:4px;height:8px;margin-bottom:8px}
.progress-fill{background:#1D9E75;border-radius:4px;height:8px;transition:width .4s}
.meta{display:flex;gap:20px;font-size:11px;color:#4b5563;margin-bottom:20px}
.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
.active{background:#1D9E75;animation:pulse 1.5s infinite}
.idle{background:#4b5563}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
table{width:100%;border-collapse:collapse;margin-bottom:24px}
th{text-align:left;color:#4b5563;font-size:10px;letter-spacing:1px;text-transform:uppercase;
   padding:6px 8px;border-bottom:1px solid #1f2937}
td{padding:6px 8px;border-bottom:1px solid #111827;color:#9ca3af;font-size:11px}
td.bright{color:#e5e7eb}
td.pos{color:#1D9E75}td.neg{color:#f87171}
.badge{display:inline-block;font-size:9px;padding:2px 6px;border-radius:3px}
.badge-US{background:#633806;color:#FAC775}.badge-EU{background:#085041;color:#9FE1CB}
.badge-GB{background:#0C447C;color:#B5D4F4}.badge-CA{background:#3b2a00;color:#fcd34d}
.badge-AU{background:#2d1a4a;color:#c4b5fd}.badge-RO{background:#4a1a1a;color:#fca5a5}
.dist{display:flex;flex-direction:column;gap:2px;margin-bottom:24px}
.bar-row{display:flex;align-items:center;gap:8px}
.bar-label{width:28px;text-align:right;color:#4b5563;font-size:10px}
.bar{background:#7F77DD;height:10px;border-radius:2px;min-width:1px}
.bar-gemini{background:#1D9E75}.bar-groq{background:#EF9F27}
.bar-deepseek{background:#378ADD}.bar-mistral{background:#D85A30}.bar-unknown{background:#4b5563}
.bar-count{font-size:10px;color:#6b7280}
.log{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;
     white-space:pre;overflow-x:auto;font-size:11px;color:#6b7280;
     max-height:420px;overflow-y:auto;line-height:1.6}
</style>
</head>
<body>
<header>
  <h1>&#9632; CivilGate backfill</h1>
  <a class="logout" href="/logout">sign out</a>
</header>
<div style="position:fixed;bottom:8px;right:12px;font-size:10px;color:#4b5563">v20</div>

<div class="grid" id="cards"></div>
<div class="progress-bar"><div class="progress-fill" id="bar"></div></div>
<div class="meta">
  <span id="pct-text"></span>
  <span id="scoring-status"></span>
  <span id="updated"></span>
</div>

<h2>Backfill workers</h2>
<div class="meta" style="margin-bottom:6px"><span id="backfill-status"></span></div>

<h2>By country</h2>
<div id="country-bars" style="margin-bottom:24px"></div>

<h2>Scoring rate — last 48 h (10-min buckets)</h2>
<div id="rate-chart" style="margin-bottom:24px"></div>

<h2>Items added per day (last 30 days)
  <span style="font-size:11px;font-weight:400;margin-left:12px">
    <button id="ingest-btn-total" onclick="renderIngest('total')" style="background:#1f2937;border:1px solid #374151;color:#e5e7eb;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;font-family:monospace">Totals</button>
    <button id="ingest-btn-sources" onclick="renderIngest('sources')" style="background:#111827;border:1px solid #1f2937;color:#4b5563;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;font-family:monospace">By source</button>
  </span>
</h2>
<div id="ingest-chart" style="margin-bottom:24px"></div>
<div id="ingest-legend" style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px;font-size:10px;color:#9ca3af"></div>

<h2>Statistics</h2>

<div style="background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:16px">
  <div style="font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#4b5563;margin-bottom:10px">SCORE DRIFT — GLOBAL AVERAGE BY YEAR</div>
  <div id="drift-chart"></div>
  <div id="drift-legend" style="display:flex;flex-wrap:wrap;gap:16px;margin-top:8px;font-size:10px;color:#9ca3af"></div>
</div>

<div style="background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:16px">
  <div style="font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#4b5563;margin-bottom:10px">COUNTRY COMPARISON — AVERAGE SCORES BY DIMENSION</div>
  <table id="country-cmp-table">
    <thead><tr><th>Country</th><th>Count</th><th>Social</th><th>Env</th><th>Econ</th><th>Rights</th><th>Gov</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div style="background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:16px">
  <div style="font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#4b5563;margin-bottom:10px">DO PROPOSED BILLS SCORE HIGHER THAN ENACTED LAWS?</div>
  <table id="status-cmp-table">
    <thead><tr><th>Status</th><th>Count</th><th>Social</th><th>Env</th><th>Econ</th><th>Rights</th><th>Gov</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div style="background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:16px">
  <div style="font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#4b5563;margin-bottom:10px">HIGHEST IMPACT POLICY AREAS</div>
  <table id="tags-table">
    <thead><tr><th>Tag</th><th>Count</th><th>Avg Impact</th><th>Lean</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div style="background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:24px">
  <div style="font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#4b5563;margin-bottom:10px">PEAK AND TROUGH MONTHS</div>
  <div id="months-chips" style="display:flex;gap:20px;flex-wrap:wrap;margin-top:4px"></div>
</div>

<h2>By source</h2>
<table id="sources-table">
  <thead><tr><th>Source</th><th>Country</th><th>Total</th><th>Scored</th><th>Remaining</th><th>%</th></tr></thead>
  <tbody></tbody>
</table>

<h2>By scorer version</h2>
<table id="ver-table">
  <thead><tr><th>Version</th><th>Count</th></tr></thead>
  <tbody></tbody>
</table>

<h2>LLM provider usage</h2>
<div class="dist" id="provider-dist"></div>

<h2>Score distribution</h2>
<div style="display:flex;gap:2rem;flex-wrap:wrap">
  <div>
    <div style="font-size:11px;color:#1D9E75;margin-bottom:.4rem;font-family:monospace">SOCIAL</div>
    <div class="dist" id="dist-social"></div>
  </div>
  <div>
    <div style="font-size:11px;color:#38bdf8;margin-bottom:.4rem;font-family:monospace">ENVIRONMENTAL</div>
    <div class="dist" id="dist-env"></div>
  </div>
  <div>
    <div style="font-size:11px;color:#EF9F27;margin-bottom:.4rem;font-family:monospace">ECONOMIC</div>
    <div class="dist" id="dist-eco"></div>
  </div>
  <div>
    <div style="font-size:11px;color:#818cf8;margin-bottom:.4rem;font-family:monospace">HUMAN RIGHTS</div>
    <div class="dist" id="dist-hr"></div>
  </div>
  <div>
    <div style="font-size:11px;color:#94a3b8;margin-bottom:.4rem;font-family:monospace">GOVERNANCE</div>
    <div class="dist" id="dist-gov"></div>
  </div>
</div>

<h2>Recently scored (last 20)</h2>
<table id="recent-table">
  <thead><tr><th>Title</th><th>Source</th><th>Soc</th><th>Env</th><th>Econ</th><th>Ver</th><th>Scored at</th></tr></thead>
  <tbody></tbody>
</table>

<h2>Scoring log</h2>
<div class="log" id="log">Loading…</div>

<script>
const _STATS = "__STATS_JSON__";
const _LOG   = "__LOG_TEXT__";

var _ingestRaw  = [];
var _ingestMode = 'total';

function renderIngest(mode) {
  if (mode !== undefined) _ingestMode = mode;
  var btnT = document.getElementById('ingest-btn-total');
  var btnS = document.getElementById('ingest-btn-sources');
  if (btnT) {
    var on  = 'background:#1f2937;border:1px solid #374151;color:#e5e7eb';
    var off = 'background:#111827;border:1px solid #1f2937;color:#4b5563';
    btnT.style.cssText = ((_ingestMode==='total') ? on : off) + ';padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;font-family:monospace';
    btnS.style.cssText = ((_ingestMode==='sources') ? on : off) + ';padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;font-family:monospace';
  }
  var raw = _ingestRaw;
  if (!raw.length) { document.getElementById('ingest-chart').textContent = 'No data yet'; return; }
  var daySet = new Set(), srcSet = new Set();
  raw.forEach(function(r){ daySet.add(r.day); srcSet.add(r.source); });
  var days = [...daySet].sort();
  var sources = [...srcSet].sort();
  var palette = ['#7F77DD','#1D9E75','#EF9F27','#378ADD','#D85A30','#5DCAA5','#D4537E','#E24B4A','#F59E0B','#EC4899','#8B5CF6','#10B981','#06B6D4','#84CC16','#F97316','#6366F1','#14B8A6'];
  var srcColor = Object.fromEntries(sources.map(function(s,i){ return [s, palette[i % palette.length]]; }));
  var lookup = {};
  raw.forEach(function(r){ (lookup[r.day] = lookup[r.day] || {})[r.source] = r.n; });
  var totals = days.map(function(d){ return sources.reduce(function(s,src){ return s + (lookup[d] && lookup[d][src] ? lookup[d][src] : 0); }, 0); });
  var maxVal = _ingestMode === 'total' ? Math.max.apply(null, totals.concat([1])) : Math.max.apply(null, raw.map(function(r){ return r.n; }).concat([1]));
  var W = document.getElementById('ingest-chart').clientWidth || 900;
  var H = 160, pad = {t:12, r:8, b:28, l:48};
  var cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
  var xOf = function(i){ return pad.l + i * (cw / Math.max(days.length - 1, 1)); };
  var yOf = function(v){ return pad.t + ch - (v / maxVal) * ch; };
  var grid = [0, 0.25, 0.5, 0.75, 1].map(function(f){
    var v = Math.round(maxVal * f), y = yOf(v).toFixed(1);
    return '<line x1="' + pad.l + '" y1="' + y + '" x2="' + (pad.l+cw) + '" y2="' + y + '" stroke="#1f2937" stroke-width="1"/>'
         + '<text x="' + (pad.l-4) + '" y="' + y + '" fill="#4b5563" font-size="9" text-anchor="end" dominant-baseline="middle">' + v.toLocaleString() + '</text>';
  }).join('');
  var areas = '', lines = '';
  if (_ingestMode === 'total') {
    var pts = days.map(function(d,i){ return [xOf(i), yOf(totals[i])]; });
    var poly = pts.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
    var apath = 'M' + xOf(0).toFixed(1) + ',' + (pad.t+ch) + ' ' + pts.map(function(p){ return 'L' + p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ') + ' L' + xOf(days.length-1).toFixed(1) + ',' + (pad.t+ch) + ' Z';
    areas = '<path d="' + apath + '" fill="#7F77DD" opacity="0.12"/>';
    lines = '<polyline points="' + poly + '" fill="none" stroke="#7F77DD" stroke-width="2" stroke-linejoin="round"/>';
  } else {
    sources.forEach(function(src){
      var color = srcColor[src];
      var pts = days.map(function(d,i){ return [xOf(i), yOf(lookup[d] && lookup[d][src] ? lookup[d][src] : 0)]; });
      var poly = pts.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
      var apath = 'M' + xOf(0).toFixed(1) + ',' + (pad.t+ch) + ' ' + pts.map(function(p){ return 'L' + p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ') + ' L' + xOf(days.length-1).toFixed(1) + ',' + (pad.t+ch) + ' Z';
      areas += '<path d="' + apath + '" fill="' + color + '" opacity="0.07"/>';
      lines += '<polyline points="' + poly + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round"/>';
    });
  }
  var slotW = cw / Math.max(days.length, 1);
  var hovers = days.map(function(day, i){
    var tip = _ingestMode === 'total'
      ? day + ': ' + totals[i] + ' items'
      : day + ' - ' + sources.filter(function(s){ return lookup[day] && lookup[day][s]; }).map(function(s){ return s + ': ' + lookup[day][s]; }).join(', ') + ' (total: ' + totals[i] + ')';
    return '<rect x="' + (xOf(i) - slotW/2).toFixed(1) + '" y="' + pad.t + '" width="' + slotW.toFixed(1) + '" height="' + ch + '" fill="transparent"><title>' + tip + '</title></rect>';
  }).join('');
  var xLabels = days.map(function(day, i){
    if (i % 7 !== 0 && i !== days.length - 1) return '';
    return '<text x="' + xOf(i).toFixed(1) + '" y="' + (H-6) + '" fill="#4b5563" font-size="9" text-anchor="middle">' + day.slice(5) + '</text>';
  }).join('');
  document.getElementById('ingest-chart').innerHTML =
    '<svg width="100%" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="display:block">'
    + grid + areas + lines + xLabels + hovers + '</svg>';
  if (_ingestMode === 'sources') {
    document.getElementById('ingest-legend').innerHTML = sources.map(function(s){
      return '<span style="display:flex;align-items:center;gap:5px"><span style="width:18px;height:2px;background:' + srcColor[s] + ';display:inline-block;border-radius:1px"></span>' + s + '</span>';
    }).join('');
  } else {
    document.getElementById('ingest-legend').innerHTML = '';
  }
}

function badge(c){return `<span class="badge badge-${c}">${c}</span>`;}
function sc(v){if(v===null||v===undefined||v==='')return'<td>—</td>';
  const n=Number(v);return`<td class="${n>0?'pos':n<0?'neg':''}">${v}</td>`;}

function refresh(stats, log){
  if (stats === undefined) { stats = _STATS; log = _LOG; }

  document.getElementById('cards').innerHTML = `
    <div class="card total"><div class="num">${stats.total.toLocaleString()}</div><div class="label">Total</div></div>
    <div class="card scored"><div class="num">${stats.scored.toLocaleString()}</div><div class="label">Scored</div></div>
    <div class="card pending"><div class="num">${stats.pending.toLocaleString()}</div><div class="label">Pending</div></div>
    <div class="card failed"><div class="num">${stats.failed.toLocaleString()}</div><div class="label">Failed</div></div>
  `;

  document.getElementById('bar').style.width = stats.pct + '%';
  document.getElementById('pct-text').textContent = stats.pct + '% complete (' + stats.scored.toLocaleString() + ' / ' + stats.total.toLocaleString() + ')';

  const active = stats.scoring_active;
  const rateStr = stats.rate_5min > 0 ? ` · ${stats.rate_5min} scored in last 5 min (~${(stats.rate_5min/5).toFixed(1)}/min)` : '';
  document.getElementById('scoring-status').innerHTML =
    `<span class="status-dot ${active?'active':'idle'}"></span>${active ? 'scoring running' + rateStr : 'scoring idle'}`;

  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();

  const bfWorkers = stats.backfill_workers || 0;
  document.getElementById('backfill-status').innerHTML =
    `<span class="status-dot ${bfWorkers > 0 ? 'active' : 'idle'}"></span>` +
    (bfWorkers > 0
      ? `${bfWorkers} backfill worker${bfWorkers > 1 ? 's' : ''} running`
      : 'no backfill running');

  const countryColors = {EU:'#1D9E75',US:'#EF9F27',GB:'#378ADD',CA:'#fcd34d',
                         AU:'#c4b5fd',DE:'#818cf8',FR:'#D85A30'};
  document.getElementById('country-bars').innerHTML = (stats.by_country || []).map(r => {
    const pct = r.total ? Math.round(100 * r.scored / r.total) : 0;
    const color = countryColors[r.country] || '#7F77DD';
    return `<div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;margin-bottom:3px;font-size:11px">
        <span style="color:#e5e7eb;font-weight:500">${r.country}</span>
        <span style="color:#4b5563">${r.scored.toLocaleString()} / ${r.total.toLocaleString()} scored &nbsp;${pct}%</span>
      </div>
      <div class="progress-bar"><div class="progress-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`;
  }).join('');

  document.querySelector('#sources-table tbody').innerHTML = stats.by_source.map(r => {
    const pct = r.total ? Math.round(100*r.scored/r.total) : 0;
    return `<tr><td class="bright">${r.source}</td><td>${badge(r.country)}</td>
      <td>${r.total.toLocaleString()}</td><td class="pos">${r.scored.toLocaleString()}</td>
      <td>${(r.total-r.scored).toLocaleString()}</td><td>${pct}%</td></tr>`;
  }).join('');

  document.querySelector('#ver-table tbody').innerHTML = stats.by_version.map(r =>
    `<tr><td class="bright">${r.ver}</td><td>${r.n.toLocaleString()}</td></tr>`
  ).join('');

  // 48h rate line chart
  (function() {
    const raw = stats.buckets_48h || [];
    const W = document.getElementById('rate-chart').clientWidth || 900;
    const H = 110, pad = {t:8, r:8, b:28, l:36};
    const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;

    // Fill every 10-min slot for the last 48h (288 slots)
    const now = new Date();
    now.setSeconds(0, 0);
    now.setMinutes(Math.floor(now.getMinutes()/10)*10);
    const slots = [];
    for (let i = 287; i >= 0; i--) {
      const d = new Date(now - i * 10 * 60000);
      const key = d.toISOString().slice(0,13) + ':' + String(Math.floor(d.getMinutes()/10)*10).padStart(2,'0');
      slots.push({key, n: 0});
    }
    const lookup = Object.fromEntries(raw.map(r => [r.bucket, r.n]));
    slots.forEach(s => { if (lookup[s.key]) s.n = lookup[s.key]; });

    const maxN = Math.max(...slots.map(s => s.n), 1);
    const xStep = cw / (slots.length - 1);

    const pts = slots.map((s, i) => {
      const x = pad.l + i * xStep;
      const y = pad.t + ch - (s.n / maxN) * ch;
      return [x, y, s];
    });

    // Area fill path
    const linePts = pts.map(([x,y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const areaPath = `M${pad.l},${pad.t+ch} ` +
      pts.map(([x,y]) => `L${x.toFixed(1)},${y.toFixed(1)}`).join(' ') +
      ` L${(pad.l+cw).toFixed(1)},${pad.t+ch} Z`;

    // X-axis labels: every 6h
    const xLabels = [];
    slots.forEach((s, i) => {
      if (s.key.endsWith(':00') && (new Date(s.key.replace('T','') + ':00Z').getUTCHours() % 6 === 0)) {
        const d = new Date(s.key.replace('T',' ') + ':00Z');
        const label = d.toLocaleString([], {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
        xLabels.push(`<text x="${(pad.l + i*xStep).toFixed(1)}" y="${H-6}" fill="#4b5563" font-size="9" text-anchor="middle">${label}</text>`);
      }
    });

    // Y-axis labels
    const yLabels = [0, Math.round(maxN/2), maxN].map(v => {
      const y = pad.t + ch - (v/maxN)*ch;
      return `<text x="${pad.l-4}" y="${y.toFixed(1)}" fill="#4b5563" font-size="9" text-anchor="end" dominant-baseline="middle">${v}</text>`;
    });

    // Invisible hover rects for tooltips
    const hoverRects = pts.map(([x, y, s], i) => {
      const bx = (x - xStep/2).toFixed(1), bw = xStep.toFixed(1);
      return `<rect x="${bx}" y="${pad.t}" width="${bw}" height="${ch}" fill="transparent">
        <title>${s.key.replace('T',' ')}: ${s.n} scored</title></rect>`;
    }).join('');

    document.getElementById('rate-chart').innerHTML = `
    <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="display:block">
      <defs>
        <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#7F77DD" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#7F77DD" stop-opacity="0.03"/>
        </linearGradient>
      </defs>
      <line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${pad.t+ch}" stroke="#1f2937" stroke-width="1"/>
      <line x1="${pad.l}" y1="${pad.t+ch}" x2="${pad.l+cw}" y2="${pad.t+ch}" stroke="#1f2937" stroke-width="1"/>
      ${yLabels.join('')}
      <path d="${areaPath}" fill="url(#areaGrad)"/>
      <polyline points="${linePts}" fill="none" stroke="#7F77DD" stroke-width="1.5" stroke-linejoin="round"/>
      ${xLabels.join('')}
      ${hoverRects}
    </svg>`;
  })();

  // Daily ingestion line chart
  _ingestRaw = stats.daily_ingest || [];
  renderIngest();

  const providerColors = {gemini:'bar-gemini',groq:'bar-groq',deepseek:'bar-deepseek',mistral:'bar-mistral',unknown:'bar-unknown'};
  const maxProv = Math.max(...stats.by_provider.map(r=>r.n), 1);
  document.getElementById('provider-dist').innerHTML = stats.by_provider.map(r => {
    const cls = providerColors[r.provider.toLowerCase()] || 'bar';
    return `<div class="bar-row"><span class="bar-label" style="width:64px;text-align:right">${r.provider}</span>
     <div class="bar ${cls}" style="width:${Math.round(240*r.n/maxProv)}px"></div>
     <span class="bar-count">${r.n.toLocaleString()}</span></div>`;
  }).join('');

  function renderDist(elId, data, color) {
    if (!data) { document.getElementById(elId).innerHTML = '<span style="color:#4b5563;font-size:11px">no data</span>'; return; }
    const entries = Object.entries(data).sort((a, b) => +b[0] - +a[0]);
    const maxBar = Math.max(...entries.map(([k,v]) => v), 1);
    document.getElementById(elId).innerHTML = entries.map(([k,v]) =>
      `<div class="bar-row"><span class="bar-label">${+k>0?'+'+k:k}</span>
       <div class="bar" style="width:${Math.round(150*v/maxBar)}px;background:${color};opacity:${v>0?1:0.15}"></div>
       <span class="bar-count">${v}</span></div>`
    ).join('');
  }
  renderDist('dist-social', stats.social_dist, '#1D9E75');
  renderDist('dist-env',    stats.env_dist,    '#38bdf8');
  renderDist('dist-eco',    stats.eco_dist,    '#EF9F27');
  renderDist('dist-hr',     stats.hr_dist,     '#818cf8');
  renderDist('dist-gov',    stats.gov_dist,    '#94a3b8');

  document.querySelector('#recent-table tbody').innerHTML = stats.recent.map(r =>
    `<tr><td class="bright" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(r.title||'').replace(/"/g,'&quot;')}">${r.title||'—'}</td>
     <td>${badge(r.country)}</td>${sc(r.social_score)}${sc(r.environmental_score)}${sc(r.economic_score)}
     <td>${r.scorer_version||'—'}</td><td>${(r.scored_at||'').slice(0,16)}</td></tr>`
  ).join('');

  // ---- STATISTICS SECTION ----

  // Shared: score cell with green/red tint
  function scCell(v) {
    if (v === null || v === undefined) return '<td style="color:#4b5563">—</td>';
    var n = parseFloat(v);
    if (isNaN(n)) return '<td style="color:#4b5563">—</td>';
    var s;
    if (n >= 1)       s = 'background:rgba(29,158,117,' + Math.min(0.45, n/10*0.4+0.06).toFixed(2) + ');color:#1D9E75';
    else if (n <= -1) s = 'background:rgba(248,113,113,' + Math.min(0.45, Math.abs(n)/10*0.4+0.06).toFixed(2) + ');color:#f87171';
    else              s = 'color:#6b7280';
    return '<td style="' + s + '">' + (n >= 0 ? '+' : '') + n.toFixed(2) + '</td>';
  }

  // Stat 1: Score drift line chart
  ;(function() {
    var raw = stats.score_drift || [];
    var el = document.getElementById('drift-chart');
    if (!el) return;
    if (!raw.length) { el.textContent = 'No data'; return; }
    var dims = [
      {key:'social', label:'Social',  color:'#1D9E75'},
      {key:'env',    label:'Env',     color:'#38bdf8'},
      {key:'econ',   label:'Econ',    color:'#EF9F27'},
      {key:'hr',     label:'Rights',  color:'#818cf8'},
      {key:'gov',    label:'Gov',     color:'#94a3b8'}
    ];
    var years = raw.map(function(r){ return r.year; });
    var allVals = [];
    dims.forEach(function(d){ raw.forEach(function(r){ if (r[d.key] !== null) allVals.push(parseFloat(r[d.key])); }); });
    var yMin = allVals.length ? Math.floor(Math.min.apply(null, allVals.concat([-0.5]))) : -2;
    var yMax = allVals.length ? Math.ceil(Math.max.apply(null,  allVals.concat([0.5])))  : 3;
    var W = el.clientWidth || 900;
    var H = 200, pad = {t:16, r:8, b:28, l:42};
    var cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
    var xOf = function(i){ return pad.l + (years.length > 1 ? i * cw / (years.length - 1) : cw / 2); };
    var yOf = function(v){ return pad.t + ch - ((v - yMin) / (yMax - yMin)) * ch; };
    // Grid ticks
    var range = yMax - yMin;
    var niceStep = range <= 4 ? 1 : range <= 8 ? 2 : 5;
    var gridTicks = [];
    for (var gv = Math.floor(yMin / niceStep) * niceStep; gv <= yMax + niceStep * 0.001; gv = Math.round((gv + niceStep) * 100) / 100) {
      gridTicks.push(gv);
    }
    if (yMin <= 0 && 0 <= yMax && gridTicks.indexOf(0) < 0) { gridTicks.push(0); gridTicks.sort(function(a,b){ return a-b; }); }
    var grid = gridTicks.map(function(gv){
      var gy = yOf(gv).toFixed(1);
      return '<line x1="' + pad.l + '" y1="' + gy + '" x2="' + (pad.l+cw) + '" y2="' + gy
           + '" stroke="' + (gv === 0 ? '#374151' : '#1f2937') + '" stroke-width="' + (gv === 0 ? 2 : 1) + '"/>'
           + '<text x="' + (pad.l-4) + '" y="' + gy + '" fill="' + (gv === 0 ? '#9ca3af' : '#4b5563')
           + '" font-size="9" text-anchor="end" dominant-baseline="middle">' + (gv > 0 ? '+' : '') + gv + '</text>';
    }).join('');
    // Lines + dots
    var linesSvg = dims.map(function(d){
      var pts = [];
      raw.forEach(function(r, i){ if (r[d.key] !== null) pts.push([xOf(i), yOf(parseFloat(r[d.key])), r]); });
      if (!pts.length) return '';
      var poly = pts.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
      var dots = pts.map(function(p){
        var val = parseFloat(p[2][d.key]);
        return '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="3" fill="' + d.color + '">'
          + '<title>' + p[2].year + ' ' + d.label + ': ' + (val >= 0 ? '+' : '') + val.toFixed(2) + ' (n=' + p[2].n.toLocaleString() + ')</title></circle>';
      }).join('');
      return '<polyline points="' + poly + '" fill="none" stroke="' + d.color + '" stroke-width="1.5" stroke-linejoin="round"/>' + dots;
    }).join('');
    // X labels
    var xLabels = years.map(function(yr, i){
      if (years.length > 12 && i % 2 !== 0 && i !== years.length - 1) return '';
      return '<text x="' + xOf(i).toFixed(1) + '" y="' + (H-6) + '" fill="#4b5563" font-size="9" text-anchor="middle">' + yr + '</text>';
    }).join('');
    el.innerHTML = '<svg width="100%" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="display:block">' + grid + linesSvg + xLabels + '</svg>';
    document.getElementById('drift-legend').innerHTML = dims.map(function(d){
      return '<span style="display:flex;align-items:center;gap:5px"><span style="width:18px;height:2px;background:' + d.color + ';display:inline-block;border-radius:1px"></span>' + d.label + '</span>';
    }).join('');
  })();

  // Stat 2: Country comparison table
  ;(function() {
    var tbody = document.querySelector('#country-cmp-table tbody');
    if (!tbody) return;
    tbody.innerHTML = (stats.country_cmp || []).map(function(r){
      return '<tr><td class="bright">' + (r.country || '—') + '</td><td>' + r.total.toLocaleString() + '</td>'
        + scCell(r.social) + scCell(r.env) + scCell(r.econ) + scCell(r.hr) + scCell(r.gov) + '</tr>';
    }).join('');
  })();

  // Stat 3: Score by status
  ;(function() {
    var tbody = document.querySelector('#status-cmp-table tbody');
    if (!tbody) return;
    tbody.innerHTML = (stats.status_cmp || []).map(function(r){
      return '<tr><td class="bright">' + (r.status || '—') + '</td><td>' + r.total.toLocaleString() + '</td>'
        + scCell(r.social) + scCell(r.env) + scCell(r.econ) + scCell(r.hr) + scCell(r.gov) + '</tr>';
    }).join('');
  })();

  // Stat 4: Highest impact tags
  ;(function() {
    var tbody = document.querySelector('#tags-table tbody');
    if (!tbody) return;
    tbody.innerHTML = (stats.top_tags || []).map(function(r){
      var s = parseFloat(r.avg_signed);
      var lean = s > 0.2
        ? '<span style="color:#1D9E75">&#9650; +' + Math.abs(s).toFixed(2) + '</span>'
        : s < -0.2
          ? '<span style="color:#f87171">&#9660; ' + s.toFixed(2) + '</span>'
          : '<span style="color:#4b5563">~ ' + s.toFixed(2) + '</span>';
      return '<tr><td class="bright">' + r.tag + '</td><td>' + r.n + '</td>'
        + '<td>' + parseFloat(r.avg_abs).toFixed(2) + '</td><td>' + lean + '</td></tr>';
    }).join('');
  })();

  // Stat 5: Best and worst months
  ;(function() {
    function chip(label, data, posColor) {
      var inner = data
        ? '<div style="font-size:20px;font-weight:500;color:' + posColor + '">' + data.month + '</div>'
          + '<div style="font-size:11px;color:' + posColor + ';margin-top:4px">avg '
          + (parseFloat(data.avg_score) >= 0 ? '+' : '') + parseFloat(data.avg_score).toFixed(1) + '</div>'
          + '<div style="font-size:10px;color:#4b5563;margin-top:2px">' + parseInt(data.n).toLocaleString() + ' items</div>'
        : '<div style="font-size:20px;font-weight:500;color:#4b5563">—</div>';
      return '<div style="background:#0a0e18;border:1px solid #1f2937;border-radius:8px;padding:14px 20px;min-width:130px">'
        + '<div style="font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:#4b5563;margin-bottom:8px">' + label + '</div>'
        + inner + '</div>';
    }
    var el = document.getElementById('months-chips');
    if (el) el.innerHTML = chip('Best Month', stats.best_month, '#1D9E75') + chip('Worst Month', stats.worst_month, '#f87171');
  })();

  const logEl = document.getElementById('log');
  const atBottom = logEl.scrollHeight - logEl.scrollTop <= logEl.clientHeight + 40;
  logEl.textContent = log;
  if(atBottom) logEl.scrollTop = logEl.scrollHeight;
}

try { refresh(); } catch(e) { document.getElementById('log').textContent = 'JS ERROR: ' + e.message + '\\n' + e.stack; }

async function ajaxRefresh() {
  try {
    const [sr, lr] = await Promise.all([
      fetch('/api/stats', {credentials: 'same-origin'}),
      fetch('/api/log',   {credentials: 'same-origin'}),
    ]);
    if (sr.status === 401) { location.reload(); return; }
    if (!sr.ok) return;
    const [stats, log] = await Promise.all([sr.json(), lr.text()]);
    try { refresh(stats, log); } catch(_) {}
  } catch(_) {}
}
setInterval(ajaxRefresh, 10000);
</script>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    from flask import render_template_string
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == USERNAME and check_password_hash(PASSWORD_HASH, p):
            session.permanent = True
            session["user"] = USERNAME
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    stats     = get_stats()
    log       = tail_log()
    stats_js  = json.dumps(stats).replace("</", "<\\/")
    log_js    = json.dumps(log)          # produces a quoted, escaped JS string literal
    html      = MAIN_HTML.replace('"__STATS_JSON__"', stats_js)
    html      = html.replace('"__LOG_TEXT__"',   log_js)
    return html


@app.route("/api/stats")
def api_stats():
    if not logged_in():
        return jsonify({"error": "unauthorized"}), 401
    resp = jsonify(get_stats())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/log")
def api_log():
    if not logged_in():
        return "unauthorized", 401
    return tail_log(), 200, {"Content-Type": "text/plain", "Cache-Control": "no-store"}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5051, debug=False)
