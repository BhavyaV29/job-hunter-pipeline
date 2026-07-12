# Deploy your own instance

This is a **deploy-your-own** tool: everyone runs their *own* copy with their
*own* API keys and their *own* Google Sheet. No shared server ever holds your
secrets, and job-board fetches run from *your* infra — the same safety model as
the CLI.

A live deploy that has durable `/data` can be configured in the browser: if you
didn't set an `ADMIN_TOKEN`, it generates one and prints a `?token=…` link in the
startup logs. That link establishes the cookie required for every private
dashboard/data/config read and all writes. (A public `DEMO_MODE=1` showcase serves
`tracker.sample.csv` publicly and blocks writes.)

Storage is host-specific. Docker's `VOLUME ["/data"]` declaration is only metadata;
it does not provision a disk on Render or any other platform.

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
3. The free blueprint comes up in safe, public **demo mode** (`DEMO_MODE=1`) and
   creates no paid resources.

To use it as a private live instance, explicitly set `DEMO_MODE=0`, keep the
generated `ADMIN_TOKEN` secret, and choose a persistence model:

- **No-cost Render:** treat the filesystem as disposable. Put API keys in Render
  environment secrets (and credentials in a secret file), use a Google Sheet as
  the durable tracker, and keep profile tuning in your fork. Browser-saved
  settings and local `tracker.csv` can disappear after restart/redeploy.
- **Persistent Render:** manually add a **paid** persistent disk mounted at
  `/data`. Render charges for this; the blueprint intentionally does not add it.
- **No Render disk:** use Docker Compose or Fly.io below, both with an explicit
  volume.

The free plan also sleeps when idle, so `ENABLE_SCHEDULER` is not reliable there;
use the authenticated GitHub Actions ping described below. A Google Sheet is still
needed if the tracker must survive filesystem replacement.

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

## Persistence at a glance

- **Docker Compose:** the `jobdata` named volume persists `/data`.
- **Fly.io:** the explicitly created `jobdata` volume persists `/data`.
- **Render free:** ephemeral filesystem; the Docker `VOLUME` does not change that.
- **Render paid disk:** durable only after you add and mount a paid disk at `/data`.
- **Google Sheets:** host-independent durable tracker; use environment secrets or
  a durable disk for configuration/credentials.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `0` (image) | `0` = live (your tracker); `1` = read-only sample showcase |
| `ADMIN_TOKEN` | _auto-generated_ | In live mode, gates dashboard/data/config reads and all writes via `?token=` / `X-Admin-Token`. Unset → generated on first boot and printed in logs; persistence follows `/data` unless supplied by the host environment |
| `TRACKER_CSV` | `/data/tracker.csv` | Where the live tracker is stored (mount a volume) |
| `ENABLE_SCHEDULER` | _(off)_ | `1` runs the pipeline daily (ignored in demo) |
| `RUN_HOUR` / `RUN_MINUTE` | `8` / `0` | Daily refresh time |
| `TZ` | `UTC` | Timezone for the scheduler |
| `NAUKRI_SKIP_PLAYWRIGHT` | `1` (image) | Skip the headless-browser Naukri route (keeps the image light) |

Every pipeline key (`RAPIDAPI_KEY`, `ADZUNA_*`, `SERPAPI_KEY`, `HUNTER_API_KEY`,
`GEMINI/OPENAI/ANTHROPIC`, `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`) can
be set from the in-app **Settings** page when `/data` is durable, or pre-set as
host environment secrets/secret files. All are optional; each source skips
gracefully if its key is missing. Browser saves go to `managed.env` under `/data`
(never committed), so they are not persistent on free Render.

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
| `GET /` | Triage dashboard (public in demo; admin token required live) |
| `GET /settings` | Visual config — public read-only demo; admin token required live |
| `GET /setup` | Config status — public in demo; admin token required live |
| `GET /roles`, `GET /stats` | Dashboard fragments — public in demo; admin token required live |
| `GET /api/roles`, `GET /api/stats`, `GET /api/run/status` | JSON — public in demo; admin token required live |
| `POST /run` | Trigger a pipeline refresh (writable instances only) |
| `GET /healthz` | Public liveness probe |
| `GET /api/docs` | OpenAPI docs |
