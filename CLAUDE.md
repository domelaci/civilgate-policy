# CivilGate — Build Instructions

## Domain & Brand Structure

| Domain | Purpose |
|---|---|
| `civilgate.org` | Main product — the policy watcher (international, English-first) |
| `volunteer.civilgate.org` | Subdomain — volunteer matching platform (to be migrated from civilkapu.hu) |
| `civilkapu.hu` | Hungarian-language civic platform — will eventually point to civilgate.hu or redirect |

**civilgate.org is currently live with volunteer matching content. This needs to be replaced with the policy watcher. The volunteer matching moves to volunteer.civilgate.org.**

## Product Narrative

CivilGate is a civic platform with two expressions of the same mission:

1. **Understand** — the policy watcher shows what governments are doing, scored and explained in plain language
2. **Act** — the volunteer subdomain lets users find organisations working on the issues they just read about

These two products feed each other. A user reads that their government cut funding for environmental NGOs → they click through to volunteer with an environmental organisation. Policy awareness driving civic action. This narrative should be present in the UI — the policy cards should link to relevant volunteer opportunities where possible.

## What You Are Building (Phase 1)

A public website at civilgate.org that tracks government policy changes across the EU and USA, summarises them in plain language, scores them on social/environmental/economic impact, and surfaces trends over time.

Think of it as a Bloomberg Terminal for democracy — free, public, and readable by anyone regardless of political background.

Primary audience: anyone who wants to understand what governments are doing — young people, journalists, NGOs, researchers, small businesses, teachers. Design for the least politically engaged user and you automatically include everyone else.

---

## North Star Principles

- **Plain language always.** If a 20-year-old with no political background can't understand a summary, rewrite it.
- **Show the reasoning.** Every score must have a one-line justification. Never just a number.
- **Free tier must be genuinely good.** The paid tier is only credible if people already trust the free one.
- **Build for the subscription model from day one.** User accounts, data filtering, and API structure should support a paid tier even before it exists.
- **Simplicity compounds.** Don't add a data source until the previous one works well.

---

## Monetisation Model (build toward this)

### Free Tier
- Latest policies summarised and scored
- Basic trend charts
- EU and US federal level only
- No account required

### Paid Tier (design for this from day one, launch later)
- Real-time alerts by topic, keyword, or country
- Full historical trend analysis with data export
- Member state detail (France, Germany, Spain, etc.)
- UN layer
- API access for developers, NGOs, newsrooms
- Custom scoring weights (e.g. user can prioritise environmental score)
- White-label embeds for universities and media outlets

### Who Will Pay
- Researchers and academics (institutional subscriptions)
- Journalists and newsrooms (team subscriptions)
- NGOs monitoring specific policy areas
- Law firms and consultancies tracking regulation for clients

---

## Data Sources

### Priority 1 — Build these first

**US Federal Register**
- URL: `https://www.federalregister.gov/api/v1/`
- No API key needed
- Covers all executive orders, proposed rules, final rules since 1994
- Ideal for trend analysis — 30+ years of data
- Start here. Easiest to get running.

**European Commission Press Corner**
- RSS: `https://ec.europa.eu/commission/presscorner/api/rss`
- Filter by policy area using query params
- No key needed
- Real-time policy announcements

### Priority 2 — Add once Priority 1 works

**Congress.gov API**
- Docs: `https://www.loc.gov/apis/additional-apis/congress-dot-gov-api/`
- Free API key via registration
- Every bill, resolution, amendment, vote, sponsor

**EUR-Lex**
- SPARQL endpoint + RSS + ELI API
- Every EU regulation, directive, decision — full text, all 24 languages
- RSS alerts available per document type
- Docs: `https://eur-lex.europa.eu/content/help/my-eurlex/my-rss-feeds.html`

**European Parliament Open Data Portal**
- URL: `https://data.europarl.europa.eu`
- Free API, no key needed
- Votes, legislative dossiers, committee docs, MEP activity

**Council of the EU**
- RSS: `https://www.consilium.europa.eu/en/about-site/rss/`

### Priority 3 — Add for paid tier / phase 2

**Regulations.gov API**
- Free with API key
- Full US regulatory rulemaking including public comments

**UN Digital Library**
- UNGA resolutions back to 1946
- Voting records by country
- Docs: `https://digitallibrary.un.org`

**LegiScan**
- US federal + all 50 state legislatures
- Consistent structured API
- Has free tier

**Member state parliaments** (add incrementally)
- Spain: `https://www.boe.es/datosabiertos/`
- France: `legifrance.gouv.fr`
- Germany: Bundestag open data

---

## Scoring System

Each policy gets three scores from 1–10:

| Score | What it measures |
|---|---|
| **Social** | Effect on people — rights, welfare, equality, healthcare, education |
| **Environmental** | Effect on climate, nature, emissions, energy, pollution |
| **Economic** | Effect on trade, jobs, markets, public finances, business |

Rules:
- Score must be accompanied by a one-sentence justification
- Positive and negative impacts both score high — a score reflects *magnitude*, not whether it's good or bad
- A trade regulation that massively affects jobs scores high on economic even if the effect is negative
- Scores are generated by LLM prompt — see LLM section below

---

## LLM Strategy

### Primary — Free Online APIs (use these first)

| Provider | Model | Free Limits | Notes |
|---|---|---|---|
| Google AI Studio | Gemini 2.0 Flash | 60 RPM, 1M token context, no credit card | Use as primary |
| Groq | Llama 3.3 70B | 100K tokens/day, no credit card | Use as backup |
| Cerebras | — | 1M tokens/day, no credit card | Best daily volume |

**Strategy:** rotate across Gemini → Groq → Cerebras to multiply free capacity. Sufficient for 50–200 policy items/day at zero cost.

### Fallback — Local (Ubuntu server)
- CPU: Intel i5-4210U, RAM: 11GB, no discrete GPU
- Install: `curl -fsSL https://ollama.com/install.sh | sh`
- Best models for this hardware: `qwen2.5:3b` or `phi3.5`
- Run: `ollama run qwen2.5:3b`
- API available at `localhost:11434`
- Inference is slow (~2–3 tok/s) but fine for overnight batch jobs

### Prompt Structure (use for every policy item)

```
You are a policy analyst. Given the following government policy document, return a JSON object with:
- "summary": a 2-3 sentence plain English summary, written for someone with no political background
- "social_score": integer 1-10 (magnitude of social impact)
- "social_reason": one sentence explaining the social score
- "environmental_score": integer 1-10
- "environmental_reason": one sentence
- "economic_score": integer 1-10
- "economic_reason": one sentence
- "tags": array of up to 5 topic keywords

Document:
{policy_text}
```

---

## Tech Stack

| Layer | Tool | Cost |
|---|---|---|
| Server / pipeline | Python on Ubuntu server | Free |
| Scheduler | Cron | Free |
| Database | SQLite to start, migrate to Supabase free tier when needed | Free |
| LLM calls | Gemini API (primary), Groq (backup) | Free |
| Website frontend | Next.js or plain HTML/CSS | Free |
| Hosting | Vercel free tier | Free |
| User accounts (build now, use later) | Supabase Auth | Free tier |

**Total monthly cost at prototype stage: ~$0**

---

## Database Schema (minimum viable)

```sql
-- Policies table
CREATE TABLE policies (
  id INTEGER PRIMARY KEY,
  source TEXT,              -- 'federal_register', 'eur_lex', 'ec_press', etc.
  country TEXT,             -- 'US', 'EU', 'FR', 'DE', 'UN', etc.
  external_id TEXT,         -- original ID from source API
  title TEXT,
  url TEXT,
  published_date DATE,
  fetched_date DATE,
  raw_text TEXT,
  summary TEXT,
  social_score INTEGER,
  social_reason TEXT,
  environmental_score INTEGER,
  environmental_reason TEXT,
  economic_score INTEGER,
  economic_reason TEXT,
  tags TEXT,                -- JSON array stored as string
  scored_at TIMESTAMP
);

-- Users table (build now, activate later for paid tier)
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE,
  tier TEXT DEFAULT 'free', -- 'free' or 'paid'
  created_at TIMESTAMP
);

-- Alerts table (paid tier feature — create schema now)
CREATE TABLE alerts (
  id INTEGER PRIMARY KEY,
  user_id TEXT,
  keywords TEXT,            -- JSON array
  countries TEXT,           -- JSON array
  active BOOLEAN DEFAULT TRUE
);
```

---

## World Map — Progressive Region Rollout

The map is a live indicator of data coverage. Countries light up as data sources are added. Do not fake coverage — only colour a country when its data is actually flowing into the database.

### Map states
| State | Visual | Meaning |
|---|---|---|
| Active | Coloured (see palette below) | Data source live, policies being ingested |
| Hover (inactive) | Purple highlight + "coming soon" tooltip | Region planned but not yet built |
| Default | Gray | No coverage planned yet |

### Region colour palette (add in this order)
| Region | Colour | ISO codes to highlight |
|---|---|---|
| EU member states | `#1D9E75` (teal) | 40,56,100,191,196,203,208,233,246,250,276,300,348,372,380,428,440,442,470,528,616,620,642,703,705,724,752 |
| USA | `#EF9F27` (amber) | 840 |
| UK | `#378ADD` (blue) | 826 |
| Middle East | `#D85A30` (coral) | 682,784,634,414,48,512,368,364,376,400,422,760,887,818 |
| China | `#E24B4A` (red) | 156 |
| Australia | `#7F77DD` (purple) | 36,554 |
| South America | `#D4537E` (pink) | 76,32,152,170,862,604,218,68,600,858,328,740 |
| UN (global) | Show a globe icon in the filter bar, not a country colour | n/a |

### Filter pills
One pill per active region plus ALL and UN. Pills are hidden until their region goes live — do not show a filter for data that doesn't exist yet. Launch with just: ALL · EU · USA.

---

## Build Order

### Step 1 — Data pipeline (prove the data works)
1. Write a Python script that fetches from the Federal Register API
2. Parse and store raw results in SQLite
3. Pass one item through Gemini API, get back summary + scores
4. Store the result
5. Confirm end-to-end flow works before touching the frontend

### Step 2 — Add EU sources
1. EC Press Corner RSS
2. EUR-Lex RSS
3. European Parliament API
4. Each one follows the same fetch → parse → score → store pattern
5. Once EU sources are live, light up EU countries on the map and add EU filter pill

### Step 3 — Minimal frontend
1. A single page listing the latest 20 policies
2. Each shows: title, source, date, summary, three scores with reasons, tags
3. World map at the top — USA and EU lit up, everything else gray with "coming soon" on hover
4. Filter pills: ALL · EU · USA only to start
5. No accounts yet

### Step 4 — Trend layer
1. Add charts showing policy volume over time by topic
2. Cross-source comparisons (EU vs US on same topic)
3. This is the most shareable content — prioritise good visualisation

### Step 5 — Expand regions (one at a time)
Add each region only when its data source is proven and flowing. Suggested order:
- UK (legislation.gov.uk + Hansard)
- UN (UN Digital Library)
- Australia (AustLII)
- South America (Brazil Chamber of Deputies API first)
- Middle East (UAE official gazette)
- China (NPC — hardest, requires translation layer)

For each new region: add data source → confirm data flowing → light up map → add filter pill.

### Step 6 — Accounts and paid tier
1. Activate Supabase Auth
2. Add alert system (email when keyword triggers)
3. Add API key generation for paid users
4. Add data export (CSV/JSON)

---

## What Not to Build Yet
- Mobile app
- Browser extension
- Social media auto-posting
- Anything requiring payment processing until Step 6
- Map colours for regions without live data

---

## Definition of Done for Step 1

- Federal Register API is being polled every 24 hours via cron
- New items are stored in SQLite with raw text
- Each item has been sent to Gemini and received summary + 3 scores + reasons + tags
- Results are stored and queryable
- One simple HTML page shows the last 20 items with their scores
