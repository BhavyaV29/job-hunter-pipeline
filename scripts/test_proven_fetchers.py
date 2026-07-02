#!/usr/bin/env python3
"""Test each board fetcher in isolation — no tracker, no full pipeline.

Usage:
  cd jobsearch && python3 scripts/test_proven_fetchers.py cutshort
  python3 scripts/test_proven_fetchers.py hirist
  python3 scripts/test_proven_fetchers.py wellfound
  python3 scripts/test_proven_fetchers.py naukri   # slow (~2min, opens browser)
  python3 scripts/test_proven_fetchers.py all
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env if present
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

CFG = {"_fetch_state": {}, "_force": False, "keywords": ["backend"], "pages": 1}


def test_cutshort():
    from cutshort_nextdata import fetch_cutshort_nextdata
    jobs = fetch_cutshort_nextdata(CFG)
    print(f"✅ Cutshort city __NEXT_DATA__: {len(jobs)} jobs")
    _sample(jobs)
    return bool(jobs)


def test_hirist():
    from fetch_jobs import fetch_hirist, SkipSource
    try:
        jobs = fetch_hirist({**CFG, "pages": 1})
        print(f"✅ Hirist fetch_hirist: {len(jobs)} jobs")
        _sample(jobs)
        return bool(jobs)
    except SkipSource as e:
        print(f"❌ Hirist: {e}")
        return False


def test_wellfound():
    from fetch_jobs import fetch_wellfound, SkipSource
    try:
        jobs = fetch_wellfound(CFG)
        print(f"✅ Wellfound fetch_wellfound: {len(jobs)} jobs")
        _sample(jobs)
        return bool(jobs)
    except SkipSource as e:
        print(f"❌ Wellfound: {e}")
        return False


def test_naukri():
    from fetch_jobs import fetch_naukri, SkipSource
    try:
        jobs = fetch_naukri({**CFG, "keywords": ["backend developer"], "experience": 0})
        print(f"✅ Naukri fetch_naukri: {len(jobs)} jobs")
        _sample(jobs)
        return bool(jobs)
    except SkipSource as e:
        print(f"❌ Naukri: {e}")
        return False


def _sample(jobs):
    for j in jobs[:3]:
        print(f"   {j.get('company','?')[:20]:20} | {j.get('title','?')[:35]:35} | {j.get('url','')[:65]}")


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    tests = {
        "cutshort": test_cutshort,
        "hirist": test_hirist,
        "wellfound": test_wellfound,
        "naukri": test_naukri,
    }
    if which == "all":
        results = {k: fn() for k, fn in tests.items()}
    elif which in tests:
        results = {which: tests[which]()}
    else:
        print(f"Unknown: {which}. Use: {', '.join(tests)} or all")
        sys.exit(1)
    print("\n=== RESULTS ===")
    for k, v in results.items():
        print(f"  {'✅' if v else '❌'} {k}")


if __name__ == "__main__":
    main()
