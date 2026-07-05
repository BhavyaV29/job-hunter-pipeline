# job-hunter-pipeline — automated job sourcing & outreach

Pulls fresh, relevant **technical** roles (SDE / Backend / ML / Applied-AI,
India + remote by default) into `tracker.csv` so you start each day with a
triaged list instead of doom-scrolling job boards. It ships tuned for an
**entry-level / new-grad** search, but every person-specific knob — seniority,
experience bands, stack keywords, geography, score weights — is a parameter you
set in `sources.yaml` (see **[Tune it to you](#tune-it-to-you-profile)**), with
no code changes.

It reaches the big consumer platforms — **LinkedIn, Indeed, Glassdoor, Naukri,
RemoteOK, Wellfound** — using the **most robust, sustainable, and safe method for
each**: official/aggregator APIs where possible, public *guest* endpoints where
not, and **never** your personal login.

## Tune it to you (`profile:`)

This is a *technical* job hunter, but it is **not hardcoded to one person.** The
knobs that change search-to-search live in a `profile:` block in `sources.yaml`;
omit the block and you get the fresher / new-grad defaults (behaviour unchanged).

```yaml
profile:
  seniority: fresher     # fresher | junior | mid | senior
  # explicit overrides (win over the preset):
  # exp_good_max: 1      # required YOE <= this -> "good"
  # exp_warn_max: 2      # required YOE <= this -> "warn" (else "bad")
  # drop_senior_titles: true
  # max_exp_years: 2     # drop roles requiring more than this at fetch (null = keep all)
  # boost_keywords:   { backend: 5, python: 3, kubernetes: 4 }   # your stack
  # negative_keywords:{ senior: -6, staff: -7 }                  # titles to avoid
  # location_boosts:  { bengaluru: 10, remote: 2 }               # your geography
  # exp_match_adjust: { good: 5, warn: -10, bad: -30, unknown: 0 }
  # remote_boost: 8
  # brand_a_boost: 30
  # brand_b_boost: 15
  # dream_boost: 50
```

| `seniority` | exp "good" ≤ | exp "warn" ≤ | senior titles | max YOE at fetch |
|---|---|---|---|---|
| `fresher` | 1 yr | 2 yr | dropped | 2 |
| `junior`  | 2 yr | 4 yr | dropped | 4 |
| `mid`     | 5 yr | 8 yr | **kept** | none |
| `senior`  | 12 yr | 20 yr | **kept** | none |

The preset drives experience-match classification (`experience.py`), the
senior-title hard-drop (`fresher_filter.py`), and defaults for the score weights
(`score.py`). Any explicit key overrides the preset.

> **For `mid` / `senior`, also relax the fetch-time gates in `filters:`** —
> remove `senior` / `lead` / `staff` from `title_exclude`, add your target
> titles to `title_include`, and raise/clear `max_exp_years`. Those gate roles
> *before* they are scored, so a senior profile left on the default fresher
> `filters` would drop the very roles it wants. Rule of thumb: **`profile:` tunes
> classification + ranking; `filters:` tunes the hard keep/drop at fetch.**

## Run it as a web app (deploy your own — no file editing)

Prefer a dashboard over the terminal? There's a self-hostable FastAPI + HTMX web
app (`server.py`) over the *same* `tracker.csv`, and you configure the whole thing
**in the browser** — no editing `.env` or `sources.yaml`:

- a **visual Settings page** (`/settings`) — paste your API keys, connect a Google
  Sheet, and set your profile (seniority + fine-tuning); everything is saved on
  *your* instance and reloads on restart;
- a **triage dashboard** — filter/sort roles, change stages inline, watch the funnel;
- a **"Run refresh" button** + optional **daily scheduler** that call the same `morning.py`;
- a **read-only demo** (`DEMO_MODE=1`) backed by `tracker.sample.csv` for a public showcase.

```bash
pip install -r requirements-web.txt
uvicorn server:app --reload            # http://localhost:8000
# or: docker compose up --build
```

A fresh instance comes up **live** and prints a one-time `?token=…` link in the
startup logs (that's your admin token, which gates writes). Open it → **Settings**
→ add your keys/Sheet/profile → **Run refresh**. Everyone runs their **own**
instance with their **own** keys — no shared server holds anyone's secrets.
One-click blueprints for **Render** and **Fly.io**, plus a `Dockerfile` /
`docker-compose.yml`, are included — see **[DEPLOY.md](DEPLOY.md)** and the in-app
**`/setup`** page. (Advanced tuning — target companies, source list, hard
filters — still lives in `sources.yaml`.)

## Daily use — one command (Sheet-first)

```bash
cd job-hunter-pipeline
set -a && source .env && set +a          # load API keys + Google Sheets creds

python3 morning.py --force               # ☀ fetch + score + Sheet + outreach drafts
```

**What `morning.py` does (in order):**

1. **Pull** Google Sheet → `tracker.csv` (your edits from yesterday: applied, not_applicable, notes)
2. **Clean** — remove junk listings, geo/salary fails, duplicates; `not_applicable` URLs are blacklisted forever
3. **Fetch** new roles in parallel (Naukri, Hirist, Cutshort, ATS boards, aggregators…)
4. **Filter** — fresher profile (~6 mo exp): drops senior titles, 3+ yr reqs, spam URLs, staffing/contract noise
5. **Dedup** — collapse same company+role+location across different board URLs (different roles at same company kept)
6. **Score** all rows; dream companies + highest fit float to top
7. **Push** Sheet — sorted by `score` descending; `not_applicable` rows hidden from Sheet (kept in CSV for dedup)
8. **Outreach** — find contacts + cold mail + referral drafts (skip with `--no-outreach`)

Then open your Sheet — **top rows = apply these today**. You never need to touch CSV.

### Morning workflow (Sheet only)

| You do in Sheet | What happens next morning |
|-----------------|---------------------------|
| `stage = applied` + `applied_date` | Stays in Sheet for tracking; drops out of `score.py` triage |
| `stage = not_applicable` | Removed from Sheet view; URL never re-fetched |
| `stage = rejected` / `withdrawn` | Kept for records; not in triage |
| Edit `notes`, `resume_variant`, contacts | Pulled back before fetch |

```bash
# Optional flags
python3 morning.py --force        # also run paid APIs (JSearch/Adzuna/SerpApi)
python3 morning.py --no-outreach  # skip contact lookup + email drafts
python3 morning.py --outreach-top 15
python3 morning.py --top 25       # optional terminal preview (default: Sheet only)
```

### Lower-level commands (if you need them)

```bash
uv run --with playwright fetch_jobs.py    # same fetch engine morning.py calls
uv run score.py --top 25                  # re-print ranked queue
uv run fetch_jobs.py --cleanup-junk       # one-time junk purge (also runs during fetch)
uv run sheets_sync.py --pull              # manual Sheet → CSV
uv run sheets_sync.py --push              # manual CSV → Sheet
```

Legacy apply marking (optional — you can set `stage=applied` in the Sheet instead):

```bash
uv run pipeline.py --applied <url>
uv run pipeline.py --mark-top 25
```

This is a **stateful, self-excluding loop**: dedup by URL + `(company, role, location)`.
Roles you've seen or marked `not_applicable` never come back.

## How it works

- `sources.yaml` — your target companies (company + ATS + token), the
  cross-source `aggregators:` block, and the title/location filters. **Edit this.**
- `fetch_jobs.py` — hits every source, keeps roles matching your filters + the
  geo/salary keep-drop rules (see **Remote eligibility** + **Salary floors**),
  dedupes against `tracker.csv` (by URL), appends new ones as `stage=sourced`. It
  also **re-applies the current rules to existing sourced rows on every run**,
  pruning ones that no longer qualify (e.g. foreign-locked remote) while leaving
  any role you've already applied to untouched. Each source is wrapped in
  try/except → a report line; one source failing never crashes the run.
- `score.py` — ranks roles by a **dominant salary/remote tier** (see **Ranking
  tiers**), then by fresher-fit + stack match (Python / Go / backend / k8s / ML /
  agents) within the tier, penalizing seniority and boosting remote + recent finds.
- `pipeline.py` — daily dashboard (`--funnel`, `--due`, `--closing`) **and** the mark-applied
  workflow (`--applied`, `--applied-file`, `--mark-top`) that moves roles out of
  the triage queue.
- `tracker.csv` — append-only, deduped single source of truth (26-column funnel
  `FIELDS` schema with `score`, `stage`, `applied_date`, `salary`, `deadline`,
  `exp_years`, `exp_match`, `resume_variant`, …).
  The column order is **canonical and identical** across `fetch_jobs.py`,
  `score.py`, and `sheets_sync.py` (`score` sits right after `company`); every
  script reads/writes by column **name**, never by positional index.
  The `deadline` column (ISO date `YYYY-MM-DD` or blank) is populated from real
  expiry data where available (JSearch, Adzuna) or a 30-day freshness proxy for
  all other sources — see **Application deadlines** below.

## Platform coverage (source → method → key?)

| Platform / source | How we reach it | Method | Key? | Notes / risk |
|---|---|---|---|---|
| **LinkedIn** | JSearch (Google for Jobs) **+** LinkedIn guest endpoint | aggregator API + public-guest HTML | JSearch: **`RAPIDAPI_KEY`** · guest: none | Two routes; guest can rate-limit/change |
| **Indeed** | JSearch (Google for Jobs) | aggregator API | **`RAPIDAPI_KEY`** | Reliable, ToS-friendly |
| **Glassdoor / ZipRecruiter** | JSearch (Google for Jobs) | aggregator API | **`RAPIDAPI_KEY`** | Comes free with JSearch |
| **Naukri** | jobapi/v3 (Nkparam) → RSS → Playwright intercept → JSearch → SerpApi | public API / RSS / headless browser | none (optional `NAUKRI_NKPARAM`) | Residential IP recommended; see `docs/PROVEN_METHODS.md` |
| **Wellfound (AngelList)** | SerpApi `site:wellfound.com` organic search | aggregator API | **`SERPAPI_KEY`** | Direct fetch is Cloudflare-gated; SerpApi route when key is set |
| **Hirist** | Hirist jobseeker API | public JSON API | none | May 503 from datacenter IPs; skips gracefully |
| **Cutshort** | city __NEXT_DATA__ → Playwright → SerpApi | HTTP + optional browser/API | **`SERPAPI_KEY`** (fallback) | Individual `/job/` URLs; company parsed from slug when API omits it |
| **RemoteOK** | RemoteOK API | aggregator API | none | Remote roles |
| **Remotive** | Remotive API | aggregator API | none | Remote software-dev |
| **The Muse** | The Muse API | aggregator API | none | Entry-level filter |
| **Arbeitnow** | Arbeitnow API | aggregator API | none | EU-heavy + remote |
| **Adzuna** | Adzuna search API | aggregator API | **`ADZUNA_APP_ID`/`KEY`** | Strong India (`in`) coverage |
| **Google Jobs (alt)** | SerpApi | aggregator API | **`SERPAPI_KEY`** | Alternate route to LinkedIn/Indeed |
| **Greenhouse/Lever/Ashby/Workable/Recruitee** | per-company ATS APIs | public API | none | Your `companies:` list |

### How LinkedIn + Indeed are reached (the important part)

- **The reliable backbone is JSearch** (RapidAPI). It aggregates **Google for
  Jobs**, which itself indexes LinkedIn, Indeed, Glassdoor and ZipRecruiter. One
  free key → all of those, legitimately, via a clean JSON API.
- As a **bonus, keyless** route to LinkedIn we also hit LinkedIn's **public
  jobs-guest** endpoint (the same one that serves job cards to logged-out
  visitors) and parse the public HTML. No account, no login.

## Ethics & safety — why no authenticated scraping

This pipeline **never uses your personal LinkedIn / Indeed / Naukri / Wellfound
login or any authenticated session.** That is deliberate:

- **Authenticated scraping is what gets *personal accounts banned*** — and you
  need those accounts to actually apply and network. Not worth the risk.
- So we only ever hit **public / guest endpoints** (no auth) or **official /
  aggregator APIs** that legally index these platforms.
- We're **polite**: realistic browser User-Agent, request timeouts, a small
  delay between paginated requests, and modest result caps.
- Anything fragile (LinkedIn guest, Naukri, Wellfound) **degrades gracefully** —
  if a platform changes shape or rate-limits us, that source reports a friendly
  line and the run continues. The keyed aggregator (JSearch) is the dependable
  fallback for LinkedIn + Indeed.

## Optional API keys (all skip gracefully if unset)

Copy `.env.example` → `.env`, fill in what you have, then load it:

```bash
cp .env.example .env        # then edit .env
set -a; source .env; set +a # load into your shell
uv run fetch_jobs.py
```

Or just `export` the vars directly. **No key is ever hardcoded** — they're read
from the environment, and each source `SkipSource`s with a clear message if its
var is missing (you'll see a `~ <source>: ... skipped` line).

| Var | Source | Where to get it |
|---|---|---|
| `RAPIDAPI_KEY` | **JSearch** (LinkedIn/Indeed/Glassdoor via Google Jobs) | <https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch> → Subscribe (free tier) |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | Adzuna (India + global) | <https://developer.adzuna.com/> → register an app |
| `SERPAPI_KEY` | SerpApi (Google Jobs engine) | <https://serpapi.com/> |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | LLM JD parsing (Gemini) | <https://aistudio.google.com/apikey> |
| `OPENAI_API_KEY` | LLM JD parsing (OpenAI) | <https://platform.openai.com/api-keys> |
| `HUNTER_API_KEY` | Outreach contact lookup | <https://hunter.io/> |

JSearch is the single highest-value sourcing key. For LLM enrichment, set **one** of Gemini/OpenAI/Anthropic (see `.env.example`). Hunter powers the outreach step in `morning.py`.

### Wellfound: SerpApi route (+ manual fallback)

Direct Wellfound fetches are **Cloudflare-gated**. When `SERPAPI_KEY` is set, the
pipeline searches Google with `site:wellfound.com` queries and ingests organic
results. URL dedup prevents double-counting if JSearch/SerpApi Google Jobs also
returns Wellfound links.

**Manual fallback:** create a saved search on `wellfound.com/jobs`
(filter: India / remote, entry-level) and check it directly a couple of times a
week. JSearch also surfaces many of the same startup roles.

### Naukri (jobapi + fallbacks)

Naukri tries routes in order: **jobapi/v3** (fastest when `NAUKRI_NKPARAM` is valid) →
**RSS** → **Playwright intercept** → **JSearch** → **SerpApi**. Playwright requires:

```bash
uv pip install playwright
playwright install chromium
```

When jobapi returns 403/406, the pipeline **auto-refreshes Nkparam** via headed Playwright
(on residential IP) and updates `.env` / `.fetch_state.json`. Manual harvest still works:

```bash
python3 scripts/harvest_nkparam.py   # add NAUKRI_NKPARAM=... to .env
```

Skip Playwright for faster runs when jobapi/RSS already work:

```bash
export NAUKRI_SKIP_PLAYWRIGHT=1
```

Preview junk rows before deleting:

```bash
uv run fetch_jobs.py --cleanup-junk --dry-run
uv run fetch_jobs.py --cleanup-junk          # actually remove sourced junk rows
```

Board-level probes and per-fetcher tests: `docs/PROVEN_METHODS.md`.

### Parallel fetch + LLM enrichment

- **Parallel sources:** `fetch_jobs.py` runs company ATS boards and aggregators concurrently (`asyncio` + `httpx` via `async_fetch.py`). Playwright/Cutshort paths stay sync in a thread pool.
- **LLM JD parsing (optional):** When `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` is set, `llm_jd.py` extracts stack, YOE, remote hints, and a fit summary into `notes` / `exp_*`. Set `LLM_PROVIDER=gemini|openai|anthropic`. Cached in `.jd_cache.json`.
- **Shared geo rules:** India/remote/foreign-lock tokens and tier logic live in `geo.py` (used by `fetch_jobs.py` and `score.py`).

Resume-tuner feasibility: `docs/RESUME_TUNER.md`.

### Tests

```bash
cd job-hunter-pipeline
python3 -m pytest tests/ -q
```

### Preferences filter (optional)

Copy `preferences.yaml.example` → `preferences.yaml`, then set `PREFERENCES_FILTER=1` in `.env`.
Blocklist companies and must-have keywords are applied at fetch time via `llm_filter.py`.

## Outreach (integrated in `morning.py`)

After fetch/score/push, `morning.py` runs `outreach_run.py` unless you pass `--no-outreach`:

1. **`find_contacts.py`** — Hunter.io + email patterns → `../outreach/contacts.csv`
2. **`cold_mail_drafter.py`** — one draft **per role** (same company, different roles = separate drafts)
3. **`referral_drafter.py`** — LinkedIn DM + email per **company** (startup/remote filter)
4. **`outreach_log.py --dashboard`** — follow-ups due + funnel stats

Review before sending — never auto-send. Outputs: `../outreach/cold_mail_drafts.md`, `../outreach/referral_drafts.md`.

Standalone: `python3 outreach_run.py --top 10 --skip-contacts`

### Dream companies

Edit `dream_companies:` in `sources.yaml` — each match gets **+50** in `score.py`.

### Experience matching (`exp_years` / `exp_match`)

Each new role is parsed for years-of-experience requirements from title +
description (`experience.py`). Columns added to `tracker.csv`:

| Column | Values |
|---|---|
| `exp_years` | Minimum years required (e.g. `2`, `3`) or blank |
| `exp_match` | `good` (≤1 yr), `warn` (2 yr), `bad` (≥3 yr), `unknown` |

`score.py` penalizes `bad` (−40), `warn` (−5), boosts `good` (+10). At fetch time,
`drop_exp_bad: true` and `max_exp_years: 2` in `sources.yaml` drop senior/noise
roles before they hit the tracker (`fresher_filter.py`).

`resume_variant` is auto-set: `backend`, `ai_platform`, or `master` (LLM can
override when a Gemini/OpenAI/Anthropic key is set).

### Parallel fetch + Nkparam auto-refresh + LLM JD parse

- **Async parallel fetch** — ATS companies and aggregators run concurrently
  (`async_fetch.py`); typically 3–5× faster daily runs.
- **Nkparam auto-refresh** — Naukri jobapi 403/406 triggers headed Playwright
  harvest; token cached in `.fetch_state.json`. Manual: `python3 scripts/harvest_nkparam.py`.
- **LLM JD enrichment** (optional) — parses stack/YOE/fit, sets `notes` with
  `[fit:NN]` and picks `resume_variant`. Cached in `.jd_cache.json`. Disable with
  `LLM_JD_DISABLE=1`.
- **Shared geo rules** — `geo.py` is the single source for India/remote/salary
  keep-drop logic (used by both `fetch_jobs.py` and `score.py`).

## Source types feeding the pipeline

1. **Per-company ATS APIs** (public, no auth): Greenhouse, Lever, Ashby,
   Workable, Recruitee — keyed off `companies:` in `sources.yaml`.

   | ATS | Endpoint |
   |---|---|
   | Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
   | Lever | `api.lever.co/v0/postings/{token}?mode=json` |
   | Ashby | `api.ashbyhq.com/posting-api/job-board/{token}` |
   | Workable | `apply.workable.com/api/v1/widget/accounts/{token}` |
   | Recruitee | `{token}.recruitee.com/api/offers` |

2. **Aggregator / index APIs + public-guest endpoints** — keyed off
   `aggregators:` in `sources.yaml` (delete the block to fall back to built-in
   defaults). See the platform table above.

## Filtering & scoring

- `filters` in `sources.yaml` keep **entry / new-grad / fresher / junior(eng) /
  associate(eng) / SDE / SDE-1 / Engineer I / MLE / backend / software engineer**
  and exclude **senior / staff / principal / lead / manager / director /
  architect / Engineer II–IV** plus non-engineering noise.
- Locations bias hard toward **India cities + remote/anywhere**, then the geo
  rules below decide keep/drop at fetch time.
- `score.py` ranks by a dominant **tier** (below), then stack match + fresher
  signals + remote/recency boost within the tier. Tune `KEYWORDS` / `NEGATIVE` freely.

### Remote eligibility (India-based candidate)

You're in India, so you can take **India onsite** roles or **remote roles open to
India / globally** — but *not* a remote role locked to a foreign region. At fetch
time each remote role is classified:

- **India-eligible (KEPT)** — its location/title (or description, when available)
  contains `india` / `worldwide` / `anywhere` / `global` / `remote - india`, **or**
  it's a bare `Remote` with no specific foreign-region restriction (benefit of the
  doubt).
- **Foreign-locked (DROPPED)** — it's remote but pinned to a specific non-India
  region with no India/global signal, e.g. `US only` / `United States` / `US-Remote`
  / `Remote (US…` / `EMEA` / `UK only` / `Canada` / `Europe only` / `EU only` /
  `LATAM` / `APAC` (without India) / `Remote - Germany`, etc.

Detection is deliberately **conservative**: a role is dropped only on a *clear*
foreign lock with **no** India/global signal; ambiguous `Remote` is always kept.
The `job_is_remote` flag from JSearch (and the remote flags from Remotive/RemoteOK/
Arbeitnow) improve remote detection where the location string alone is silent.

### Keep / drop rules

| Role | Keep if | Drop if |
|---|---|---|
| Foreign onsite (non-India, non-remote) | — | always |
| Foreign-locked remote | — | always |
| India-eligible **remote** | salary ≥ `remote_floor_lpa` (7) **or unknown** | stated salary < 7 LPA |
| India **onsite** | salary ≥ `min_salary_lpa` (10) **or unknown** | stated salary < 10 LPA |

**Unknown salary is always kept** ("decent company, benefit of the doubt").

### Ranking tiers (`score.py`)

`score.py` sorts by a dominant tier (large score gaps), using the salary stored in
`tracker.csv` and the remote/India classification above. Within a tier, the stack
fit + `REMOTE_BOOST` breaks ties.

| Tier | Meaning | Tier score |
|---|---|---|
| **T1** | India-eligible remote **and** salary ≥ 10 LPA | +1000 |
| **T2** | India onsite **and** salary ≥ 10 LPA | +750 |
| **T3** | India-eligible remote **and** 7 ≤ salary < 10 LPA | +500 |
| **T4** | Unknown salary (kept on benefit-of-the-doubt) | +250 |

The `TIER` and `SALARY` columns appear in the `score.py` table. Thresholds come
from `filters.min_salary_lpa` / `filters.remote_floor_lpa` in `sources.yaml`.

## Salary

A `salary` column (human-readable, e.g. `₹12-18 LPA`, `$120k-150k/yr`, or blank
when unknown) sits right after `location` in `tracker.csv`. It's populated from
the sources that expose pay:

| Source | Salary fields used |
|---|---|
| **JSearch** | `job_min_salary`, `job_max_salary`, `job_salary_currency`, `job_salary_period` (YEAR/MONTH/HOUR) |
| **Adzuna** | `salary_min`, `salary_max` (annual; currency by country — INR for `in`, GBP for `gb`, USD for `us`, …) |
| **Naukri** | the salary placeholder label (e.g. `3-7 Lacs P.A.`, `12 LPA`); `Not disclosed` → blank |
| Muse / Remotive / RemoteOK / Arbeitnow / LinkedIn-guest | none → left blank |

### Salary floors (`min_salary_lpa` + `remote_floor_lpa`)

Two floors in `sources.yaml` drive the keep/drop table above:

- `filters.min_salary_lpa` (default **10** = ₹10,00,000/yr) — India **onsite** floor.
- `filters.remote_floor_lpa` (default **7**) — India-eligible **remote** floor
  (remote pay runs lower but is still worth keeping).

The rule stays deliberately lenient:

- A role is **dropped only if** it advertises a salary we can parse into an
  annual-INR figure **below** its floor.
- **Missing or unparseable salary → KEPT** (no-salary / undisclosed-pay roles are
  intentionally retained — they land in **T4**).
- The MAX of a salary range is used for the comparison (benefit of the doubt).
- Each run prints the keep/drop counts (remote vs onsite, by reason).

### Currency / period normalization (approximate, editable)

To compare non-INR salaries against the floor, `fetch_jobs.py` annualizes by period
(MONTH ×12, HOUR ×~2080 hrs/yr, YEAR ×1) and converts to INR using **approximate,
hand-editable FX constants** near the top of `fetch_jobs.py`:

```python
USD_TO_INR = 83.0
EUR_TO_INR = 90.0
GBP_TO_INR = 105.0   # also CAD_TO_INR, AUD_TO_INR
```

These are rough — bump them when the rupee moves. **Unknown currencies are treated
as unparseable, so the role is kept** (never wrongly dropped). The salary *display*
string always shows the original currency; the INR figure is used only for the
filter.

## Finding a company's token

Open its careers page and grab the last path segment:
`boards.greenhouse.io/stripe` → token `stripe`. If `fetch_jobs.py` reports a
company as "skipped (check token)", the token is wrong or the company moved ATS —
fix it in `sources.yaml` or remove it.

## Application deadlines

### The `deadline` column

Every row in `tracker.csv` carries a `deadline` column (ISO date `YYYY-MM-DD` or blank).

| Source | Deadline value | Type |
|---|---|---|
| **JSearch** | `job_offer_expiration_datetime` field | **Real expiry** |
| **Adzuna** | `expiration_date` field | **Real expiry** |
| **Naukri** | `jobExpiryDate` / `expiryDate` field (if present) | **Real expiry** |
| Naukri (fallback) | `footerPlaceholderLabel` ("X days ago") + 30 days | 30-day proxy |
| Muse / Remotive / RemoteOK / Arbeitnow / LinkedIn-guest / SerpApi | post date + 30 days | **30-day proxy** |
| Greenhouse / Lever / Ashby / Workable / Recruitee (company ATS) | `updated` date + 30 days | **30-day proxy** |

> **Proxy caveat:** The 30-day freshness proxy is a *staleness heuristic*, not a
> guaranteed close date. Postings older than 30 days are *likely* to have closed;
> postings within 30 days are likely still open. Only JSearch and Adzuna deliver
> real deadlines set by the employer.

### Expired-role filtering at fetch time

After a role passes the geo-salary filter, `fetch_jobs.py` checks its deadline.
If the deadline is a parseable date and it is **before today**, the role is dropped
immediately — it never reaches `tracker.csv`. The fetch summary always prints:

```
    N role(s) dropped (expired).
```

This only applies to **new ingest** — existing tracker rows you are already
tracking are **never deleted** automatically (they may still be active; manage
them via `pipeline.py`).

### Urgency boost in `score.py`

T1 and T2 roles (remote ≥ 10 LPA or onsite ≥ 10 LPA) with a **known deadline
within `expiry_warn_days` days** (default 7, configurable in `sources.yaml →
filters → expiry_warn_days`) receive a `+500` urgency boost that floats them
above all other T1/T2 roles in the triage ranking. The `score.py` output marks
these with a visual tag:

```
 1524  T1    Acme Corp         Software Engineer              Remote       $80k-100k/yr  ⚠ EXPIRES: 2026-06-18 (3d)
```

Roles with no known deadline get no boost and stay in normal tier order.

### CLOSING SOON dashboard (`pipeline.py`)

The default `uv run pipeline.py` dashboard always opens with a **CLOSING SOON**
section — all `stage=sourced` roles whose deadline is within `expiry_warn_days`
days, sorted soonest-first with columns: company, role, tier, salary, deadline,
days left. This makes imminent deadlines impossible to miss.

Use `--closing` to show only this section:

```bash
uv run pipeline.py --closing
```

### Configuring the urgency window

Edit `sources.yaml`:

```yaml
filters:
  expiry_warn_days: 7   # days before deadline to trigger boost / CLOSING SOON
```

Both `score.py` and `pipeline.py` read this value (with a fallback default of 7).

## Google Sheets setup (5 min, one-time)

Make Google Sheets the live source of truth for your tracker — update stages,
add notes, and log interview dates from any device (phone, tablet, browser) and
the pipeline picks them up on the next sync.

### Step-by-step

**1.** Go to [https://console.cloud.google.com](https://console.cloud.google.com) → create a new project (or use an existing one).

**2.** Enable two APIs for the project:
  - **Google Sheets API** — search for it under "APIs & Services → Library"
  - **Google Drive API** — same page, search and enable

**3.** Create a service account:
  - IAM & Admin → **Service Accounts** → **Create service account**
  - Name it anything (e.g. `job-tracker`). No roles are required.
  - Click through to **Done**, then open the new service account → **Keys** tab → **Add Key → Create new key → JSON**.
  - Download the JSON key file.

**4.** Save the key file somewhere safe, e.g. `~/.config/job-tracker-sa.json`.
Set in `jobsearch/.env`:
```
GOOGLE_SERVICE_ACCOUNT_JSON=/Users/you/.config/job-tracker-sa.json
```

**5.** Create a new Google Sheet at [https://sheets.google.com](https://sheets.google.com).
Copy the **Sheet ID** from the URL — it is the long string between `/d/` and `/edit`:
```
https://docs.google.com/spreadsheets/d/  <<<YOUR_ID_HERE>>>  /edit
```
Set in `jobsearch/.env`:
```
GOOGLE_SHEETS_ID=your_sheet_id_here
```

**6.** **Share the Sheet with the service account:**
  - Open the Sheet → **Share** (top-right)
  - Paste the service account's `client_email` address (found in the JSON key file)
  - Grant **Editor** access → **Send**

**7.** Upload your current tracker to the Sheet:
```bash
cd job-hunter-pipeline
set -a; source .env; set +a
uv run sheets_sync.py --push
```
Open the Sheet — you should see all your roles with a dark header row and
colour-coded stage column (blue=sourced, yellow=applied, orange=interview,
green=offer, grey=rejected/withdrawn). Deadlines within 7 days appear in red.

**8.** Your daily loop from here:
```bash
uv run fetch_jobs.py          # fetch new roles → auto-syncs to Sheet (--sync)
# ... open the Sheet, update stages, add notes, log interview dates ...
uv run pipeline.py --pull-sheets --due   # pull Sheet edits, then show due items
uv run score.py --top 20                 # triage sourced roles
```

### Manual sync commands

```bash
uv run sheets_sync.py --push    # CSV -> Sheet  (replace Sheet with local CSV)
uv run sheets_sync.py --pull    # Sheet -> CSV  (replace local CSV with Sheet)
uv run sheets_sync.py --sync    # two-way merge (Sheet edits win for user fields)
uv run sheets_sync.py --status  # inspect row counts + schema (no writes)
```

### Sync merge rules (`--sync`, the default daily mode)

| Field category | Who wins | Fields |
|---|---|---|
| **User-edited** (Sheet wins) | You update these in the Sheet | `stage`, `applied_date`, `contact_name`, `contact_email`, `job_id`, `resume_variant`, `referral_contact`, `oa_date`, `phone_date`, `tech_date`, `onsite_date`, `offer_details`, `next_action`, `next_action_date`, `notes` |
| **Pipeline-owned** (CSV wins) | `fetch_jobs.py` / `score.py` set these | `date_found`, `company`, `score`, `role`, `location`, `salary`, `source`, `deadline` |

New roles fetched by the pipeline (CSV-only) are appended to the Sheet.
Rows you add manually in the Sheet (Sheet-only) are kept in both.
`tracker.csv` is backed up to `tracker.csv.bak` before every write.

### Sheet formatting applied automatically

- **Header row**: bold, frozen, dark navy background (`#1a1a2e`), white text.
- **Frozen**: row 1 (header) + column 1 (date_found).
- **Column widths**: company=180, role=250, location=150, salary=120, stage=100, deadline=100, applied_date=110, next_action=200.
- **Stage colour-coding**: sourced=blue, applied=yellow, oa/phone/tech/onsite=orange, offer=green, rejected/withdrawn=grey.
- **Deadline urgency**: deadline within 7 days → red cell; within 3 days → orange row; past deadline → red row.
- **NEW jobs**: `date_found = today` → green row background.
- **Experience mismatch**: `exp_match = bad` → dark red text on row.
- **Row order**: pushed pre-sorted by `score` descending, so the Sheet opens best-first.

### All three manual steps (your checklist)

1. Create service account JSON key (step 3–4 above)
2. Create the Google Sheet and copy its ID (step 5)
3. Share the Sheet with the service account's `client_email` as Editor (step 6)

Once those three are done, run `uv run sheets_sync.py --push` and you're live.

## Tips

- Add the companies from `../outreach/targets.md` here so sourcing + referrals stay aligned.
- Run it every morning; it only ever adds *new* roles you haven't seen.
- Set `RAPIDAPI_KEY` first — it unlocks the LinkedIn/Indeed coverage.
