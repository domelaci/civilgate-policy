# CivilGate — Build Instructions

## Domain & Brand Structure

| Domain | Purpose |
|---|---|
| `civilgate.org` | Main product — the policy watcher (international, English-first) |
| `volunteer.civilgate.org` | Subdomain — volunteer matching platform (migrated from civilkapu.hu) |
| `civilkapu.hu` | Hungarian-language civic platform — will eventually redirect |

**civilgate.org is currently live with the policy watcher. The volunteer matching lives at volunteer.civilgate.org.**

## Product Narrative

CivilGate is a civic platform with two expressions of the same mission:

1. **Understand** — the policy watcher shows what governments are doing, scored and explained in plain language
2. **Act** — the volunteer subdomain lets users find organisations working on the issues they just read about

These two products feed each other. A user reads that their government cut funding for environmental NGOs → they click through to volunteer with an environmental organisation. Policy awareness driving civic action. This narrative should be present in the UI — the policy cards should link to relevant volunteer opportunities where possible.

---

## What We're Building

A public website at civilgate.org that tracks government policy changes across the EU and USA, summarises them in plain language, scores them on social/environmental/economic impact, and surfaces trends over time.

Think of it as a Bloomberg Terminal for democracy — free, public, and readable by anyone regardless of political background.

Target audience: young people who don't follow politics but want to understand what their governments are doing. Design for the least politically engaged user and you automatically include everyone else — journalists, NGOs, researchers, small businesses, teachers.

Two core functions:
1. **Daily feed** — new policies summarised and scored as they come out
2. **Trend analysis** — longitudinal stats showing where governments are moving and how fast (e.g. regulatory volume by topic over time, cross-Atlantic comparisons)

---

## Design Language

The visual reference is holadelej.hu — a Hungarian real-time electricity grid dashboard. Study it before building the frontend. The CivilGate UI should feel like a live data instrument, not a news website.

### Core aesthetic
- **Dark background throughout.** Page bg: `#0a0e18`. Card bg: `#111827`. Never use white or light backgrounds except for text.
- **Data is the hero.** Scores and numbers are large, bold, and immediately readable. No decorative elements. No stock photos. No hero images.
- **Colour carries meaning, not decoration.** Every colour on the page encodes information. Nothing is coloured for style alone.
- **Monospace for data labels.** Use a monospace font (`font-family: 'JetBrains Mono', 'Fira Code', monospace`) for all labels, tags, source badges, dates, and score labels. Use a clean sans-serif (`Inter`, `system-ui`) for body text and summaries.
- **Live feel.** The page should feel like it's connected to something real. Include a "updated X minutes ago" indicator in the header that refreshes. Use subtle animated dots for live status.

### Colour palette

| Role | Hex | Usage |
|---|---|---|
| Page background | `#0a0e18` | Full page bg |
| Card background | `#111827` | Policy cards, map container |
| Card border | `#1f2937` | Subtle card edges |
| Social score | `#1D9E75` | Social impact number and label |
| Environmental score | `#5DCAA5` | Environmental impact number and label |
| Economic score | `#EF9F27` | Economic impact number and label |
| EU region | `#1D9E75` | Map country fill, EU badge bg |
| USA region | `#EF9F27` | Map country fill, USA badge bg |
| UK region | `#378ADD` | Map country fill, UK badge bg |
| Accent / interactive | `#7F77DD` | Hover states, selected filters, links |
| Text primary | `#e5e7eb` | Body text, titles |
| Text secondary | `#9ca3af` | Subtitles, reasons, metadata |
| Text muted | `#4b5563` | Labels, timestamps, tags |

### Typography scale

```css
/* Page title */
font-size: 20px; font-weight: 500; letter-spacing: 2px; font-family: monospace; text-transform: uppercase;

/* Card title */
font-size: 14px; font-weight: 500; color: #e5e7eb; line-height: 1.4;

/* Score number */
font-size: 28px; font-weight: 500; line-height: 1; color: [score colour];

/* Score label */
font-size: 10px; font-family: monospace; text-transform: uppercase; color: #4b5563; letter-spacing: 1px;

/* Score reason */
font-size: 11px; color: #9ca3af; line-height: 1.4; margin-top: 3px;

/* Summary text */
font-size: 13px; color: #9ca3af; line-height: 1.6;

/* Source badge */
font-size: 10px; font-family: monospace; padding: 2px 8px; border-radius: 4px;

/* Timestamp */
font-size: 11px; font-family: monospace; color: #4b5563;
```

### Policy card structure

Each card contains exactly:
1. Top row: source badge (coloured by region) + date (right-aligned, muted monospace)
2. Title (14px, primary text, max 2 lines)
3. Summary (13px, secondary text, 2–3 sentences, plain English)
4. Score row (3 columns, divider above): SOCIAL · ENV · ECON — each with large number, label above, reason below
5. Optional: tag chips at the bottom (monospace, dark bg, muted text)

Card border: `1px solid #1f2937`. Hover: `border-color: #374151`. Border-radius: `12px`. Padding: `1rem 1.25rem`.

### Source badge colours

| Source | Text colour | Background |
|---|---|---|
| EUR-LEX · EU | `#085041` | `#9FE1CB` |
| EC PRESS · EU | `#085041` | `#9FE1CB` |
| EP · EU | `#085041` | `#9FE1CB` |
| FED. REGISTER · USA | `#633806` | `#FAC775` |
| CONGRESS · USA | `#633806` | `#FAC775` |
| UN DIGITAL LIBRARY | `#0C447C` | `#B5D4F4` |

### World map

- Dark muted base for unlit countries: `#1f2937`
- Country borders: `#0a0e18` (same as page bg), `stroke-width: 0.3`
- Lit countries use region colours from palette above
- Unlit countries with planned coverage: show tooltip "coming soon" on hover, briefly highlight in `#2d3748`
- Map container background: `#111827`, top bar with monospace label "SELECT REGION"
- Filter pills: dark bg, monospace font, coloured border + text when active

### Header

```
CIVILGATE          live government data — eu · usa          ● updated 4 min ago    15:42:01
```

- Site name: uppercase monospace, large
- Subtitle: small monospace, muted
- Live indicator: pulsing dot + "updated X min ago", right-aligned
- Clock: real-time, monospace, far right (optional but adds to live feel)
- Navigation: minimal — just text links, no borders or boxes

### What NOT to do
- No white backgrounds anywhere
- No gradients
- No images or illustrations
- No rounded pill buttons with coloured fills — use bordered pills only
- No card shadows — borders only
- No emoji in the UI
- No "Read more" links — summaries must be self-contained
- No pagination — infinite scroll or "load more" button

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

## Data Sources (all free)

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
- Every EU regulation, directive, decision — full text, all 24 languages, back to the 1950s
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

**LegiScan**
- US federal + all 50 state legislatures, structured and consistent
- Has free tier

**UN Digital Library**
- UNGA resolutions back to 1946
- Voting records by country
- Docs: `https://digitallibrary.un.org`

**Member state parliaments** (add incrementally)
- Spain: `https://www.boe.es/datosabiertos/`
- France: `legifrance.gouv.fr`
- Germany: Bundestag open data

---

## Scoring System

Each policy gets three scores (1–10):

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

## LLM Strategy (free)

### Online (preferred — no local compute needed)

| Provider | Model | Free limits | Notes |
|---|---|---|---|
| Google AI Studio | Gemini 2.0 Flash | 60 RPM, 1M token context, no credit card | Primary choice |
| Groq | Llama 3.3 70B | 100K tokens/day, no credit card | Fast, good backup |
| Cerebras | — | 1M tokens/day, no credit card | Best daily volume |
| OpenRouter | 30+ models | 50 req/day free | Useful for variety |

**Strategy:** rotate across Gemini → Groq → Cerebras to multiply free capacity. More than sufficient for 50–200 policy items/day at zero cost.

### Local (fallback)
Server specs: Intel i5-4210U, 11GB RAM, no discrete GPU, Ubuntu 24.04, 118GB free disk.

- Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
- Best models for this hardware: `qwen2.5:3b` or `phi3.5` (fits in RAM, reasonable speed)
- Inference will be slow (~2–3 tok/s) but fine for overnight batch processing
- Run: `ollama run qwen2.5:3b`
- Local API available at `localhost:11434`

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
| Website frontend | Plain HTML/CSS/JS | Free |
| Hosting | Cloudflare Pages | Free |
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
  scored_at TIMESTAMP,
  score_failed INTEGER DEFAULT 0
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
| Default | Dark gray | No coverage planned yet |

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

### Step 1 — Data pipeline ✅ DONE
- Federal Register API fetched and stored in SQLite ✅
- EC Press Corner RSS fetched and stored ✅
- Gemini scoring: summary + 3 scores + reasons + tags ✅
- policies.json exported and served on civilgate.org ✅
- Cron runs daily at 07:00 ✅

### Step 2 — Add more EU sources
1. EUR-Lex RSS
2. European Parliament API
3. Council of the EU RSS
4. Each one follows the same fetch → parse → score → store pattern
5. Once confirmed flowing, EU countries are already lit on the map

### Step 3 — Backfill historical data
1. Federal Register back to 2020 (5 years of trend data)
2. Run as a one-time bulk job overnight
3. This unlocks the trend layer immediately

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

## Phase 2 Feature — Contact Your Representative

When a user reads a policy that affects them, they should be able to immediately see who to contact and how. This connects the "understand" and "act" parts of CivilGate.

### UX Flow

1. Each policy card has a **"Contact your rep"** button at the bottom
2. On first click, a modal asks: "Where are you based?" — text input for address or zip/postcode
3. Location is stored in localStorage so they only enter it once
4. The modal shows their relevant representatives with name, role, phone, and email
5. Below the rep list: a **pre-drafted message** they can copy, referencing the specific policy by name
6. Optional: "Send via email" button that opens their mail client with the message pre-filled

### Data Sources

**USA — Google Civic Information API**
- Free, requires a Google API key (no billing needed for low volume)
- Docs: `https://developers.google.com/civic-information`
- Input: any US address or zip code
- Returns: House representative, two Senators, state legislators, and local officials with office addresses, phone numbers, and emails
- This is the most complete and reliable source — build US support first

**EU — European Parliament MEP Search**
- API: `https://data.europarl.europa.eu/api/v1/meps`
- Filter by country to get MEPs for any EU member state
- Returns: name, country, political group, contact email, official page URL
- For national MPs (Bundestag, Assemblée nationale, etc.) — add per country incrementally, starting with the largest (DE, FR, ES)

### Database Addition

```sql
-- Cache representative lookups to avoid re-hitting APIs
CREATE TABLE rep_cache (
  id INTEGER PRIMARY KEY,
  location_key TEXT UNIQUE,   -- normalised zip/postcode or address hash
  country TEXT,               -- 'US', 'DE', 'FR', etc.
  reps_json TEXT,             -- full API response cached as JSON
  cached_at TIMESTAMP
);
```

Cache rep data for 7 days — it changes rarely and API calls should be minimised.

### Pre-Drafted Message Template

```
Subject: [Policy title] — request for your position

Dear [Representative name],

I am writing as a constituent from [location] regarding [policy title],
published on [date] by [source].

[One-sentence plain-language summary of the policy]

I would like to know your position on this issue and what steps, if any,
you plan to take in response.

Thank you for your time.

[User can add their name here]
```

The message should be editable before copying — it's a starting point, not a form letter.

### Implementation Notes

- Build US support first (Google Civic API is the easiest and most complete)
- EU MEP layer second
- National MPs third — add one country at a time based on traffic/demand
- Do not store user addresses on the server — keep in localStorage only
- The "Contact your rep" button should only appear on cards where the policy's country matches the user's stored location (a US policy doesn't show EU reps and vice versa)
- Show this feature behind a subtle "Act" label in the card footer to distinguish it from the "Read source" link

### Build Order (within Phase 2)

1. Add Google Civic Information API key to pipeline config
2. Build a `/api/reps?address=...` endpoint (or client-side fetch) that calls the Civic API and returns formatted rep data
3. Add the "Contact your rep" button and modal to policy cards — US policies only
4. Add rep cache table to database
5. Add EU MEP lookup for EU policies
6. Add pre-drafted message with copy button
7. Add "mailto:" link as optional convenience

---

## Key Differentiator

Most civic tech shows *what* is happening. This product shows *direction and velocity* — where governments are moving and how fast. The trend layer (e.g. "EU AI regulation up 3x since 2022", "US climate rulemaking collapsed after 2025") is the thing journalists, NGOs, and researchers would cite and share.

---

## What Not to Build Yet
- Mobile app
- Browser extension
- Social media auto-posting
- Anything requiring payment processing until Step 6
- Map colours for regions without live data

---

## Definition of Done for Step 1 ✅

- Federal Register API polled every 24 hours via cron ✅
- EC Press Corner RSS polled every 24 hours via cron ✅
- New items stored in SQLite with raw text ✅
- Each item sent to Gemini → summary + 3 scores + reasons + tags ✅
- Results stored and queryable ✅
- HTML page shows latest policies with scores at civilgate.org ✅

---

## Monetisation Strategy

> Full detail in `MONETISATION.md`. Summary here for session context.

**Core principle:** The data is not the product. Filtering, relevance, and alerts are the product. Free tier builds trust and traffic; paid tiers sell "not having to read the data yourself."

### Tiers
| Tier | Price | What they get |
|---|---|---|
| Free | £0 | Public feed, scores, trend chart, basic filters |
| Freemium | £5–15/mo | Email alerts, saved filters, weekly digest |
| Organisation | £30–80/mo | Multi-user, sector dashboards, PDF snapshots, rep voting history, consultation deadlines |
| Enterprise | £200–500/mo | API access, white-label, custom scoring weights, bulk export |

### Grant funding (parallel track)
- NLnet Foundation, Shuttleworth Foundation, Omidyar Network, Luminate, UK National Lottery Community Fund

### Feature priority order
1. User accounts + saved filters
2. Email alerts (sector/keyword)
3. Weekly digest email
4. Consultation deadline detection
5. MP/rep voting record per constituency
6. PDF snapshot export
7. Organisation multi-user
8. API access tier
9. Custom scoring weights

**Design rule:** Before building any feature, ask: free tier (trust/traffic) or paid tier (saves time/adds relevance)? Never put relevance filtering behind a wall too early — it kills growth.
