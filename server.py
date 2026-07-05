"""FastAPI web app over the pipeline's tracker.csv.

A triage dashboard (HTMX + Tailwind), a visual Settings page (enter API keys,
connect a Sheet, set your profile — no file editing), a JSON API, a "Run refresh"
button and an optional daily scheduler that call the same morning.py a human
would. A fresh instance comes up live and self-configures an ADMIN_TOKEN (printed
on startup) that gates writes; open the dashboard with ?token=... to configure.
Set DEMO_MODE=1 to serve the read-only sample (the public showcase). Each user
runs their own instance — no shared server holds anyone's secrets.

    pip install -r requirements-web.txt && uvicorn server:app --reload
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import profile_config
import tracker_store as store
import web_settings

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()  # load managed keys, ensure admin token, arm scheduler
    yield


app = FastAPI(title="job-hunter-pipeline", docs_url="/api/docs", redoc_url=None,
              lifespan=_lifespan)

LLM_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

_run_lock = threading.Lock()
_run_state: dict = {"running": False, "started": None, "finished": None,
                    "returncode": None, "log": ""}


# --- access control ----------------------------------------------------------
def _admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "").strip()


def admin_ok(request: Request) -> bool:
    """True when writes/run are allowed: no token configured, or it matches."""
    tok = _admin_token()
    if not tok:
        return True
    got = (request.headers.get("X-Admin-Token", "") or request.query_params.get("token", "")
           or request.cookies.get("admin_token", "")).strip()
    return got == tok


def require_writable(request: Request) -> None:
    if store.is_demo():
        raise HTTPException(403, "Read-only demo. Deploy your own instance to make changes.")
    if not admin_ok(request):
        raise HTTPException(401, "Admin token required.")


def _apply_token_cookie(request: Request, resp: HTMLResponse) -> HTMLResponse:
    """On a page load with a valid ?token=, remember it in a cookie so forms and
    HTMX calls stay authed without repeating the token in every URL."""
    tok = _admin_token()
    if tok and request.query_params.get("token", "").strip() == tok:
        resp.set_cookie("admin_token", tok, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return resp


def config_status() -> dict:
    def has(name: str) -> bool:
        return bool(os.environ.get(name, "").strip())

    managed = web_settings.SECRET_FIELDS + ["GOOGLE_SHEETS_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"]
    return {
        "demo": store.is_demo(),
        "admin_protected": bool(_admin_token()),
        "sheets": has("GOOGLE_SHEETS_ID") and has("GOOGLE_SERVICE_ACCOUNT_JSON"),
        "api_keys": {
            "RAPIDAPI_KEY (JSearch)": has("RAPIDAPI_KEY"),
            "ADZUNA": has("ADZUNA_APP_ID") and has("ADZUNA_APP_KEY"),
            "SERPAPI_KEY": has("SERPAPI_KEY"),
            "HUNTER_API_KEY": has("HUNTER_API_KEY"),
            "LLM (Gemini/OpenAI/Anthropic)": any(has(k) for k in LLM_KEYS),
        },
        "present": {name: has(name) for name in managed},
        "llm_provider": os.environ.get("LLM_PROVIDER", "").strip(),
        "sheets_id": os.environ.get("GOOGLE_SHEETS_ID", "").strip(),
        "seniority": str(profile_config.load_profile().get("seniority", "fresher")),
        "tracker": store.active_path().name,
    }


# --- background pipeline run -------------------------------------------------
def _run_pipeline(force: bool, no_outreach: bool) -> None:
    cmd = [sys.executable, str(ROOT / "morning.py")]
    if force:
        cmd.append("--force")
    if no_outreach:
        cmd.append("--no-outreach")
    _run_state.update(running=True, started=time.time(), finished=None, returncode=None, log="")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        _run_state.update(log=(proc.stdout or "") + (proc.stderr or ""), returncode=proc.returncode)
    except Exception as exc:  # noqa: BLE001 - surface launch failures in the UI
        _run_state.update(log=f"failed to launch pipeline: {exc}", returncode=-1)
    finally:
        _run_state.update(running=False, finished=time.time())
        _run_lock.release()


def start_run(force: bool = True, no_outreach: bool = False) -> bool:
    """Kick a run in the background; False if one is already in progress."""
    if not _run_lock.acquire(blocking=False):
        return False
    threading.Thread(target=_run_pipeline, args=(force, no_outreach), daemon=True).start()
    return True


# --- roles helpers -----------------------------------------------------------
def _view_params(request: Request) -> dict:
    q = request.query_params
    return {"view": q.get("view", "triage"), "stage": q.get("stage", ""),
            "q": q.get("q", ""), "sort": q.get("sort", "score")}


def _query_roles(p: dict) -> list[dict]:
    return store.list_roles(stage=p["stage"], query=p["q"], sort=p["sort"],
                            triage_only=(p["view"] == "triage" and not p["stage"]))


def _rows_response(request: Request, p: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/rows.html", {
        "roles": _query_roles(p), "cfg": config_status(),
        "admin_ok": admin_ok(request), "params": p})


# --- pages + partials --------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    p = _view_params(request)
    resp = templates.TemplateResponse(request, "dashboard.html", {
        "roles": _query_roles(p), "stats": store.stats(),
        "cfg": config_status(), "params": p, "admin_ok": admin_ok(request),
        "run_state": _run_state})
    return _apply_token_cookie(request, resp)


@app.get("/roles", response_class=HTMLResponse)
def roles_partial(request: Request):
    return _rows_response(request, _view_params(request))


@app.get("/stats", response_class=HTMLResponse)
def stats_partial(request: Request):
    return templates.TemplateResponse(request, "partials/stats.html",
                                      {"stats": store.stats()})


@app.post("/roles/stage", response_class=HTMLResponse)
def set_stage(request: Request, url: str = Form(...), stage: str = Form(...),
              view: str = Form("triage"), sort: str = Form("score"),
              q: str = Form(""), stage_filter: str = Form("")):
    require_writable(request)
    store.update_stage(url, stage)
    resp = _rows_response(request, {"view": view, "stage": stage_filter, "q": q, "sort": sort})
    resp.headers["HX-Trigger"] = "refreshStats"
    return resp


@app.post("/run", response_class=HTMLResponse)
def run(request: Request, force: bool = Form(True), no_outreach: bool = Form(False)):
    require_writable(request)
    msg = ("Refresh started — new roles will appear shortly."
           if start_run(force, no_outreach) else "A refresh is already running.")
    return HTMLResponse(f'<span class="text-sm text-neutral-300">{msg}</span>')


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    resp = templates.TemplateResponse(request, "setup.html", {
        "cfg": config_status(), "admin_ok": admin_ok(request)})
    return _apply_token_cookie(request, resp)


# --- visual settings ---------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    resp = templates.TemplateResponse(request, "settings.html", {
        "cfg": config_status(), "profile": profile_config.load_profile(),
        "demo": store.is_demo(), "admin_ok": admin_ok(request),
        "saved": request.query_params.get("saved", "")})
    return _apply_token_cookie(request, resp)


@app.post("/settings/keys")
async def settings_keys(request: Request):
    require_writable(request)
    web_settings.save_keys(dict(await request.form()))
    return RedirectResponse("/settings?saved=keys", status_code=303)


@app.post("/settings/sheet")
async def settings_sheet(request: Request):
    require_writable(request)
    form = await request.form()
    sid = (form.get("GOOGLE_SHEETS_ID") or "").strip()
    if sid:
        web_settings.save_keys({"GOOGLE_SHEETS_ID": sid})
    web_settings.save_service_account(form.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "")
    return RedirectResponse("/settings?saved=sheet", status_code=303)


@app.post("/settings/profile")
async def settings_profile(request: Request):
    require_writable(request)
    form = await request.form()

    def num(name: str):
        v = (form.get(name) or "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None

    dst = (form.get("drop_senior_titles") or "").strip()
    web_settings.save_profile({
        "seniority": (form.get("seniority") or "").strip() or None,
        "exp_good_max": num("exp_good_max"),
        "exp_warn_max": num("exp_warn_max"),
        "max_exp_years": num("max_exp_years"),
        "drop_senior_titles": True if dst == "yes" else (False if dst == "no" else None),
    })
    return RedirectResponse("/settings?saved=profile", status_code=303)


# --- JSON API ----------------------------------------------------------------
@app.get("/api/roles")
def api_roles(request: Request):
    keep = ("company", "role", "location", "salary", "deadline", "stage", "url",
            "source", "date_found", "resume_variant", "exp_match")
    return JSONResponse([
        {**{k: r.get(k, "") for k in keep}, "score": r["_score"], "tier": r["_tier"]}
        for r in _query_roles(_view_params(request))])


@app.get("/api/stats")
def api_stats():
    return JSONResponse(store.stats())


@app.get("/api/run/status")
def api_run_status():
    return JSONResponse({k: v for k, v in _run_state.items() if k != "log"})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "demo": store.is_demo()}


# --- startup -----------------------------------------------------------------
def _startup() -> None:
    web_settings.load_managed_env()
    if not store.is_demo():
        tok = web_settings.ensure_admin_token()
        print("  Live instance ready. Configure + run from the dashboard:")
        print(f"    open http://localhost:8000/?token={tok}")
    _maybe_start_scheduler()


def _maybe_start_scheduler() -> None:
    if store.is_demo() or os.environ.get("ENABLE_SCHEDULER", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("  ENABLE_SCHEDULER set but apscheduler not installed — skipping.")
        return
    hour, minute = int(os.environ.get("RUN_HOUR", "8")), int(os.environ.get("RUN_MINUTE", "0"))
    sched = BackgroundScheduler(timezone=os.environ.get("TZ", "UTC"))
    sched.add_job(lambda: start_run(force=True), CronTrigger(hour=hour, minute=minute),
                  id="daily_refresh", replace_existing=True)
    sched.start()
    print(f"  Scheduler on — daily refresh at {hour:02d}:{minute:02d} ({sched.timezone}).")
