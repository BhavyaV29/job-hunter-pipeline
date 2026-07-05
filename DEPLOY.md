# Deploy your own instance

This is a **deploy-your-own** tool: everyone runs their *own* copy with their
*own* API keys and their *own* Google Sheet. No shared server ever holds your
secrets, and job-board fetches run from *your* infra — the same safety model as
the CLI.

A deploy comes up **live** and, if you didn't set an `ADMIN_TOKEN`, generates one
and prints a `?token=…` link in the startup logs. Open that link → **Settings** →
paste your API keys, connect a Google Sheet and set your profile — **all in the
browser, no file editing**. (Want a public read-only showcase instead? Set
`DEMO_MODE=1` — it serves `tracker.sample.csv` and blocks writes.)

## Option A — Docker Compose (local or any VPS)

```bash
docker compose up --build     # http://localhost:8000
```

Watch the logs for the `open http://localhost:8000/?token=…` line, open it, and
finish setup on the **Settings** page. The tracker + your saved keys persist in
the `jobdata` volume. (You can also pre-set `ADMIN_TOKEN` / keys via `.env` if you
prefer — copy `.env.example` → `.env` first.)

## Option B — Render (blueprint)

1. Fork this repo.
2. Render → **New → Blueprint** → pick your fork (`render.yaml` is detected).
3. It comes up **live** with a generated `ADMIN_TOKEN` (see it under the service's
   **Environment** tab). Open `https://YOUR-APP.onrender.com/?token=THAT_TOKEN` →
   **Settings** and add your keys/Sheet/profile.

> Free plan sleeps when idle, so `ENABLE_SCHEDULER` isn't reliable there — use a
> paid instance, or hit `POST /run` from an external cron (e.g. GitHub Actions).

## Option C — Fly.io

```bash
fly launch --no-deploy            # accept the bundled fly.toml
fly volumes create jobdata --size 1
fly secrets set ADMIN_TOKEN=$(openssl rand -hex 16)   # optional; else it auto-generates
fly deploy
```

Then open `https://YOUR-APP.fly.dev/?token=YOUR_TOKEN` → **Settings**. For the
daily scheduler on Fly, set `ENABLE_SCHEDULER=1`, `RUN_HOUR`, `TZ`, and keep a
machine warm (`min_machines_running = 1`).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `0` (image) | `0` = live (your tracker); `1` = read-only sample showcase |
| `ADMIN_TOKEN` | _auto-generated_ | Gates writes + "Run refresh" via `?token=` / `X-Admin-Token`. Unset → generated on first boot and printed in the logs (persisted on the data volume) |
| `TRACKER_CSV` | `/data/tracker.csv` | Where the live tracker is stored (mount a volume) |
| `ENABLE_SCHEDULER` | _(off)_ | `1` runs the pipeline daily (ignored in demo) |
| `RUN_HOUR` / `RUN_MINUTE` | `8` / `0` | Daily refresh time |
| `TZ` | `UTC` | Timezone for the scheduler |
| `NAUKRI_SKIP_PLAYWRIGHT` | `1` (image) | Skip the headless-browser Naukri route (keeps the image light) |

Every pipeline key (`RAPIDAPI_KEY`, `ADZUNA_*`, `SERPAPI_KEY`, `HUNTER_API_KEY`,
`GEMINI/OPENAI/ANTHROPIC`, `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`) is
best set from the in-app **Settings** page — but you can still pre-set any of them
as env vars/secrets if you prefer. All optional; each source skips gracefully if
its key is missing. Keys saved in the browser live in a `managed.env` on the data
volume (never committed).

## Tune it to *you*

Set your **profile** (seniority + experience fine-tuning) right on the **Settings**
page. Advanced search tuning — stack keywords, geography, target companies, the
source list and hard filters — still lives in `sources.yaml` → `profile:` /
`filters:` / `companies:` (see the README's **Tune it to you** section). No code
changes needed either way.

## Daily auto-refresh

There are two ways to run the pipeline every day; pick **one**.

**A. In-app scheduler (`ENABLE_SCHEDULER=1`)** — an in-process timer. Only reliable
on a **warm** instance (paid Render, or Fly with `min_machines_running = 1`). On a
host that sleeps when idle it **won't fire**, because the timer dies with the
process and a timer can't wake a sleeping instance.

**B. GitHub Actions ping (free-host friendly)** — the bundled
`.github/workflows/refresh.yml` runs on a daily cron, **wakes** your instance and
POSTs `/run`. In your fork → **Settings**:

| Type | Name | Value |
|---|---|---|
| Variable | `APP_URL` | `https://your-app.onrender.com` (no trailing slash) |
| Variable | `ENABLE_DAILY_REFRESH` | `true` to enable the daily cron |
| Secret | `ADMIN_TOKEN` | your instance's admin token |

- **Change the time:** edit the `cron:` line in `refresh.yml` (it's **UTC**).
- **Run on demand:** Actions tab → *daily-refresh* → **Run workflow**.
- **Turn it off:** set `ENABLE_DAILY_REFRESH` to `false` (or delete it), or **Disable
  workflow** in the Actions tab. If you use this, keep `ENABLE_SCHEDULER` off to
  avoid double runs.

> **Manual is always fine.** The dashboard's **Run refresh** button calls the exact
> same `POST /run` → same `morning.py`. So just clicking it once a day works
> identically — the scheduler and the Action are only ways to automate that click.

## Endpoints

| Path | What |
|---|---|
| `GET /` | Triage dashboard |
| `GET /settings` | Visual config — keys, Google Sheet, profile (writable instances) |
| `GET /setup` | Config status + onboarding checklist |
| `GET /api/roles`, `GET /api/stats` | JSON |
| `POST /run` | Trigger a pipeline refresh (writable instances only) |
| `GET /healthz` | Liveness probe |
| `GET /api/docs` | OpenAPI docs |
