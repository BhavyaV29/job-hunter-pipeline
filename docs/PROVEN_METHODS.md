# Proven fetch methods

## On residential IP (personal laptop) — expected behavior

| Board | Primary (fast, free) | Fallback |
|-------|----------------------|----------|
| **Cutshort** | City-path `__NEXT_DATA__` HTTP | SerpApi |
| **Hirist** | Direct `jobseeker-api` JSON API | SerpApi → Playwright |
| **Naukri** | `jobapi/v3` with `NAUKRI_NKPARAM` | RSS → Playwright → JSearch → SerpApi |
| **Wellfound** | Playwright Apollo `__NEXT_DATA__` | SerpApi |

Nothing is "blocked" on residential except:
- **Naukri Nkparam** expires → run `python3 scripts/harvest_nkparam.py` once, add to `.env`
- **Wellfound** if Playwright fails → SerpApi still works

## Test each board (no full pipeline)

```bash
cd job-hunter-pipeline
set -a && source .env && set +a

python3 scripts/test_proven_fetchers.py cutshort
python3 scripts/test_proven_fetchers.py hirist
python3 scripts/test_proven_fetchers.py wellfound
python3 scripts/test_proven_fetchers.py naukri
```

## Optional .env tuning

```bash
NAUKRI_NKPARAM=...          # from harvest_nkparam.py — skips Playwright when valid
NAUKRI_SKIP_PLAYWRIGHT=1    # force jobapi/RSS only (when Nkparam works)
PLAYWRIGHT_HEADED=1         # if Naukri headless gets Access Denied
```

## Daily run (personal laptop)

```bash
cd job-hunter-pipeline
set -a && source .env && set +a
uv run --with playwright fetch_jobs.py
uv run fetch_jobs.py --cleanup-junk --dry-run   # preview junk rows
uv run fetch_jobs.py --cleanup-junk            # one-time if sheet has junk rows
```
