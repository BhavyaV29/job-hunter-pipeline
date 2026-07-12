# job-hunter-pipeline

[![Live demo](https://img.shields.io/badge/live%20demo-online-brightgreen?logo=render&logoColor=white)](https://job-hunter-pipeline.onrender.com) [![Source](https://img.shields.io/badge/source-GitHub-181717?logo=github)](https://github.com/BhavyaV29/job-hunter-pipeline) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Automated job **sourcing, ranking & outreach**. It pulls fresh *technical* roles
(SDE / Backend / ML / Applied-AI, India + remote) from ~15 sources into one ranked
tracker, so you start each day with a triaged shortlist instead of doom-scrolling
job boards.

**▶ Live demo: <https://job-hunter-pipeline.onrender.com>** — read-only sample data, no keys needed.

> Tuned out of the box for an entry-level / new-grad search, but every knob
> (seniority, experience bands, stack keywords, geography, score weights) is a
> parameter you set — no code changes. See **[Tune it to you](#tune-it-to-you)**.

## What it does

- **Sources** roles from official/aggregator APIs + public *guest* endpoints across
  LinkedIn, Indeed, Glassdoor, Naukri, Wellfound, RemoteOK and per-company ATS boards
  (Greenhouse, Lever, Ashby …) — **never** your personal login.
- **Filters** to your profile (seniority, years of experience, location,
  remote-eligibility, salary floors) at fetch time.
- **Dedupes** across boards and **scores** every role into tiers so the best float up.
- **Tracks** each role through stages (sourced → applied → interview → offer) in
  `tracker.csv`, optionally synced to a Google Sheet you can edit from any device.
- **Drafts outreach** (cold email + referral) per role — you review, never auto-send.
- Runs from the **terminal** or a **web dashboard** you configure entirely in the browser.

---

## Two ways to run it

### A) Web app — recommended, no file editing

A self-hostable FastAPI + HTMX dashboard over the same `tracker.csv`. You add API
keys, connect a Sheet, and set your profile from a visual **Settings** page.

```bash
uv sync --locked --extra web
uv run --locked --extra web uvicorn server:app --reload  # http://localhost:8000
# or:  docker compose up --build
```

Then:

1. **Open the URL.** A fresh local/Compose/Fly instance comes up *live* and prints a
   `?token=…` link in the startup logs. In live mode that token gates the
   dashboard, tracker/stats/run APIs, setup/settings metadata, and all writes.
   `/healthz` stays public for hosting probes.
2. **Open that link → Settings** → paste your API keys, optionally connect a Google
   Sheet, and set your profile.
3. **Hit "Run refresh".** Roles fill the dashboard; filter/sort and change stages inline.

Everyone runs their **own** instance with their **own** keys — no shared server
holds anyone's secrets. **Docker Compose** and **Fly.io** mount durable storage.
The included free **Render** blueprint is deliberately a safe read-only demo:
Render's free filesystem is ephemeral, and Docker's `VOLUME` line does not create
a Render disk. For a private Render instance, explicitly switch `DEMO_MODE=0` and
use Render environment secrets + a Google Sheet, or add a paid persistent disk;
browser-saved settings and local `tracker.csv` otherwise reset on restart/redeploy.
See **[DEPLOY.md](DEPLOY.md)** for the no-surprise persistence options.

**Daily auto-refresh:** a bundled GitHub Actions workflow
(`.github/workflows/refresh.yml`) wakes your deployed instance and triggers one
pipeline run per day (02:30 UTC / 08:00 IST) — handy when the host sleeps while idle,
where an in-process timer can't fire. Opt in with the `APP_URL`, `ADMIN_TOKEN`, and
`ENABLE_DAILY_REFRESH` repo variables (see the workflow header); it simply automates
the dashboard's **Run refresh** button. On free Render, use a Google Sheet as the
durable tracker because the local filesystem can be replaced.

> Public showcase? Set `DEMO_MODE=1` to serve read-only sample data — that's exactly
> what the [live demo](https://job-hunter-pipeline.onrender.com) runs.

### B) Terminal — one command a day

```bash
cd job-hunter-pipeline
cp .env.example .env                 # fill in the keys you have (all optional)
set -a && source .env && set +a      # load them into your shell
python3 morning.py --force           # fetch + score + Sheet sync + outreach drafts
```

**What `morning.py` does, in order:**

1. **Pull** your Google Sheet → `tracker.csv` (yesterday's edits: applied, notes …)
2. **Clean** junk / geo / salary fails / duplicates (`not_applicable` URLs blacklisted forever)
3. **Fetch** new roles from all sources in parallel
4. **Filter** to your profile (drops senior titles, high-YOE roles, spam, staffing noise)
5. **Dedupe** same company + role + location across boards
6. **Score** into tiers; dream companies + best fit float to the top
7. **Push** back to the Sheet, sorted best-first
8. **Outreach** — contact lookup + cold-mail / referral drafts (skip with `--no-outreach`)

Open the Sheet — **top rows = apply today**. You never touch the CSV by hand.

Common flags:

```bash
python3 morning.py --no-outreach     # skip the outreach step
python3 morning.py --top 25          # also print a terminal preview
python3 morning.py --outreach-top 15 # limit outreach to the top N
```

---

## Tune it to you

It's a *technical* job hunter, but **not hardcoded to one person.** Person-specific
knobs live in a `profile:` block in `sources.yaml` (or the web **Settings** page).
Omit it and you get the fresher / new-grad defaults.

```yaml
profile:
  seniority: fresher     # fresher | junior | mid | senior
  # optional overrides (win over the preset):
  # max_exp_years: 2
  # boost_keywords:    { backend: 5, python: 3, kubernetes: 4 }   # your stack
  # negative_keywords: { senior: -6, staff: -7 }                  # titles to avoid
  # location_boosts:   { bengaluru: 10, remote: 2 }               # your geography
  # remote_boost: 8
  # dream_boost: 50
```

| `seniority` | exp "good" ≤ | exp "warn" ≤ | senior titles | max YOE at fetch |
|---|---|---|---|---|
| `fresher` | 1 yr | 2 yr | dropped | 2 |
| `junior`  | 2 yr | 4 yr | dropped | 4 |
| `mid`     | 5 yr | 8 yr | **kept** | none |
| `senior`  | 12 yr | 20 yr | **kept** | none |

> **For `mid` / `senior`:** also relax the fetch-time gates in `filters:` — remove
> `senior` / `lead` / `staff` from `title_exclude`, add your target titles to
> `title_include`, and raise/clear `max_exp_years`. Rule of thumb: **`profile:` tunes
> classification + ranking; `filters:` tunes the hard keep/drop at fetch.**

---

## Reference

<details>
<summary><b>Sources &amp; how each platform is reached</b></summary>

| Platform / source | How we reach it | Key? |
|---|---|---|
| **LinkedIn** | JSearch (Google for Jobs) + LinkedIn guest endpoint | `RAPIDAPI_KEY` (guest: none) |
| **Indeed / Glassdoor / ZipRecruiter** | JSearch (Google for Jobs) | `RAPIDAPI_KEY` |
| **Naukri** | jobapi/v3 → RSS → Playwright intercept → JSearch → SerpApi | none (optional `NAUKRI_NKPARAM`) |
| **Wellfound (AngelList)** | SerpApi `site:wellfound.com` search | `SERPAPI_KEY` |
| **Hirist** | jobseeker JSON API | none |
| **Cutshort** | `__NEXT_DATA__` → Playwright → SerpApi | `SERPAPI_KEY` (fallback) |
| **RemoteOK / Remotive / The Muse / Arbeitnow** | public APIs | none |
| **Adzuna** | search API (strong India coverage) | `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` |
| **Greenhouse / Lever / Ashby / Workable / Recruitee** | per-company ATS APIs (your `companies:` list) | none |

**LinkedIn + Indeed (the important part):** the reliable backbone is **JSearch**
(RapidAPI), which aggregates **Google for Jobs** — itself indexing LinkedIn, Indeed,
Glassdoor and ZipRecruiter. One free key covers all of them, legitimately, via a
clean JSON API. As a keyless bonus we also hit LinkedIn's **public jobs-guest**
endpoint (the cards served to logged-out visitors). No account, no login.

Each source is wrapped in try/except → a report line; one source failing never
crashes the run. Anything fragile (LinkedIn guest, Naukri, Wellfound) degrades
gracefully. Board-level probes: `docs/PROVEN_METHODS.md`.
</details>

<details>
<summary><b>API keys (all optional — sources skip gracefully if unset)</b></summary>

Copy `.env.example` → `.env`, fill in what you have, then `set -a; source .env; set +a`.
No key is ever hardcoded; a missing var prints a `~ <source>: … skipped` line.

| Var | Unlocks | Where to get it |
|---|---|---|
| `RAPIDAPI_KEY` | **JSearch** → LinkedIn / Indeed / Glassdoor via Google Jobs | <https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch> (free tier) |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | Adzuna (India + global) | <https://developer.adzuna.com/> |
| `SERPAPI_KEY` | Wellfound + Google Jobs engine | <https://serpapi.com/> |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM JD parsing (set **one**; `LLM_PROVIDER=gemini\|openai\|anthropic`) | <https://aistudio.google.com/apikey> · <https://platform.openai.com/api-keys> |
| `HUNTER_API_KEY` | Outreach contact lookup | <https://hunter.io/> |

**JSearch is the single highest-value key** — it unlocks LinkedIn/Indeed coverage.
</details>

<details>
<summary><b>Filtering, remote-eligibility, scoring &amp; ranking tiers</b></summary>

**Filters** (`sources.yaml`) keep entry / new-grad / SDE-1 / backend / MLE titles
and exclude senior / staff / principal / lead / manager + non-engineering noise.
Locations bias hard toward **India cities + remote/anywhere**.

**Remote eligibility (India-based candidate):** at fetch time each remote role is
classified —

- **India-eligible (KEPT)** — location/title/description mentions
  `india` / `worldwide` / `anywhere` / `global`, **or** it's a bare `Remote` with no
  foreign-region restriction (benefit of the doubt).
- **Foreign-locked (DROPPED)** — remote but pinned to a non-India region with no
  India/global signal (`US only`, `EMEA`, `UK only`, `Remote - Germany`, …).

Detection is conservative: a role is dropped only on a *clear* foreign lock.

**Keep / drop rules:**

| Role | Keep if | Drop if |
|---|---|---|
| Foreign onsite / foreign-locked remote | — | always |
| India-eligible **remote** | salary ≥ `remote_floor_lpa` (7) **or unknown** | stated salary < 7 LPA |
| India **onsite** | salary ≥ `min_salary_lpa` (10) **or unknown** | stated salary < 10 LPA |

**Unknown salary is always kept.**

**Ranking tiers (`score.py`)** — sorts by a dominant tier (large score gaps), then
stack fit + remote boost within the tier:

| Tier | Meaning | Tier score |
|---|---|---|
| **T1** | India-eligible remote **and** ≥ 10 LPA | +1000 |
| **T2** | India onsite **and** ≥ 10 LPA | +750 |
| **T3** | India-eligible remote **and** 7–10 LPA | +500 |
| **T4** | Unknown salary (benefit of the doubt) | +250 |

Dream companies (`dream_companies:` in `sources.yaml`) get **+50**. Shared geo/tier
logic lives in `geo.py` (used by both `fetch_jobs.py` and `score.py`).
</details>

<details>
<summary><b>Experience matching &amp; salary</b></summary>

**Experience** — each role is parsed for YOE from title + description
(`experience.py`), adding two columns:

| Column | Values |
|---|---|
| `exp_years` | Minimum years required (e.g. `2`, `3`) or blank |
| `exp_match` | `good` (≤1 yr), `warn` (2 yr), `bad` (≥3 yr), `unknown` |

`score.py` penalizes `bad` (−40) / `warn` (−5), boosts `good` (+10).
`resume_variant` is auto-set to `backend`, `ai_platform`, or `master` (LLM can
override when a key is set).

**Salary** — a human-readable `salary` column (`₹12-18 LPA`, `$120k-150k/yr`, or
blank) is populated from sources that expose pay (JSearch, Adzuna, Naukri label).
To compare against the floors, non-INR pay is annualized and converted with rough,
hand-editable FX constants near the top of `fetch_jobs.py`. Unknown/unparseable
salary is treated as *keep*.
</details>

<details>
<summary><b>Application deadlines</b></summary>

Every row has a `deadline` column (ISO `YYYY-MM-DD` or blank):

- **Real expiry** from JSearch (`job_offer_expiration_datetime`) and Adzuna
  (`expiration_date`); Naukri when present.
- **30-day freshness proxy** for all other sources (a staleness heuristic, not a
  guaranteed close date).

At fetch time, a role whose deadline is **before today** is dropped and never
reaches `tracker.csv` (existing tracked rows are never auto-deleted). T1/T2 roles
with a known deadline within `expiry_warn_days` (default 7) get a **+500 urgency
boost**, and `pipeline.py` opens with a **CLOSING SOON** section
(`uv run pipeline.py --closing`).
</details>

<details>
<summary><b>Google Sheets setup (5 min, one-time)</b></summary>

Make the Sheet the live source of truth — update stages/notes from any device and
the pipeline picks them up on the next sync. (You can also do this from the web
**Settings** page instead of `.env`.)

1. **Google Cloud Console** → create/select a project; enable the **Google Sheets
   API** and **Google Drive API**.
2. **IAM & Admin → Service Accounts** → create one (no roles needed) → **Keys →
   Add key → JSON** → download it.
3. Save the JSON somewhere safe and set `GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/key.json`.
4. Create a Google Sheet; copy its **ID** (the string between `/d/` and `/edit`) into
   `GOOGLE_SHEETS_ID=…`.
5. **Share** the Sheet with the service account's `client_email` (from the JSON) as **Editor**.
6. `set -a; source .env; set +a && uv run sheets_sync.py --push` — you're live.

**Sync (`--sync`, the daily default):** you own stage/notes/contacts/dates in the
Sheet (Sheet wins); the pipeline owns `company/score/role/location/salary/deadline`
(CSV wins). New pipeline roles are appended; `tracker.csv` is backed up before every
write. Manual: `sheets_sync.py --push | --pull | --sync | --status`.
</details>

<details>
<summary><b>Outreach &amp; lower-level commands</b></summary>

After fetch/score/push, `morning.py` runs `outreach_run.py` (unless `--no-outreach`):

1. `find_contacts.py` — Hunter.io + email patterns → `../outreach/contacts.csv`
2. `cold_mail_drafter.py` — one draft per role
3. `referral_drafter.py` — LinkedIn DM + email per company
4. `outreach_log.py --dashboard` — follow-ups due + funnel stats

Review before sending — **never auto-send.** Standalone:
`python3 outreach_run.py --top 10 --skip-contacts`.

```bash
uv run fetch_jobs.py                 # same fetch engine morning.py calls
uv run score.py --top 25             # re-print the ranked queue
uv run fetch_jobs.py --cleanup-junk  # purge sourced junk rows (--dry-run to preview)
uv run sheets_sync.py --pull         # manual Sheet → CSV
uv run pipeline.py --applied <url>   # mark a role applied (or set stage in the Sheet)
```

**Naukri Playwright fallback** (only if jobapi/RSS fail): `uv pip install playwright
&& playwright install chromium`. Skip it with `export NAUKRI_SKIP_PLAYWRIGHT=1`.
</details>

<details>
<summary><b>Ethics &amp; safety — why no authenticated scraping</b></summary>

This pipeline **never uses your personal LinkedIn / Indeed / Naukri / Wellfound
login or any authenticated session** — that's what gets *personal accounts banned*,
and you need those to actually apply and network.

- We only hit **public / guest endpoints** (no auth) or **official / aggregator
  APIs** that legally index these platforms.
- We're **polite**: realistic User-Agent, timeouts, small delays between paginated
  requests, modest result caps.
- Fragile sources **degrade gracefully**; the keyed aggregator (JSearch) is the
  dependable fallback for LinkedIn + Indeed.
</details>

### Tests

```bash
uv run --locked --extra web --extra dev pytest -q
```

---

**Tips:** run it every morning (it only ever adds *new* roles you haven't seen);
set `RAPIDAPI_KEY` first — it unlocks the LinkedIn/Indeed coverage.
