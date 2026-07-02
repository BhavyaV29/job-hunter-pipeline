"""Cutshort fetcher — city-specific pages embed jobs in __NEXT_DATA__ (no Playwright).

Probed 2026-06-26: generic /jobs/backend-jobs is empty (client-rendered),
but city paths like /jobs/backend-developer-jobs-in-bangalore-bengaluru
return ~50 individual postings with publicUrl + headline in __NEXT_DATA__.
"""
from __future__ import annotations

import json
import re
import time

import requests

from fetch_jobs import BROWSER_HEADERS, POLITE_DELAY, SkipSource, clean
from job_quality import parse_cutshort_url_slug

# Verified working paths (probe_boards hit-and-trial).
CITY_PATHS = [
    "/jobs/backend-developer-jobs-in-bangalore-bengaluru",
    "/jobs/machine-learning-ml-jobs-in-bangalore-bengaluru",
    "/jobs/backend-developer-jobs-in-delhi-ncr-gurgaon-noida",
    "/jobs/software-engineer-jobs-in-hyderabad",
    "/jobs/backend-developer-jobs-in-pune",
    "/jobs/machine-learning-ml-jobs-in-india",
]


def _walk_cutshort_jobs(payload) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()

    def walk(obj):
        if isinstance(obj, dict):
            title = (
                obj.get("headline") or obj.get("title")
                or obj.get("jobTitle") or obj.get("name")
            )
            link = (
                obj.get("publicUrl") or obj.get("url")
                or obj.get("jobUrl") or obj.get("slug")
            )
            if title and link:
                if not str(link).startswith("http"):
                    link = (
                        f"https://cutshort.io{link}"
                        if str(link).startswith("/")
                        else f"https://cutshort.io/job/{link}"
                    )
                if "/job/" in link and link not in seen:
                    company = (
                        obj.get("companyName")
                        or (obj.get("company") or {}).get("name", "")
                        if isinstance(obj.get("company"), dict)
                        else obj.get("company", "")
                    )
                    loc = obj.get("location") or obj.get("locations") or "India"
                    if isinstance(loc, list):
                        loc = ", ".join(str(x) for x in loc)
                    company = str(company or "")
                    if not company.strip():
                        parsed_co, _ = parse_cutshort_url_slug(link)
                        if parsed_co:
                            company = parsed_co
                    seen.add(link)
                    found.append({
                        "company": str(company or ""),
                        "title": str(title),
                        "location": str(loc or "India"),
                        "url": link,
                        "updated": str(obj.get("createdAt") or obj.get("postedOn") or ""),
                        "description": clean(
                            obj.get("description") or obj.get("jobDescription") or ""
                        ),
                    })
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return found


def fetch_cutshort_nextdata(cfg: dict) -> list[dict]:
    """Fetch individual Cutshort jobs from city-specific __NEXT_DATA__ pages."""
    paths = cfg.get("city_paths") or cfg.get("paths") or CITY_PATHS
    out: list[dict] = []
    seen: set[str] = set()

    for path in paths:
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"https://cutshort.io{path}"
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=25)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            r.text, re.DOTALL,
        )
        if not m:
            continue
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for j in _walk_cutshort_jobs(payload):
            if j["url"] not in seen:
                seen.add(j["url"])
                out.append(j)
        time.sleep(POLITE_DELAY)

    if not out:
        raise SkipSource(
            "Cutshort city __NEXT_DATA__ returned 0 jobs (paths may have changed)."
        )
    return out
