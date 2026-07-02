# Resume tuner — feasibility notes

This doc assesses adding **per-company resume tailoring** and **LLM filtering against your existing resume + preferences** to the jobsearch pipeline.

## What you already have

- `resume_variant` on each tracker row (`master`, `backend`, `ai_platform`) — keyword-based, set in `fetch_jobs.make_row()`.
- `score.py` keyword fit + tier ranking — stack/location/seniority heuristics, not resume-aware.
- `llm_jd.py` — optional JD parsing (stack, YOE, remote, fit summary → `notes` / `exp_*`) when `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is set.
- No resume PDFs in this repo (gitignored under `resume/out/`); resumes live in `resume/*.tex` and compile to `resume/out/`. The tuner should accept a path via env (`RESUME_PATH`).

## Per-company resume tuner (JD → tailored bullets)

**Easy (MVP, 1–2 evenings)**

- Keyword gap analysis: diff JD tokens (from `llm_jd` stack output or simple noun extraction) against a plain-text resume export.
- Suggest 2–3 bullet rewrites per JD using `resume_variant` as the base PDF/Markdown template — LLM returns *suggestions only*, you paste into your real resume.
- Wire: `uv run resume_tune.py --url <tracker-url>` reading row from `tracker.csv` + `RESUME_PATH`.

**Hard (not worth automating fully yet)**

- Layout-perfect PDF generation per company (fonts, spacing, ATS parsers) — fragile and high maintenance.
- Fully autonomous rewrite without human review — risks fabrication and inconsistent dates/titles.
- Maintaining three complete resume forks per application at scale.

**Recommended MVP:** Keep one canonical resume (per variant). For top-N `stage=sourced` roles, run LLM to output a short “gap + bullet suggestions” block appended to `notes` or a sidecar `tuning/<company>.md`. Human approves before export.

## LLM filtering (resume + preferences)

**Easy**

- Binary “apply / skip” with reason: pass JD + resume text + hard rules from `sources.yaml` (`min_salary_lpa`, remote, `max_exp_years`) to a cheap model; cache by URL in `.jd_cache.json` (same pattern as `llm_jd.py`).
- Respect existing columns: set `exp_match`, optionally drop before append (`llm_jd.enrich_job` already supports `keep: false`).

**Hard**

- Reliable scoring aligned with your true preferences (culture, team size, visa, hybrid days) without a structured preference file.
- Comparing resume PDFs with embedded graphics — use `.txt` / `.md` export for LLM input.

**Recommended MVP:** Add `preferences.yaml` (must-have keywords, blocklist companies, max YOE). Extend `llm_jd` or a thin `llm_filter.py` called from `_process_fetched_job()` only when `LLM_FILTER=1`. Default off; log skip reasons to stdout, not silent drops.

## Bottom line

Per-JD **keyword gaps + bullet suggestions** and **resume-aware skip/rank** are feasible on top of `resume_variant` + `llm_jd` with minimal new surface area. Full auto-resume PDF rewriting is not a good ROI for a personal pipeline — treat LLM output as drafting aid, keep `tracker.csv` as source of truth, and never commit API keys or resume files to git.
