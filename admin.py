#!/usr/bin/env python3
"""CivilGate backfill admin — port 5051."""
import json, os, secrets, sqlite3, subprocess
from pathlib import Path
from flask import Flask, jsonify, request, session, redirect, url_for
from werkzeug.security import check_password_hash

BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "policies.db"

USERNAME      = "domelaci"
PASSWORD_HASH = "scrypt:32768:8:1$n67wx2JX6GeD7mJu$c5065d74a5aba1d39832701702b7576d7fed0406b871fdff96769aec7b3c2a9d8b0c932fed05b59197790d80be0e38fb47ad53d5bf5a634557363cdf6f7cf8a2"

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def logged_in():
    return session.get("user") == USERNAME


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
        SELECT social_score AS s FROM policies
        WHERE summary IS NOT NULL AND social_score IS NOT NULL
    """).fetchall()

    by_provider = conn.execute("""
        SELECT COALESCE(scored_by, 'unknown') AS provider, COUNT(*) AS n
        FROM policies WHERE summary IS NOT NULL
        GROUP BY scored_by ORDER BY n DESC
    """).fetchall()

    # per-minute rate: items scored in the last 5 minutes
    rate = conn.execute("""
        SELECT COUNT(*) FROM policies
        WHERE scored_at >= datetime('now', '-5 minutes')
    """).fetchone()[0]

    conn.close()

    buckets = {str(i): 0 for i in range(-10, 11)}
    for r in dist:
        k = str(r["s"])
        if k in buckets:
            buckets[k] += 1

    # Check if scoring process is active
    try:
        result = subprocess.run(["pgrep", "-f", "pipeline.py|eu_backfill.py"],
                                capture_output=True, text=True)
        scoring_active = bool(result.stdout.strip())
    except Exception:
        scoring_active = False

    return {
        "total": row["total"], "scored": row["scored"],
        "pending": row["pending"], "failed": row["failed"],
        "pct": round(100 * row["scored"] / row["total"], 1) if row["total"] else 0,
        "scoring_active": scoring_active,
        "by_source": [dict(r) for r in by_source],
        "by_version": [dict(r) for r in by_version],
        "by_provider": [dict(r) for r in by_provider],
        "recent": [dict(r) for r in recent],
        "social_dist": buckets,
        "rate_5min": rate,
    }


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

<div class="grid" id="cards"></div>
<div class="progress-bar"><div class="progress-fill" id="bar"></div></div>
<div class="meta">
  <span id="pct-text"></span>
  <span id="scoring-status"></span>
  <span id="updated"></span>
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

<h2>Social score distribution</h2>
<div class="dist" id="dist"></div>

<h2>Recently scored (last 20)</h2>
<table id="recent-table">
  <thead><tr><th>Title</th><th>Source</th><th>Soc</th><th>Env</th><th>Econ</th><th>Ver</th><th>Scored at</th></tr></thead>
  <tbody></tbody>
</table>

<h2>Scoring log</h2>
<div class="log" id="log">Loading…</div>

<script>
function badge(c){return `<span class="badge badge-${c}">${c}</span>`;}
function sc(v){if(v===null||v===undefined||v==='')return'<td>—</td>';
  const n=Number(v);return`<td class="${n>0?'pos':n<0?'neg':''}">${v}</td>`;}
let prevScored = null;

async function refresh(){
  const [stats, log] = await Promise.all([
    fetch('/api/stats').then(r=>r.json()),
    fetch('/api/log').then(r=>r.text()),
  ]);

  document.getElementById('cards').innerHTML = `
    <div class="card total"><div class="num">${stats.total.toLocaleString()}</div><div class="label">Total</div></div>
    <div class="card scored"><div class="num">${stats.scored.toLocaleString()}</div><div class="label">Scored</div></div>
    <div class="card pending"><div class="num">${stats.pending.toLocaleString()}</div><div class="label">Pending</div></div>
    <div class="card failed"><div class="num">${stats.failed.toLocaleString()}</div><div class="label">Failed</div></div>
  `;

  document.getElementById('bar').style.width = stats.pct + '%';
  document.getElementById('pct-text').textContent = stats.pct + '% complete (' + stats.scored.toLocaleString() + ' / ' + stats.total.toLocaleString() + ')';

  const rate = prevScored !== null ? stats.scored - prevScored : null;
  prevScored = stats.scored;

  const active = stats.scoring_active;
  const rateStr = stats.rate_5min > 0 ? ` · ${stats.rate_5min} scored in last 5 min (~${(stats.rate_5min/5).toFixed(1)}/min)` : '';
  document.getElementById('scoring-status').innerHTML =
    `<span class="status-dot ${active?'active':'idle'}"></span>${active ? 'scoring running' + rateStr : 'scoring idle'}`;

  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();

  document.querySelector('#sources-table tbody').innerHTML = stats.by_source.map(r => {
    const pct = r.total ? Math.round(100*r.scored/r.total) : 0;
    return `<tr><td class="bright">${r.source}</td><td>${badge(r.country)}</td>
      <td>${r.total.toLocaleString()}</td><td class="pos">${r.scored.toLocaleString()}</td>
      <td>${(r.total-r.scored).toLocaleString()}</td><td>${pct}%</td></tr>`;
  }).join('');

  document.querySelector('#ver-table tbody').innerHTML = stats.by_version.map(r =>
    `<tr><td class="bright">${r.ver}</td><td>${r.n.toLocaleString()}</td></tr>`
  ).join('');

  const providerColors = {gemini:'bar-gemini',groq:'bar-groq',deepseek:'bar-deepseek',mistral:'bar-mistral',unknown:'bar-unknown'};
  const maxProv = Math.max(...stats.by_provider.map(r=>r.n), 1);
  document.getElementById('provider-dist').innerHTML = stats.by_provider.map(r => {
    const cls = providerColors[r.provider.toLowerCase()] || 'bar';
    return `<div class="bar-row"><span class="bar-label" style="width:64px;text-align:right">${r.provider}</span>
     <div class="bar ${cls}" style="width:${Math.round(240*r.n/maxProv)}px"></div>
     <span class="bar-count">${r.n.toLocaleString()}</span></div>`;
  }).join('');

  const maxBar = Math.max(...Object.values(stats.social_dist), 1);
  document.getElementById('dist').innerHTML = Object.entries(stats.social_dist).map(([k,v]) =>
    `<div class="bar-row"><span class="bar-label">${+k>0?'+'+k:k}</span>
     <div class="bar" style="width:${Math.round(180*v/maxBar)}px"></div>
     <span class="bar-count">${v}</span></div>`
  ).join('');

  document.querySelector('#recent-table tbody').innerHTML = stats.recent.map(r =>
    `<tr><td class="bright" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(r.title||'').replace(/"/g,'&quot;')}">${r.title||'—'}</td>
     <td>${badge(r.country)}</td>${sc(r.social_score)}${sc(r.environmental_score)}${sc(r.economic_score)}
     <td>${r.scorer_version||'—'}</td><td>${(r.scored_at||'').slice(0,16)}</td></tr>`
  ).join('');

  const logEl = document.getElementById('log');
  const atBottom = logEl.scrollHeight - logEl.scrollTop <= logEl.clientHeight + 40;
  logEl.textContent = log;
  if(atBottom) logEl.scrollTop = logEl.scrollHeight;
}

refresh();
setInterval(refresh, 5000);
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
    return MAIN_HTML


@app.route("/api/stats")
def api_stats():
    if not logged_in():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(get_stats())


@app.route("/api/log")
def api_log():
    if not logged_in():
        return "unauthorized", 401
    return tail_log(), 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5051, debug=False)
