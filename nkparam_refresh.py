"""Auto-refresh Naukri Nkparam when jobapi returns 403/406."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_STATE_KEY = "naukri_nkparam"


def _state_path() -> Path:
    return Path(__file__).resolve().parent / ".fetch_state.json"


def load_cached_nkparam() -> str | None:
    """Nkparam from env, then .fetch_state.json."""
    env = os.environ.get("NAUKRI_NKPARAM", "").strip()
    if env:
        return env
    path = _state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        v = (data.get(_STATE_KEY) or "").strip()
        if v:
            os.environ["NAUKRI_NKPARAM"] = v
        return v or None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def persist_nkparam(value: str) -> None:
    """Set NAUKRI_NKPARAM in-process and cache in .fetch_state.json."""
    value = value.strip()
    if not value:
        return
    os.environ["NAUKRI_NKPARAM"] = value
    path = _state_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data[_STATE_KEY] = value
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def harvest_nkparam(*, headed: bool = True, timeout_ms: int = 60000) -> str | None:
    """Capture Nkparam from a real browser session (Playwright).

    Not gated by NAUKRI_SKIP_PLAYWRIGHT — that flag only skips job-listing intercept.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    captured: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_context().new_page()

            def on_req(request):
                nk = request.headers.get("nkparam") or request.headers.get("Nkparam")
                if nk and nk not in captured:
                    captured.append(nk)

            page.on("request", on_req)
            page.goto(
                "https://www.naukri.com/backend-developer-jobs?k=backend+developer&experience=0",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(8000)
            browser.close()
    except Exception as e:
        err = str(e)
        if "Executable doesn't exist" in err or "playwright install" in err.lower():
            print(
                "  [naukri] Playwright browser missing. Run once:\n"
                "           python3 -m playwright install chromium"
            )
        else:
            print(f"  [naukri] Playwright harvest failed: {type(e).__name__}: {e}")
        return None

    return captured[0] if captured else None


def refresh_nkparam_if_needed(*, force: bool = False) -> str | None:
    """Harvest + persist Nkparam. Returns new value or None."""
    if not force:
        cached = load_cached_nkparam()
        if cached:
            return cached
    value = harvest_nkparam(headed=True)
    if value:
        persist_nkparam(value)
        patch_env_file(value)
    elif force:
        print("  [naukri] Headed harvest did not capture Nkparam (install: pip install playwright && python3 -m playwright install chromium)")
    return value


def patch_env_file(value: str, env_path: Path | None = None) -> bool:
    """Optionally write NAUKRI_NKPARAM to .env (best-effort)."""
    path = env_path or Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        line = f"NAUKRI_NKPARAM={value}\n"
        if re.search(r"^NAUKRI_NKPARAM=", text, re.M):
            text = re.sub(r"^NAUKRI_NKPARAM=.*$", line.strip(), text, flags=re.M)
        else:
            text = text.rstrip() + "\n" + line
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False
