#!/usr/bin/env python3
"""One-time helper: capture Naukri Nkparam header from a real browser session.

Run on residential IP:
  python3 scripts/harvest_nkparam.py

Copy the printed value into .env (or rely on auto-write):
  NAUKRI_NKPARAM=...
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from nkparam_refresh import harvest_nkparam, patch_env_file, persist_nkparam

    value = harvest_nkparam(headed=True)
    if not value:
        print("No Nkparam captured.")
        print("If browser failed to open, run: python3 -m playwright install chromium")
        sys.exit(1)

    persist_nkparam(value)
    patched = patch_env_file(value)
    print("\nNkparam saved to .fetch_state.json")
    if patched:
        print("Updated job-hunter-pipeline/.env with NAUKRI_NKPARAM")
    else:
        print("\nAdd to .env manually:\n")
        print(f"NAUKRI_NKPARAM={value}\n")
    print("(Expires periodically — fetch_jobs auto-refreshes on 403/406 when Playwright is available.)")


if __name__ == "__main__":
    main()
