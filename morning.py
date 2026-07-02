# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
One command for your daily job-search refresh.

Usage:
    python3 morning.py --force          # recommended (uses your conda env + Playwright)
    uv run morning.py --force           # also works; auto-finds system python for fetch

Prereqs (once):
    cp .env.example .env && set -a && source .env && set +a
    pip install httpx requests pyyaml beautifulsoup4 playwright
    python3 -m playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from morning_summary import print_morning_wrapup

ROOT = Path(__file__).resolve().parent


def _run(cmd: list[str], *, env: dict | None = None, check: bool = False) -> int:
    print(f"\n{'=' * 60}\n==> {' '.join(cmd)}\n{'=' * 60}\n")
    return subprocess.run(cmd, cwd=str(ROOT), env=env, check=check).returncode


def _python_has_fetch_deps(py: str) -> bool:
    try:
        r = subprocess.run(
            [py, "-c", "import requests, yaml, bs4, httpx"],
            capture_output=True,
            timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _best_python() -> str:
    """Prefer conda/home python with fetch deps (and usually Playwright)."""
    candidates: list[str] = []
    conda = os.environ.get("CONDA_PREFIX")
    if conda:
        candidates.append(os.path.join(conda, "bin", "python3"))
    which_py = shutil.which("python3")
    if which_py:
        candidates.append(which_py)
    candidates.append("/opt/homebrew/Caskroom/miniforge/base/bin/python3")
    if sys.executable not in candidates:
        candidates.append(sys.executable)

    seen: set[str] = set()
    for py in candidates:
        if not py or py in seen or not os.path.isfile(py):
            continue
        seen.add(py)
        if _python_has_fetch_deps(py):
            return py
    return sys.executable


def _fetch_command(force: bool) -> tuple[list[str], dict]:
    env = os.environ.copy()
    env.setdefault("UV_HTTP_TIMEOUT", "300")
    # Never auto-set NAUKRI_SKIP_PLAYWRIGHT — Nkparam harvest + intercept need browser.
    env.pop("NAUKRI_SKIP_PLAYWRIGHT", None)

    flags = ["--force"] if force else []
    py = _best_python()
    if _python_has_fetch_deps(py):
        return [py, str(ROOT / "fetch_jobs.py"), *flags], env

    print(
        "  ⚠  No python with fetch deps found. Install once:\n"
        "     pip install httpx requests pyyaml beautifulsoup4 playwright\n"
    )
    return ["uv", "run", "fetch_jobs.py", *flags], env


def _score_command(top: int, py: str) -> list[str]:
    if _python_has_fetch_deps(py):
        return [py, str(ROOT / "score.py"), "--top", str(top)]
    return ["uv", "run", "score.py", "--top", str(top)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="Bypass 20h cooldown for paid APIs (jsearch/adzuna/serpapi).")
    ap.add_argument("--top", type=int, default=0,
                    help="Print top N in terminal (0 = Sheet only, default).")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip fetch (dedup/rescore/push only).")
    ap.add_argument("--no-outreach", action="store_true",
                    help="Skip outreach prep (contacts + draft emails).")
    ap.add_argument("--outreach-top", type=int, default=10,
                    help="Top N roles/companies for outreach drafts (default 10).")
    args = ap.parse_args()

    sheets_ok = bool(
        os.environ.get("GOOGLE_SHEETS_ID", "").strip()
        and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    )
    py = _best_python()

    print(
        "\n  ☀  MORNING RUN — job search refresh\n"
        "  ─────────────────────────────────────\n"
        "  1. Pull Sheet → clean junk/geo-fails → fetch → score → push Sheet\n"
        "  2. Outreach prep → contacts + cold/referral drafts (unless --no-outreach)\n"
        f"  Python: {py}\n"
    )
    if not sheets_ok:
        print("  ⚠  Google Sheets not configured — tracker.csv only.\n")
    else:
        print("  ✓  Work from your Sheet after this finishes.\n")

    env_base = os.environ.copy()
    env_base.setdefault("UV_HTTP_TIMEOUT", "300")
    env_base.pop("NAUKRI_SKIP_PLAYWRIGHT", None)

    if not args.no_fetch:
        fetch_cmd, fetch_env = _fetch_command(args.force)
        code = _run(fetch_cmd, env=fetch_env)
        if code != 0:
            print(f"\n  fetch_jobs.py exited {code} — check output above.")
            return code
    else:
        dedup = [py, str(ROOT / "fetch_jobs.py"), "--dedup-only"]
        _run(dedup, env=env_base)
        if sheets_ok:
            _run(["uv", "run", "sheets_sync.py", "--push"], env=env_base)

    if args.top > 0:
        _run(_score_command(args.top, py), env=env_base)

    if not args.no_outreach:
        outreach_cmd = [
            py, str(ROOT / "outreach_run.py"),
            "--top", str(args.outreach_top),
        ]
        _run(outreach_cmd, env=env_base)

    print_morning_wrapup(
        sheets_ok=sheets_ok,
        sheet_id=os.environ.get("GOOGLE_SHEETS_ID", "").strip(),
        outreach_ran=not args.no_outreach,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
