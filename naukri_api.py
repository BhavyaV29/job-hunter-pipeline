"""Naukri job search via public jobapi/v3 (jobspy-js approach).

Warms a browser-like session, then calls the same JSON API the Naukri website
uses. Auto-refreshes Nkparam via headed Playwright on 403/406 when possible.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from fetch_jobs import BROWSER_UA, POLITE_DELAY, SkipSource, clean
from nkparam_refresh import load_cached_nkparam, patch_env_file, refresh_nkparam_if_needed

NAUKRI_SEARCH = "https://www.naukri.com/jobapi/v3/search"

# Default from jobspy-js; override via NAUKRI_NKPARAM when it expires (403).
_DEFAULT_NKPARAM = (
    "Ppy0YK9uSHqPtG3bEejYc04RTpUN2CjJOrqA68tzQt0SKJHXZKzz9M8cZtKLVkoOuQmfe4cTb1r2CwfHaxW5Tg=="
)

# experience filter: 0=fresher, 1=1yr, 2=2yr, ...
EXP_MAP = {0: "0", 1: "1", 2: "2", 3: "3"}


def _naukri_headers() -> dict[str, str]:
    nk = os.environ.get("NAUKRI_NKPARAM", _DEFAULT_NKPARAM).strip()
    return {
        "authority": "www.naukri.com",
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "appid": "109",
        "clientid": "d3skt0p",
        "systemid": "Naukri",
        "Nkparam": nk,
        "user-agent": BROWSER_UA,
        "referer": "https://www.naukri.com/",
    }


def _parse_naukri_salary(placeholders: list) -> dict | None:
    for p in placeholders or []:
        if p.get("type") != "salary":
            continue
        text = (p.get("label") or "").strip()
        if not text or "not disclosed" in text.lower():
            return None
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(Lacs?|Lakh|Cr)",
            text, re.I,
        )
        if not m:
            return None
        lo, hi = float(m.group(1)), float(m.group(2))
        unit = m.group(3).lower()
        mult = 1e7 if unit == "cr" else 1e5
        return {
            "min": int(lo * mult),
            "max": int(hi * mult),
            "currency": "INR",
            "period": "YEAR",
        }
    return None


def _parse_naukri_location(placeholders: list) -> str:
    for p in placeholders or []:
        if p.get("type") == "location":
            return (p.get("label") or "").strip()
    return "India"


def _job_from_api(job: dict) -> dict | None:
    job_id = job.get("jobId")
    title = (job.get("title") or "").strip()
    if not job_id or not title:
        return None
    company = (job.get("companyName") or "").strip()
    placeholders = job.get("placeholders") or []
    location = _parse_naukri_location(placeholders)
    jd = job.get("jdURL") or f"/job-listings-{job_id}"
    url = jd if jd.startswith("http") else f"https://www.naukri.com{jd}"
    desc = clean(job.get("jobDescription") or "")
    return {
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "updated": str(job.get("createdDate") or ""),
        "salary": _parse_naukri_salary(placeholders),
        "description": desc,
    }


def fetch_naukri_api(cfg: dict) -> list[dict]:
    """Search Naukri jobapi/v3 — individual job-listings with full metadata."""
    load_cached_nkparam()
    keywords = cfg.get("keywords") or [
        "software engineer", "backend developer",
        "machine learning engineer", "sde",
    ]
    experience = int(cfg.get("experience", 0))
    pages = int(cfg.get("pages", 2))
    location = cfg.get("location", "India")

    session = requests.Session()
    session.headers.update(_naukri_headers())

    try:
        session.get("https://www.naukri.com/", timeout=20)
        time.sleep(1)
    except Exception:
        pass

    out: list[dict] = []
    seen: set[str] = set()
    nkparam_refreshed = False

    for kw in keywords:
        seo = f"{kw.lower().replace(' ', '-')}-jobs"
        for page in range(1, pages + 1):
            params = {
                "noOfResults": "20",
                "urlType": "search_by_keyword",
                "searchType": "adv",
                "keyword": kw,
                "pageNo": str(page),
                "k": kw,
                "seoKey": seo,
                "src": "jobsearchDesk",
                "latLong": "",
                "location": location,
                "experience": EXP_MAP.get(experience, str(experience)),
            }
            try:
                r = session.get(NAUKRI_SEARCH, params=params, timeout=25)
            except Exception as e:
                raise SkipSource(f"Naukri API unreachable ({type(e).__name__})")

            if r.status_code in (403, 406) and not nkparam_refreshed:
                print(f"  [naukri] jobapi {r.status_code} — attempting Nkparam auto-refresh…")
                new_nk = refresh_nkparam_if_needed(force=True)
                if new_nk:
                    patch_env_file(new_nk)
                    print("  [naukri] Nkparam auto-refresh OK (headed Playwright)")
                    session.headers.update(_naukri_headers())
                    nkparam_refreshed = True
                    try:
                        r = session.get(NAUKRI_SEARCH, params=params, timeout=25)
                    except Exception as e:
                        raise SkipSource(
                            f"Naukri API unreachable after nkparam refresh ({type(e).__name__})"
                        )
                else:
                    print("  [naukri] Nkparam auto-refresh FAILED — run scripts/harvest_nkparam.py")
            if r.status_code in (403, 406):
                raise SkipSource(
                    "Naukri API 403/406 — Nkparam expired. Run: python3 scripts/harvest_nkparam.py"
                )
            if r.status_code != 200:
                break

            try:
                data = r.json()
            except ValueError:
                break

            jobs = data.get("jobDetails") or []
            if not jobs:
                break

            for job in jobs:
                row = _job_from_api(job)
                if not row or row["url"] in seen:
                    continue
                seen.add(row["url"])
                out.append(row)

            time.sleep(POLITE_DELAY)

    if not out:
        raise SkipSource("Naukri jobapi returned 0 jobs")
    return out
