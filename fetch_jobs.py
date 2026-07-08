# /// script
# requires-python = ">=3.9"
# dependencies = ["requests", "pyyaml", "beautifulsoup4", "httpx"]
# ///
"""
Tier-1 job sourcing pipeline.

Three kinds of sources feed one filtered, deduped, append-only tracker.csv:

1. Per-company ATS APIs (Greenhouse / Lever / Ashby / Workable / Recruitee)
   for the companies listed under `companies:` in sources.yaml. Public, no auth.

2. Aggregator / index APIs (The Muse / Remotive / RemoteOK / Arbeitnow / Adzuna /
   JSearch / SerpApi) listed under `aggregators:` in sources.yaml. These index
   roles posted across many job boards. Two of them are the legitimate route to
   the big consumer platforms:
     - JSearch (RapidAPI) aggregates *Google for Jobs*, which indexes LinkedIn,
       Indeed, Glassdoor, ZipRecruiter, etc. -> our reliable LinkedIn/Indeed feed.
     - SerpApi (Google Jobs engine) is an alternate route to the same index.
   Both are OPTIONAL and need a free/keyed env var; they SkipSource if unset.

3. Public / guest endpoints of consumer platforms, used *without any login*:
     - LinkedIn guest job-search (public jobs-guest HTML cards, no auth).
     - Naukri public web-search JSON API (no login; freshers via experience=0).
     - Wellfound via Playwright Apollo __NEXT_DATA__ or SerpApi fallback.

   We NEVER use your personal LinkedIn/Indeed/Naukri/Wellfound login or any
   authenticated session - authenticated scraping is what gets *personal accounts
   banned*. We only hit public/guest endpoints, politely (realistic User-Agent,
   timeouts, small delays, modest caps), and degrade gracefully when a platform
   changes its shape or rate-limits us.

Everything is filtered by title + location + the geo/salary keep-drop rules,
deduped against the tracker (by URL), and appended to tracker.csv with
stage=sourced.

Run daily:
    uv run fetch_jobs.py
    uv run fetch_jobs.py --sources sources.yaml --tracker tracker.csv
    uv run fetch_jobs.py --force        # bypass the 20h paid-API cooldown
    uv run fetch_jobs.py --dedup-only   # collapse (company,role,location) dups + rescore, no fetch

Optional keyed sources (skipped gracefully unless their env vars are set):
    RAPIDAPI_KEY                 -> JSearch  (LinkedIn/Indeed/Glassdoor via Google Jobs)
    ADZUNA_APP_ID / ADZUNA_APP_KEY -> Adzuna (India + global listings)
    SERPAPI_KEY                  -> SerpApi  (Google Jobs engine)
See README.md / .env.example for setup.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import requests
import yaml

from dedup_keys import canonical_key, norm_text as _norm_text, norm_url as _canon_url
from experience import parse_experience
from job_quality import SOURCE_NAMES, accept_job, is_invalid_company, normalize_job_fields
from geo import (
    DEFAULT_MIN_SALARY_LPA,
    DEFAULT_REMOTE_FLOOR_LPA,
    DROP_RESULTS,
    KEEP_RESULTS,
    geo_salary_result,
    passes_geo_salary,
    salary_display_to_inr,
)
from fresher_filter import passes_fresher_filter, is_senior_title
from async_fetch import run_parallel_fetch
from llm_jd import enrich_job, llm_enabled

TIMEOUT = 20


def _norm_key(company, role, location, *, use_location: bool = True) -> tuple:
    """Canonical dedup identity (company, role, location).

    Location-sensitive by default so genuinely distinct city postings stay their
    own row; true duplicates still collapse via the shared canonical URL. Set
    filters.dedup_use_location: false (sources.yaml) to merge city re-listings.
    """
    return canonical_key(company, role, location, use_location=use_location)

# Polite, realistic browser User-Agent. The consumer-platform guest endpoints
# (LinkedIn / Naukri) reject the generic bot UA, so we present as a real browser
# but stay well-behaved: low page caps, delays between paginated requests.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": "job-sourcing-pipeline/1.0 (personal job search)"}
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9",
}

# Seconds to sleep between paginated/looped requests to a single host (polite).
POLITE_DELAY = 1.0

# ---- Salary normalization -------------------------------------------------
# Approximate FX rates used to convert non-INR salaries to INR purely for the
# >= min_salary_lpa filter. These are deliberately rough and easy to edit; bump
# them when the rupee moves. Unknown currencies are treated as unparseable
# (the role is then KEPT, never dropped).
USD_TO_INR = 83.0
EUR_TO_INR = 90.0
GBP_TO_INR = 105.0
CAD_TO_INR = 61.0
AUD_TO_INR = 55.0
CURRENCY_TO_INR = {
    "INR": 1.0, "RS": 1.0,
    "USD": USD_TO_INR, "EUR": EUR_TO_INR, "GBP": GBP_TO_INR,
    "CAD": CAD_TO_INR, "AUD": AUD_TO_INR,
}
CURRENCY_SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£",
                   "CAD": "C$", "AUD": "A$"}

# Approximate working hours per year (40h/week x 52) for HOUR-period salaries.
HOURS_PER_YEAR = 2080
# Multiplier to annualize a salary given its period.
PERIOD_MULTIPLIER = {
    "YEAR": 1, "YEARLY": 1, "ANNUM": 1, "ANNUAL": 1, "": 1,
    "MONTH": 12, "MONTHLY": 12,
    "WEEK": 52, "WEEKLY": 52,
    "DAY": 260, "DAILY": 260,
    "HOUR": HOURS_PER_YEAR, "HOURLY": HOURS_PER_YEAR,
}

# Salary floor defaults imported from geo.py (overridable via sources.yaml filters).

def _fmt_lpa(amount: float) -> str:
    v = amount / 1e5
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def _fmt_k(amount: float) -> str:
    v = amount / 1000
    return f"{v:.0f}k" if abs(v - round(v)) < 0.5 else f"{v:.1f}k"


def _salary_display(currency: str, lo_annual: float, hi_annual: float) -> str:
    """Human-readable annualized salary string in the original currency."""
    sym = CURRENCY_SYMBOL.get(currency, (currency + " ") if currency else "")
    if currency == "INR":
        lo, hi = _fmt_lpa(lo_annual), _fmt_lpa(hi_annual)
        return f"{sym}{hi} LPA" if lo == hi else f"{sym}{lo}-{hi} LPA"
    lo, hi = _fmt_k(lo_annual), _fmt_k(hi_annual)
    return f"{sym}{hi}/yr" if lo == hi else f"{sym}{lo}-{hi}/yr"


def normalize_salary(spec) -> tuple:
    """Convert a parsed salary spec into (display_string, annual_inr).

    `spec` is a dict {min, max, currency, period} (any field optional) or None.
    Returns ("", None) when nothing is parseable. `annual_inr` is None when the
    figure can't be converted to INR (unknown currency) - callers must KEEP such
    roles, never drop them.
    """
    if not spec:
        return "", None
    currency = (spec.get("currency") or "INR").upper()
    period = (spec.get("period") or "YEAR").upper()
    mult = PERIOD_MULTIPLIER.get(period)
    nums = []
    for v in (spec.get("min"), spec.get("max")):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            nums.append(f)
    if not nums or mult is None:
        return "", None
    lo_annual, hi_annual = min(nums) * mult, max(nums) * mult
    display = _salary_display(currency, lo_annual, hi_annual)
    rate = CURRENCY_TO_INR.get(currency)
    if rate is None:
        # Unknown currency: show what we have but leave it unparseable for filtering.
        return display, None
    # Benefit of the doubt: use the MAX of the range for the INR comparison.
    return display, hi_annual * rate


# Indian salary strings like "3-7 Lacs P.A." / "12 LPA" / "1.2 Cr" -> INR spec.
_INDIAN_SAL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(\d+(?:\.\d+)?)?\s*(lacs?|lakhs?|lpa|cr(?:ore)?s?)",
    re.IGNORECASE,
)


def parse_indian_salary_text(text: str):
    """Parse a Naukri-style salary label into a {min,max,currency,period} INR spec.

    Returns None for 'Not disclosed' / unparseable strings.
    """
    if not text:
        return None
    low = text.lower()
    if "not disclosed" in low or "unpaid" in low:
        return None
    m = _INDIAN_SAL_RE.search(low)
    if not m:
        return None
    unit = m.group(3).lower()
    scale = 1e7 if unit.startswith("cr") else 1e5  # crore vs lakh
    lo = float(m.group(1)) * scale
    hi = float(m.group(2)) * scale if m.group(2) else lo
    return {"min": lo, "max": hi, "currency": "INR", "period": "YEAR"}


class SkipSource(Exception):
    """Raised by a fetcher to skip itself with an informational (non-error) message."""


# ---- Deadline helpers ---------------------------------------------------------
# JSearch and Adzuna carry real expiry dates; use those directly.
# Every other source (per-company ATS, Muse, Remotive, RemoteOK, Arbeitnow,
# LinkedIn-guest, SerpApi, Naukri-fallback) uses a 30-day FRESHNESS PROXY:
#   deadline = post_date + 30 days
# This is NOT a guaranteed close date — it's a staleness signal. Postings older
# than 30 days are more likely to have closed; postings within 30 days are fresh.
DEADLINE_PROXY_DAYS = 30


def _parse_iso_date(s) -> str:
    """Extract YYYY-MM-DD from an ISO datetime/date string or any value; return ''."""
    if not s:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(s).strip())
    if not m:
        return ""
    try:
        dt.date.fromisoformat(m.group(1))
        return m.group(1)
    except ValueError:
        return ""


def _parse_deadline_proxy(updated_str, proxy_days: int = DEADLINE_PROXY_DAYS) -> str:
    """Return ISO date = parsed_post_date + proxy_days, or '' if unparseable.

    30-day freshness proxy — NOT a guaranteed expiry date. Sources that provide
    real deadlines (JSearch, Adzuna) should call _parse_iso_date on their real
    field instead of this function.
    """
    if not updated_str:
        return ""
    s = str(updated_str).strip()
    if not s:
        return ""
    # ISO date prefix (most API sources return ISO datetime or date)
    iso_d = _parse_iso_date(s)
    if iso_d:
        try:
            return (dt.date.fromisoformat(iso_d) + dt.timedelta(days=proxy_days)).isoformat()
        except ValueError:
            pass
    # Unix timestamp (RemoteOK / Arbeitnow sometimes return epoch seconds or ms)
    try:
        ts = float(s)
        if ts > 1e10:          # milliseconds -> seconds
            ts /= 1000
        return (dt.date.fromtimestamp(ts) + dt.timedelta(days=proxy_days)).isoformat()
    except (ValueError, OSError, OverflowError):
        pass
    # Relative human string ("2 days ago", "1 week ago") — SerpApi uses this
    rel = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", s.lower())
    if rel:
        n, unit = int(rel.group(1)), rel.group(2)
        delta = {"minute": 0, "hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit]
        d = dt.date.today() - dt.timedelta(days=delta)
        return (d + dt.timedelta(days=proxy_days)).isoformat()
    return ""


def clean(text: str) -> str:
    """Strip HTML tags + collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


_RETRY_STATUS = frozenset({429, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF = 2.0


def _request_with_retry(method: str, url: str, *, max_attempts: int = _RETRY_ATTEMPTS,
                        backoff: float = _RETRY_BACKOFF,
                        retry_status=frozenset(_RETRY_STATUS), session=None, **kwargs):
    """GET/POST with exponential backoff on transient HTTP errors and network faults."""
    caller = session if session is not None else requests
    last_exc = None
    for attempt in range(max_attempts):
        try:
            if method.upper() == "POST":
                r = caller.post(url, **kwargs)
            else:
                r = caller.get(url, **kwargs)
            if r.status_code in retry_status and attempt < max_attempts - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_attempts - 1:
                time.sleep(backoff * (attempt + 1))
            else:
                raise
    if last_exc:
        raise last_exc
    return r


def _get(url: str, params=None):
    r = _request_with_retry("GET", url, headers=HEADERS, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _get_text(url: str, params=None, headers=None, session=None):
    """GET returning the raw response (used for HTML guest endpoints)."""
    getter = session.get if session is not None else requests.get
    r = getter(url, headers=headers or BROWSER_HEADERS, params=params, timeout=TIMEOUT)
    return r


# --- per-ATS fetchers: each returns a list of {title, location, url, updated} ---
def fetch_greenhouse(token: str):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    return [
        {
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "updated": j.get("updated_at", ""),
            "description": clean(j.get("content", "")),
        }
        for j in data.get("jobs", [])
    ]


def fetch_lever(token: str):
    data = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append(
            {
                "title": j.get("text", ""),
                "location": cats.get("location", ""),
                "url": j.get("hostedUrl", ""),
                "updated": str(j.get("createdAt", "")),
                "description": clean(
                    j.get("descriptionPlain") or j.get("description", "") or ""
                ),
            }
        )
    return out


def fetch_ashby(token: str):
    data = _get(
        f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    )
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl", ""),
            "updated": j.get("publishedAt", "") or "",
            "description": clean(
                j.get("descriptionPlain") or j.get("descriptionHtml", "") or ""
            ),
        }
        for j in data.get("jobs", [])
    ]


def fetch_workable(token: str):
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city", ""), j.get("country", "")]))
        out.append(
            {
                "title": j.get("title", ""),
                "location": loc,
                "url": j.get("url", "") or j.get("application_url", ""),
                "updated": j.get("published_on", "") or "",
            }
        )
    return out


def fetch_recruitee(token: str):
    data = _get(f"https://{token}.recruitee.com/api/offers")
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("location", "") or j.get("city", ""),
            "url": j.get("careers_url", "") or j.get("url", ""),
            "updated": j.get("published_at", "") or "",
        }
        for j in data.get("offers", [])
    ]


def fetch_workday(token: str):
    """Workday CXS API — token format: host/tenant/site
    e.g. browserstack.wd3/browserstack/External"""
    parts = (token or "").split("/")
    if len(parts) != 3:
        raise ValueError(
            f"workday token must be host/tenant/site, got {token!r}"
        )
    host, tenant, site = parts
    api_url = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    base_url = f"https://{host}.myworkdayjobs.com/en-US/{site}"
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    # Workday CXS requires a session cookie from the public careers page.
    s.get(base_url, timeout=TIMEOUT)
    out = []
    offset = 0
    limit = 20
    while True:
        r = _request_with_retry(
            "POST", api_url,
            json={"appliedFacets": {}, "limit": limit, "offset": offset,
                  "searchText": ""},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
            timeout=TIMEOUT,
            session=s,
        )
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            out.append({
                "title": j.get("title", ""),
                "location": j.get("locationsText", "") or "",
                "url": f"{base_url}{path}" if path else "",
                "updated": j.get("postedOn", "") or "",
            })
        offset += len(postings)
        if offset >= int(data.get("total") or 0):
            break
        time.sleep(POLITE_DELAY)
    return out


def fetch_smartrecruiters(token: str):
    """SmartRecruiters public postings API — token is the company identifier."""
    out = []
    offset = 0
    limit = 100
    while True:
        r = _request_with_retry(
            "GET",
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            headers=HEADERS,
            params={"offset": offset, "limit": limit},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        content = data.get("content") or []
        if not content:
            break
        for j in content:
            jid = j.get("id", "")
            loc = (j.get("location") or {}).get("fullLocation", "")
            out.append({
                "title": j.get("name", ""),
                "location": loc,
                "url": f"https://jobs.smartrecruiters.com/{token}/{jid}",
                "updated": j.get("releasedDate", "") or "",
            })
        offset += len(content)
        total = int(data.get("totalFound") or offset)
        if offset >= total:
            break
        time.sleep(POLITE_DELAY)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
}


# --- aggregator fetchers --------------------------------------------------
# Unlike the per-company ATS fetchers above, these index roles across MANY
# employers, so each returned row carries its own "company" name. Each takes its
# config dict from sources.yaml and returns a list of
#   {company, title, location, url, updated}.
def fetch_themuse(cfg: dict):
    """The Muse public jobs API - free, no key. Strong for entry-level roles."""
    categories = cfg.get("categories") or [
        "Software Engineering", "Data Science", "Data and Analytics",
        "Computer and IT",
    ]
    levels = cfg.get("levels") or ["Entry Level"]
    pages = int(cfg.get("pages", 3))
    out = []
    for page in range(pages):
        params = [("page", page)]
        params += [("category", c) for c in categories]
        params += [("level", lv) for lv in levels]
        data = _get("https://www.themuse.com/api/public/jobs", params)
        results = data.get("results") or []
        if not results:
            break
        for j in results:
            locs = ", ".join(
                (loc or {}).get("name", "") for loc in (j.get("locations") or [])
            )
            out.append(
                {
                    "company": (j.get("company") or {}).get("name", ""),
                    "title": j.get("name", ""),
                    "location": locs,
                    "url": (j.get("refs") or {}).get("landing_page", ""),
                    "updated": j.get("publication_date", "") or "",
                }
            )
    return out


def fetch_remotive(cfg: dict):
    """Remotive remote-jobs API - free, no key. Remote-first, so empty -> Remote."""
    categories = cfg.get("categories") or ["software-dev"]
    out = []
    for cat in categories:
        data = _get("https://remotive.com/api/remote-jobs", {"category": cat})
        for j in data.get("jobs", []):
            out.append(
                {
                    "company": j.get("company_name", ""),
                    "title": j.get("title", ""),
                    "location": j.get("candidate_required_location", "") or "Remote",
                    "url": j.get("url", ""),
                    "updated": j.get("publication_date", "") or "",
                    "remote": True,  # Remotive is a remote-only board
                }
            )
    return out


def fetch_remoteok(cfg: dict):
    """RemoteOK API - free, no key. First array element is a legal/metadata blob."""
    data = _get("https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or "legal" in j:  # skip the metadata element
            continue
        out.append(
            {
                "company": j.get("company", ""),
                "title": j.get("position", "") or j.get("title", ""),
                "location": j.get("location", "") or "Remote",
                "url": j.get("url", ""),
                "updated": str(j.get("date", "")),
                "remote": True,  # RemoteOK is a remote-only board
            }
        )
    return out


def fetch_arbeitnow(cfg: dict):
    """Arbeitnow job-board API - free, no key."""
    data = _get("https://www.arbeitnow.com/api/job-board-api")
    out = []
    for j in data.get("data", []):
        loc = j.get("location", "")
        if not loc and j.get("remote"):
            loc = "Remote"
        out.append(
            {
                "company": j.get("company_name", ""),
                "title": j.get("title", ""),
                "location": loc,
                "url": j.get("url", ""),
                "updated": str(j.get("created_at", "")),
                "remote": bool(j.get("remote")),
            }
        )
    return out


def fetch_weworkremotely(cfg: dict):
    """We Work Remotely RSS feeds — free, no key, global remote-first startups."""
    import xml.etree.ElementTree as ET

    default_feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    ]
    feeds = cfg.get("feeds") or default_feeds
    out: list[dict] = []
    for feed_url in feeds:
        try:
            r = requests.get(feed_url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            continue
        channel = root.find("channel") or root
        for item in channel.findall("item"):
            title_raw = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title_raw or not link:
                continue
            company, _, title = title_raw.partition(": ")
            if not title:
                title, company = title_raw, company or ""
            region = (item.findtext("{https://weworkremotely.com}region") or "").strip()
            location = region or "Remote"
            out.append(
                {
                    "company": company.strip(),
                    "title": title.strip(),
                    "location": location,
                    "url": link,
                    "updated": (item.findtext("pubDate") or "").strip(),
                    "remote": True,
                }
            )
        time.sleep(POLITE_DELAY)
    return _quality_filter_jobs(out, "weworkremotely")


def fetch_adzuna(cfg: dict):
    """Adzuna search API - free but key-based. Country 'in' covers Naukri/Indeed-style
    India listings. Skips gracefully unless ADZUNA_APP_ID / ADZUNA_APP_KEY are set."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise SkipSource(
            "ADZUNA_APP_ID / ADZUNA_APP_KEY not set - optional source skipped "
            "(get free keys at https://developer.adzuna.com/)"
        )
    countries = cfg.get("countries") or ["gb"]
    queries = cfg.get("queries") or [
        "remote software engineer worldwide",
        "remote backend developer work from anywhere",
        "junior software engineer remote global",
    ]
    rpp = int(cfg.get("results_per_page", 40))
    max_calls = int(cfg.get("max_calls", 0))  # 0 = no cap
    remote_query_markers = ("remote", "worldwide", "anywhere", "work from anywhere", "global")
    # Adzuna salaries are annual figures in the country's local currency.
    country_currency = {"in": "INR", "gb": "GBP", "us": "USD", "ca": "CAD",
                        "au": "AUD", "de": "EUR", "fr": "EUR", "nl": "EUR",
                        "es": "EUR", "it": "EUR", "sg": "SGD"}
    out = []
    calls = 0
    for country in countries:
        cur = country_currency.get(country, country.upper())
        for what in queries:
            if max_calls and calls >= max_calls:
                break
            data = _get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                {
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": what,
                    "results_per_page": rpp,
                },
            )
            calls += 1
            remote_hint = any(m in what.lower() for m in remote_query_markers)
            for j in data.get("results", []):
                salary = None
                if j.get("salary_min") or j.get("salary_max"):
                    salary = {
                        "min": j.get("salary_min"),
                        "max": j.get("salary_max"),
                        "currency": cur,
                        "period": "YEAR",
                    }
                loc = (j.get("location") or {}).get("display_name", "")
                if remote_hint and loc and "remote" not in loc.lower():
                    loc = f"Remote — {loc}"
                out.append(
                    {
                        "company": (j.get("company") or {}).get("display_name", ""),
                        "title": j.get("title", ""),
                        "location": loc or ("Remote" if remote_hint else ""),
                        "url": j.get("redirect_url", ""),
                        "updated": j.get("created", "") or "",
                        "salary": salary,
                        "remote": remote_hint,
                        "deadline": (
                            _parse_iso_date(j.get("expiration_date"))
                            or _parse_deadline_proxy(j.get("created", "") or "")
                        ),
                    }
                )
        if max_calls and calls >= max_calls:
            break
    return out


def _serpapi_on_cooldown(fetch_state: dict | None) -> bool:
    """True when the main serpapi aggregator ran within COOLDOWN_HOURS."""
    if not fetch_state:
        return False
    on_cd, _ = _is_on_cooldown(fetch_state, "serpapi")
    return on_cd


def _jsearch_on_cooldown(fetch_state: dict | None) -> bool:
    if not fetch_state:
        return False
    on_cd, _ = _is_on_cooldown(fetch_state, "jsearch")
    return on_cd


def _serpapi_organic_jobs(
    cfg: dict,
    *,
    default_queries: list[str],
    site_substring: str,
    url_path_markers: tuple[str, ...] = ("/",),
    default_location: str = "India",
    source_label: str = "site search",
    source_name: str = "",
    fetch_state: dict | None = None,
    force_serpapi: bool = False,
) -> list[dict]:
    """Shared SerpApi Google organic search for site:-scoped job boards."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        raise SkipSource(
            f"SERPAPI_KEY not set — required for {source_label}."
        )
    if not force_serpapi and not cfg.get("_force") and _serpapi_on_cooldown(fetch_state):
        raise SkipSource(
            f"SerpApi on {COOLDOWN_HOURS}h cooldown — {source_label} deferred."
        )
    queries = cfg.get("serpapi_queries") or cfg.get("queries") or default_queries
    gl = cfg.get("gl", "in")
    src = source_name or site_substring.split(".")[0]
    out: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries:
        r = _request_with_retry(
            "GET", "https://serpapi.com/search",
            params={"engine": "google", "q": q, "hl": "en", "gl": gl,
                    "api_key": key, "num": 20},
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("organic_results", []) or []:
            url = (item.get("link") or "").split("?")[0]
            if not url or site_substring not in url or url in seen_urls:
                continue
            if url_path_markers and not any(m in url for m in url_path_markers):
                continue
            title = item.get("title", "")
            snippet = item.get("snippet", "") or ""
            company, role = normalize_job_fields("", title, url, src)
            ok, _reason = accept_job(
                company, role, url, src, description=snippet,
            )
            if not ok:
                continue
            seen_urls.add(url)
            out.append({
                "company": company,
                "title": role,
                "location": default_location,
                "url": url,
                "updated": item.get("date", "") or "",
                "description": snippet,
            })
        time.sleep(POLITE_DELAY)
    return out


def fetch_jsearch(cfg: dict):
    """JSearch (RapidAPI) - aggregates Google for Jobs, which indexes LinkedIn,
    Indeed, Glassdoor, ZipRecruiter and more. This is our legitimate, reliable
    route to those big platforms. OPTIONAL: needs RAPIDAPI_KEY (free tier at
    https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch); skips if unset.

    Docs: GET https://jsearch.p.rapidapi.com/search
      params: query, page, num_pages, date_posted (all|today|3days|week|month)
      headers: X-RapidAPI-Key, X-RapidAPI-Host: jsearch.p.rapidapi.com
    """
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        raise SkipSource(
            "RAPIDAPI_KEY not set - optional source skipped (free key at "
            "rapidapi.com -> JSearch by Letscrape). This is the LinkedIn/Indeed feed."
        )
    host = "jsearch.p.rapidapi.com"
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    queries = cfg.get("queries") or [
        "software engineer fresher in India",
        "backend developer entry level in India",
        "machine learning engineer fresher in India",
        "graduate software engineer India",
        "junior software engineer remote",
        "backend engineer entry level remote",
    ]
    num_pages = int(cfg.get("num_pages", 1))
    date_posted = cfg.get("date_posted", "month")
    out = []
    for q in queries:
        r = _request_with_retry(
            "GET", f"https://{host}/search",
            headers=headers,
            params={"query": q, "page": "1", "num_pages": str(num_pages),
                    "date_posted": date_posted},
            timeout=60,
        )
        r.raise_for_status()
        for j in r.json().get("data", []) or []:
            loc = ", ".join(
                filter(None, [j.get("job_city"), j.get("job_state"),
                              j.get("job_country")])
            ) or ("Remote" if j.get("job_is_remote") else "")
            url = j.get("job_apply_link") or j.get("job_google_link") or ""
            salary = None
            if j.get("job_min_salary") or j.get("job_max_salary"):
                salary = {
                    "min": j.get("job_min_salary"),
                    "max": j.get("job_max_salary"),
                    "currency": j.get("job_salary_currency") or "USD",
                    "period": j.get("job_salary_period") or "YEAR",
                }
            out.append(
                {
                    "company": j.get("employer_name", ""),
                    "title": j.get("job_title", ""),
                    "location": loc,
                    "url": url,
                    "updated": j.get("job_posted_at_datetime_utc", "") or "",
                    "salary": salary,
                    "remote": bool(j.get("job_is_remote")),
                    "deadline": _parse_iso_date(j.get("job_offer_expiration_datetime")),
                    "description": clean(j.get("job_description", "") or ""),
                }
            )
        time.sleep(POLITE_DELAY)
    return _quality_filter_jobs(out, "jsearch")


def fetch_serpapi(cfg: dict):
    """SerpApi Google Jobs engine - an alternate paid/keyed route to Google for
    Jobs (LinkedIn / Indeed / Glassdoor / etc). OPTIONAL: needs SERPAPI_KEY
    (https://serpapi.com/); skips gracefully if unset."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        raise SkipSource(
            "SERPAPI_KEY not set - optional source skipped (key at serpapi.com)."
        )
    queries = cfg.get("queries") or [
        "software engineer fresher india", "backend developer entry level india",
        "machine learning engineer fresher india",
    ]
    gl = cfg.get("gl", "in")
    out = []
    for q in queries:
        r = _request_with_retry(
            "GET", "https://serpapi.com/search",
            params={"engine": "google_jobs", "q": q, "hl": "en", "gl": gl,
                    "api_key": key},
            timeout=30,
        )
        r.raise_for_status()
        for j in r.json().get("jobs_results", []) or []:
            opts = j.get("apply_options") or []
            url = (opts[0].get("link") if opts else "") or j.get("share_link", "")
            posted = (j.get("detected_extensions") or {}).get("posted_at", "")
            out.append(
                {
                    "company": j.get("company_name", ""),
                    "title": j.get("title", ""),
                    "location": j.get("location", ""),
                    "url": url,
                    "updated": posted,
                }
            )
        time.sleep(POLITE_DELAY)
    return _quality_filter_jobs(out, "serpapi")


def _quality_filter_jobs(jobs: list[dict], source: str) -> list[dict]:
    """Drop category pages, spam domains, and junk titles; normalize company/role."""
    out: list[dict] = []
    for j in jobs:
        url = j.get("url") or ""
        co = j.get("company") or ""
        title = j.get("title") or ""
        desc = j.get("description") or ""
        ok, _reason = accept_job(co, title, url, source, description=desc)
        if not ok:
            continue
        co, title = normalize_job_fields(co, title, url, source)
        row = dict(j)
        row["company"] = co
        row["title"] = title
        out.append(row)
    return out


def fetch_naukri(cfg: dict):
    """Naukri — jobapi → RSS → Playwright → JSearch → SerpApi (residential-friendly order)."""
    errors: list[str] = []
    skip_pw = os.environ.get("NAUKRI_SKIP_PLAYWRIGHT", "").strip() in ("1", "true", "yes")

    # 1) Direct jobapi — fast on residential when Nkparam valid (set NAUKRI_NKPARAM in .env).
    try:
        from naukri_api import fetch_naukri_api
        out = _quality_filter_jobs(fetch_naukri_api(cfg), "naukri")
        if out:
            return out
        errors.append("jobapi returned 0 valid jobs")
    except SkipSource as e:
        errors.append(f"jobapi: {e}")

    # 2) RSS — often works on residential IP.
    try:
        out = _quality_filter_jobs(_fetch_naukri_rss(cfg), "naukri")
        if out:
            return out
        errors.append("RSS returned 0 valid jobs")
    except SkipSource as e:
        errors.append(f"RSS: {e}")

    # 3) Playwright intercept — fallback; auto-retries headed if headless blocked.
    if not skip_pw:
        try:
            from board_playwright import fetch_naukri_playwright_intercept, _playwright_available
            if _playwright_available():
                out = _quality_filter_jobs(fetch_naukri_playwright_intercept(cfg), "naukri")
                if out:
                    return out
                errors.append("Playwright intercept returned 0 jobs")
        except SkipSource as e:
            errors.append(f"Playwright: {e}")
        except ImportError:
            errors.append("Playwright not installed")
    rapid_key = os.environ.get("RAPIDAPI_KEY")
    if rapid_key and (cfg.get("_force") or not _jsearch_on_cooldown(cfg.get("_fetch_state"))):
        try:
            out = _quality_filter_jobs(_fetch_naukri_jsearch(cfg, rapid_key), "naukri")
            if out:
                return out
            errors.append("JSearch returned 0 valid Naukri jobs")
        except Exception as e:
            errors.append(f"JSearch: {type(e).__name__}")

    # 6) SerpApi — inurl:job-listings only.
    if os.environ.get("SERPAPI_KEY"):
        try:
            out = _serpapi_organic_jobs(
                cfg,
                default_queries=[
                    "site:naukri.com inurl:job-listings backend developer bangalore",
                    "site:naukri.com inurl:job-listings software engineer india",
                    "site:naukri.com inurl:job-listings machine learning fresher",
                ],
                site_substring="naukri.com",
                url_path_markers=("job-listings-", "job-details/"),
                source_label="Naukri site search",
                source_name="naukri",
                fetch_state=cfg.get("_fetch_state"),
                force_serpapi=True,
            )
            if out:
                return out
            errors.append("SerpApi returned 0 valid Naukri jobs")
        except SkipSource as e:
            errors.append(f"SerpApi: {e}")

    raise SkipSource(
        "Naukri exhausted all routes (" + "; ".join(errors) + "). "
        "Try: uv pip install playwright && playwright install chromium "
        "&& PLAYWRIGHT_HEADED=1 uv run fetch_jobs.py"
    )


def _fetch_naukri_jsearch(cfg: dict, rapid_key: str) -> list[dict]:
    """Pull Naukri postings surfaced by JSearch (Google for Jobs)."""
    keywords = cfg.get("keywords") or [
        "software engineer", "backend developer",
        "machine learning engineer", "sde",
    ]
    host = "jsearch.p.rapidapi.com"
    headers = {"X-RapidAPI-Key": rapid_key, "X-RapidAPI-Host": host}
    out: list[dict] = []
    seen: set[str] = set()
    for kw in keywords:
        q = f"{kw} naukri india"
        r = _request_with_retry(
            "GET", f"https://{host}/search",
            headers=headers,
            params={"query": q, "page": "1", "num_pages": "1",
                    "date_posted": "month"},
            timeout=60,
        )
        r.raise_for_status()
        for j in r.json().get("data", []) or []:
            url = j.get("job_apply_link") or j.get("job_google_link") or ""
            if not url or "naukri.com" not in url or url in seen:
                continue
            seen.add(url)
            loc = ", ".join(
                filter(None, [j.get("job_city"), j.get("job_state"),
                              j.get("job_country")])
            )
            out.append({
                "company": j.get("employer_name", ""),
                "title": j.get("job_title", ""),
                "location": loc or "India",
                "url": url,
                "updated": j.get("job_posted_at_datetime_utc", "") or "",
                "description": clean(j.get("job_description", "") or ""),
            })
        time.sleep(POLITE_DELAY)
    return out


def _fetch_naukri_rss(cfg: dict) -> list[dict]:
    """Naukri public RSS feed (no login). Raises SkipSource on captcha/HTML."""
    import xml.etree.ElementTree as ET

    keywords = cfg.get("keywords") or [
        "software engineer", "backend developer",
        "machine learning engineer", "sde",
    ]
    experience = int(cfg.get("experience", 0))
    rpp = int(cfg.get("results_per_keyword", 50))
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    s.headers.update({"Accept": "application/rss+xml, application/xml"})
    out = []
    for kw in keywords:
        params = {
            "noOfResults": rpp,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": kw,
            "experience": experience,
        }
        try:
            r = s.get(
                "https://www.naukri.com/rss/jobsearch.php",
                params=params,
                timeout=TIMEOUT,
            )
        except Exception as e:
            raise SkipSource(
                f"Naukri RSS fetch failed ({type(e).__name__}: {e})"
            )
        if r.status_code != 200:
            raise SkipSource(
                f"Naukri RSS returned HTTP {r.status_code} for keyword '{kw}'. "
                "RSS feed temporarily unavailable."
            )
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            raise SkipSource(
                f"Naukri RSS XML parse error: {e} "
                f"(first 200 chars: {r.text[:200]!r})"
            )
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            date_el = item.find("pubDate")

            raw_title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            pub_date = (date_el.text or "").strip() if date_el is not None else ""
            desc_text = clean((desc_el.text or "") if desc_el is not None else "")

            # Title format is usually "Job Title - Company Name"
            if " - " in raw_title:
                title, company = raw_title.split(" - ", 1)
                title, company = title.strip(), company.strip()
            else:
                title, company = raw_title, ""

            # Location: look for "Location :" pattern in description HTML
            location = ""
            loc_m = re.search(r"Location\s*:\s*([^|<\n]+)", desc_text, re.IGNORECASE)
            if loc_m:
                location = loc_m.group(1).strip()

            # Company fallback: look for "Company :" in description
            if not company:
                comp_m = re.search(r"Company\s*:\s*([^|<\n]+)", desc_text, re.IGNORECASE)
                if comp_m:
                    company = comp_m.group(1).strip()

            out.append({
                "company": company,
                "title": title,
                "location": location,
                "url": link,
                "updated": pub_date,
                "description": desc_text,
            })
        time.sleep(POLITE_DELAY)
    return out


def fetch_linkedin_guest(cfg: dict):
    """LinkedIn public *guest* job search (NO login) - the unauthenticated
    jobs-guest endpoint that powers public job cards. We parse the returned HTML
    cards politely (low page cap + delay). f_E experience filter: 1=internship,
    2=entry, 3=associate, 4=mid-senior.

    This is a public-guest endpoint with no auth and no personal account involved,
    but LinkedIn can change/rate-limit it; we cap pages and degrade gracefully.
    """
    from bs4 import BeautifulSoup

    queries = cfg.get("queries") or [
        "software engineer", "backend developer",
        "machine learning engineer", "sde",
    ]
    locations = cfg.get("locations") or ["India"]
    f_e = str(cfg.get("experience_levels", "1,2,3"))
    pages = int(cfg.get("pages", 2))
    endpoint = (
        "https://www.linkedin.com/jobs-guest/jobs/api/"
        "seeMoreJobPostings/search"
    )
    out, seen_urns, block_reason = [], set(), None
    for location in locations:
        for q in queries:
            for page in range(pages):
                params = {"keywords": q, "location": location, "f_E": f_e,
                          "start": page * 10}
                try:
                    r = requests.get(endpoint, headers=BROWSER_HEADERS,
                                     params=params, timeout=TIMEOUT)
                except Exception as e:
                    block_reason = f"{type(e).__name__}"
                    break
                if r.status_code != 200:
                    block_reason = f"HTTP {r.status_code}"
                    break
                soup = BeautifulSoup(r.text, "html.parser")
                cards = soup.select("li")
                if not cards:
                    break
                for li in cards:
                    a = li.select_one("a.base-card__full-link") or li.select_one("a")
                    title = li.select_one("h3.base-search-card__title")
                    company = li.select_one("h4.base-search-card__subtitle")
                    loc = li.select_one(".job-search-card__location")
                    urn = li.select_one("[data-entity-urn]")
                    if not (a and title):
                        continue
                    key = (urn or {}).get("data-entity-urn") if urn else None
                    url = (a.get("href") or "").split("?")[0]
                    dedupe_key = key or url
                    if dedupe_key in seen_urns:
                        continue
                    seen_urns.add(dedupe_key)
                    out.append(
                        {
                            "company": company.get_text(strip=True) if company else "",
                            "title": title.get_text(strip=True),
                            "location": loc.get_text(strip=True) if loc else "",
                            "url": url,
                            "updated": "",
                        }
                    )
                time.sleep(POLITE_DELAY)
    if not out and block_reason:
        raise SkipSource(
            f"LinkedIn guest endpoint returned {block_reason} (public endpoint "
            "can rate-limit/change). Fallback: JSearch covers LinkedIn via Google Jobs."
        )
    return out


def fetch_wellfound(cfg: dict):
    """Wellfound — SerpApi first (reliable), then Playwright Apollo fallback."""
    errors: list[str] = []

    # 1) SerpApi — proven on VPN/residential; uses queries from sources.yaml.
    if os.environ.get("SERPAPI_KEY"):
        try:
            out = _serpapi_organic_jobs(
                cfg,
                default_queries=[
                    "site:wellfound.com/jobs backend engineer india",
                    "site:wellfound.com/jobs software engineer bangalore startup",
                    "site:wellfound.com/jobs machine learning engineer india",
                ],
                site_substring="wellfound.com",
                url_path_markers=("/jobs/",),
                source_label="Wellfound site search",
                source_name="wellfound",
                fetch_state=cfg.get("_fetch_state"),
                force_serpapi=True,
            )
            if out:
                return _quality_filter_jobs(out, "wellfound")
            errors.append("SerpApi returned 0 valid jobs")
        except SkipSource as e:
            errors.append(f"SerpApi: {e}")
    else:
        errors.append("SERPAPI_KEY not set")

    # 2) Playwright — when SerpApi empty and browser available.
    try:
        from board_playwright import fetch_wellfound_playwright, _playwright_available
        if _playwright_available():
            out = _quality_filter_jobs(fetch_wellfound_playwright(cfg), "wellfound")
            if out:
                return out
            errors.append("Playwright returned 0 jobs")
    except SkipSource as e:
        errors.append(f"Playwright: {e}")

    if errors:
        print(f"  [wellfound] {'; '.join(errors)}")
    return []


def fetch_hirist(cfg: dict):
    """Hirist — official jobfeed API (correct params), Playwright intercept, SerpApi."""
    keywords = cfg.get("keywords") or [
        "software engineer", "backend", "machine learning", "python", "golang",
    ]
    pages = int(cfg.get("pages", 2))
    loc = str(cfg.get("loc", ""))  # e.g. "17" = Bangalore; empty = all India
    api_url = "https://jobseeker-api.hirist.com/jobfeed/-1/search"
    headers = {
        **BROWSER_HEADERS,
        "Host": "jobseeker-api.hirist.com",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.hirist.com",
        "Referer": "https://www.hirist.com/",
        "Authorization": "Bearer undefined",
    }
    out: list[dict] = []
    api_errors: list[str] = []

    for kw in keywords:
        for page in range(pages):
            params = {
                "pageNo": str(page),
                "query": kw,
                "loc": loc,
                "minexp": "0",
                "maxexp": "0",
                "range": "0",
                "boost": "0",
                "searchRange": "4",
                "searchOp": "AND",
                "jobType": "1",
            }
            try:
                r = _request_with_retry(
                    "GET", api_url, headers=headers, params=params, timeout=TIMEOUT,
                )
            except Exception as e:
                api_errors.append(type(e).__name__)
                break
            if r.status_code in (403, 503, 502, 429):
                api_errors.append(f"HTTP {r.status_code}")
                break
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except ValueError:
                api_errors.append("non-JSON")
                break
            jobs = data if isinstance(data, list) else (data.get("jobs") or [])
            if not jobs:
                break
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                title = j.get("title") or j.get("jobTitle") or ""
                company = j.get("companyName") or j.get("company") or ""
                url = j.get("jobDetailUrl") or j.get("jobUrl") or j.get("url") or ""
                if url and not url.startswith("http"):
                    url = f"https://www.hirist.com{url}"
                desc = clean(j.get("description") or j.get("jobDescription") or "")
                out.append({
                    "company": str(company or ""),
                    "title": str(title),
                    "location": str(j.get("location") or "India"),
                    "url": url,
                    "updated": str(j.get("createdAt") or j.get("postedDate") or ""),
                    "description": desc,
                })
            time.sleep(POLITE_DELAY)

    if out:
        return _quality_filter_jobs(out, "hirist")

    # SerpApi — PROVEN when API 503 (individual hirist.com/j/ URLs).
    if os.environ.get("SERPAPI_KEY"):
        print(f"  [hirist] API failed ({', '.join(api_errors)}); trying SerpApi site:hirist.com/j/.")
        try:
            serp_out = _serpapi_organic_jobs(
                cfg,
                default_queries=[
                    "site:hirist.com/j/ backend developer bangalore",
                    "site:hirist.com/j/ software engineer india",
                    "site:hirist.com/j/ machine learning engineer",
                    "site:hirist.com/j/ python developer",
                ],
                site_substring="hirist.com",
                url_path_markers=("/j/",),
                source_label="Hirist site search",
                source_name="hirist",
                fetch_state=cfg.get("_fetch_state"),
                force_serpapi=True,  # board fetchers bypass main-aggregator cooldown
            )
            if serp_out:
                return serp_out
            api_errors.append("SerpApi returned 0 valid jobs")
        except SkipSource as e:
            api_errors.append(str(e))

    # Playwright intercept (works when browser can reach jobseeker-api).
    try:
        from board_playwright import fetch_hirist_playwright, _playwright_available
        if _playwright_available():
            out = _quality_filter_jobs(fetch_hirist_playwright(cfg), "hirist")
            if out:
                return out
            api_errors.append("Playwright returned 0 jobs")
    except SkipSource as e:
        api_errors.append(str(e))

    raise SkipSource(
        "Hirist exhausted all routes (" + "; ".join(api_errors) + ")."
    )


def fetch_cutshort(cfg: dict):
    """Cutshort — city __NEXT_DATA__ (proven HTTP), then Playwright, then SerpApi."""
    errors: list[str] = []

    try:
        from cutshort_nextdata import fetch_cutshort_nextdata
        out = _quality_filter_jobs(fetch_cutshort_nextdata(cfg), "cutshort")
        if out:
            return out
        errors.append("city __NEXT_DATA__ returned 0 valid jobs")
    except SkipSource as e:
        errors.append(f"city __NEXT_DATA__: {e}")

    try:
        from board_playwright import fetch_cutshort_playwright, _playwright_available
        if _playwright_available():
            out = _quality_filter_jobs(fetch_cutshort_playwright(cfg), "cutshort")
            if out:
                return out
            errors.append("Playwright returned 0 jobs")
    except SkipSource as e:
        errors.append(f"Playwright: {e}")

    try:
        return _fetch_cutshort_serpapi(cfg)
    except SkipSource as e:
        errors.append(str(e))

    raise SkipSource("Cutshort exhausted: " + "; ".join(errors))


def _fetch_cutshort_serpapi(cfg: dict) -> list[dict]:
    """Cutshort individual postings via SerpApi site:cutshort.io/job/."""
    out = _serpapi_organic_jobs(
        cfg,
        default_queries=[
            "site:cutshort.io/job backend engineer bangalore",
            "site:cutshort.io/job machine learning engineer india",
            "site:cutshort.io/job software engineer startup",
        ],
        site_substring="cutshort.io",
        url_path_markers=("/job/",),
        source_label="Cutshort site search",
        source_name="cutshort",
        fetch_state=cfg.get("_fetch_state"),
        force_serpapi=True,
    )
    if not out:
        raise SkipSource(
            "Cutshort SerpApi returned 0 individual job postings."
        )
    return out


AGGREGATOR_FETCHERS = {
    "themuse": fetch_themuse,
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "weworkremotely": fetch_weworkremotely,
    "adzuna": fetch_adzuna,
    "jsearch": fetch_jsearch,
    "serpapi": fetch_serpapi,
    "naukri": fetch_naukri,
    "linkedin_guest": fetch_linkedin_guest,
    "wellfound": fetch_wellfound,
    "hirist": fetch_hirist,
    "cutshort": fetch_cutshort,
}

# Paid aggregators that consume API credits on each call — subject to cooldown.
PAID_SOURCES = frozenset({"jsearch", "adzuna", "serpapi"})
COOLDOWN_HOURS = 20

# Used when sources.yaml has no `aggregators:` block, so the pipeline still works.
DEFAULT_AGGREGATORS = [
    {
        "source": "themuse",
        "categories": [
            "Software Engineering", "Data Science", "Data and Analytics",
            "Computer and IT",
        ],
        "levels": ["Entry Level"],
        "pages": 3,
    },
    {"source": "remotive", "categories": ["software-dev"]},
    {"source": "remoteok"},
    {"source": "arbeitnow"},
    {
        "source": "adzuna",
        "countries": ["in", "gb", "us"],
        "queries": [
            "software engineer", "backend developer",
            "machine learning engineer", "graduate engineer",
        ],
        "results_per_page": 50,
    },
    {"source": "jsearch", "num_pages": 1, "date_posted": "month"},
    {"source": "serpapi", "gl": "in"},
    {"source": "naukri", "experience": 0, "results_per_keyword": 40},
    {"source": "linkedin_guest", "locations": ["India"], "pages": 2,
     "experience_levels": "1,2,3"},
    {"source": "wellfound"},
    {"source": "hirist", "pages": 2},
    {"source": "cutshort"},
]

_V1_FIELDS = [
    "date_found", "company", "role", "location", "salary", "source", "url", "status",
    "referral_contact", "resume_variant", "notes", "next_action_date",
]

# Canonical 26-column schema. `stage` and `url` sit right after `score` so the
# most actionable columns are visible without horizontal scrolling in the Sheet.
# sheets_sync.py FIELDS mirrors this EXACT order. Every script reads/writes by
# column NAME (DictReader/DictWriter), never by positional index, so order
# changes can never misalign data.
FIELDS = [
    "date_found", "company", "score", "stage", "url", "role", "location",
    "salary", "deadline", "source", "applied_date", "contact_name",
    "contact_email", "job_id", "resume_variant", "referral_contact", "oa_date",
    "phone_date", "tech_date", "onsite_date", "offer_details", "next_action",
    "next_action_date", "notes", "exp_years", "exp_match", "link_status",
]


def matches(title: str, location: str, filters: dict) -> bool:
    t = (title or "").lower()
    include = [k.lower() for k in (filters.get("title_include") or [])]
    exclude = [k.lower() for k in (filters.get("title_exclude") or [])]
    if include and not any(k in t for k in include):
        return False
    if any(k in t for k in exclude):
        return False
    locs = [k.lower() for k in (filters.get("location_include") or [])]
    if locs:
        loc = (location or "").lower()
        if not any(k in loc for k in locs):
            return False
    return True


# Geo / remote / salary rules live in geo.py (shared with score.py).
_geo_salary_result = geo_salary_result


def _inr_from_display(disp: str):
    return salary_display_to_inr(disp, usd_to_inr=USD_TO_INR)


def classify_job(job: dict, min_lpa_inr: float, remote_floor_inr: float):
    """Run a freshly-fetched job dict through normalize_salary + the geo/salary
    keep-drop rules. Returns (result, salary_display)."""
    salary_display, annual_inr = normalize_salary(job.get("salary"))
    sal = job.get("salary") if isinstance(job.get("salary"), dict) else {}
    currency = sal.get("currency", "") if isinstance(sal, dict) else ""
    result = _geo_salary_result(
        job.get("title", ""), job.get("location", ""), annual_inr,
        min_lpa_inr, remote_floor_inr,
        description=job.get("description", ""),
        remote_flag=bool(job.get("remote")),
        salary_display=salary_display,
        salary_currency=currency,
    )
    return result, salary_display


def infer_resume_variant(role: str) -> str:
    """Pick resume variant from role title keywords."""
    t = (role or "").lower()
    if any(k in t for k in ("machine learning", "ml engineer", "data science",
                            " ai ", "llm", "deep learning", "nlp")):
        return "ai_platform"
    if any(k in t for k in ("backend", "back end", "api", "platform", "infra",
                            "infrastructure", "distributed", "golang", "go ")):
        return "backend"
    return "master"


def make_row(company: str, role: str, location: str, source: str, url: str,
             salary: str = "", deadline: str = "", description: str = "",
             *, llm_enrichment: dict | None = None) -> dict:
    exp_years, exp_match = parse_experience(role, description)
    if llm_enrichment:
        if llm_enrichment.get("exp_years") is not None:
            exp_years = llm_enrichment["exp_years"]
        if llm_enrichment.get("exp_match"):
            exp_match = llm_enrichment["exp_match"]
    exp_years_str = "" if exp_years is None else (
        str(int(exp_years)) if exp_years == int(exp_years) else str(exp_years)
    )
    variant = infer_resume_variant(role)
    if llm_enrichment and llm_enrichment.get("resume_variant"):
        variant = llm_enrichment["resume_variant"]
    notes = ""
    if llm_enrichment and llm_enrichment.get("note"):
        fit = llm_enrichment.get("llm_fit")
        notes = llm_enrichment["note"]
        if fit is not None:
            notes = f"[fit:{fit}] {notes}"
        notes = notes[:200]
    return {
        "date_found": dt.date.today().isoformat(),
        "company": clean(company),
        "score": "",
        "role": clean(role),
        "location": clean(location),
        "salary": salary,
        "deadline": deadline,
        "source": source,
        "url": url,
        "stage": "sourced",
        "applied_date": "",
        "contact_name": "",
        "contact_email": "",
        "job_id": "",
        "resume_variant": variant,
        "referral_contact": "",
        "oa_date": "",
        "phone_date": "",
        "tech_date": "",
        "onsite_date": "",
        "offer_details": "",
        "next_action": "",
        "next_action_date": "",
        "notes": notes,
        "exp_years": exp_years_str,
        "exp_match": exp_match,
        "link_status": "",
    }


def _process_fetched_job(
    j: dict,
    company_name: str,
    source: str,
    filters: dict,
    threshold_inr: float,
    remote_floor_inr: float,
    seen: set,
    seen_keys: set,
    geo: Counter,
    drop_stats: Counter,
) -> dict | None:
    """Filter one fetched job; return tracker row or None if dropped."""
    url = j.get("url") or ""
    company_name, title = normalize_job_fields(
        company_name or j.get("company") or "",
        j.get("title") or "",
        url,
        source,
    )
    if company_name.lower() in SOURCE_NAMES:
        company_name = ""

    use_location = bool(filters.get("dedup_use_location", True))
    key = _norm_key(company_name or "TBD", title, j.get("location"),
                    use_location=use_location)
    canon = _canon_url(url)
    if not url or canon in seen or key in seen_keys:
        return None
    if not matches(title, j.get("location", ""), filters):
        return None

    desc = j.get("description") or ""
    ok, fresher_reason = passes_fresher_filter(
        title,
        desc,
        drop_exp_bad=bool(filters.get("drop_exp_bad", True)),
        max_exp_years=float(filters.get("max_exp_years", 2)),
    )
    if not ok:
        drop_stats[fresher_reason] += 1
        return None

    llm_en = None
    if llm_enabled() and (desc or title):
        llm_en = enrich_job(
            company=company_name or "Unknown",
            title=title,
            description=desc,
            url=url,
        )
        if not llm_en.get("keep", True):
            drop_stats["llm_skip"] += 1
            return None

    j_norm = dict(j)
    j_norm["title"] = title
    j_norm["company"] = company_name

    result, salary_display = classify_job(j_norm, threshold_inr, remote_floor_inr)
    geo[result] += 1
    if result in DROP_RESULTS:
        return None

    deadline_str = (
        j["deadline"] if "deadline" in j else _parse_deadline_proxy(j.get("updated", ""))
    )
    if filters.get("drop_expired_at_fetch", False) and deadline_str:
        try:
            if dt.date.fromisoformat(deadline_str) < dt.date.today():
                drop_stats["expired"] += 1
                return None
        except ValueError:
            pass

    seen.add(url)
    seen_keys.add(key)
    return make_row(
        company_name or "TBD",
        title,
        j.get("location", ""),
        source,
        url,
        salary_display,
        deadline_str,
        description=desc,
        llm_enrichment=llm_en,
    )


def migrate_tracker(tracker_path: Path) -> None:
    """Migrate a pre-salary tracker.csv to v1 schema (adds salary column).
    Idempotent: skips if salary already present or file absent.
    """
    if not tracker_path.exists():
        return
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = reader.fieldnames or []
        if "salary" in existing_fields:
            return
        rows = list(reader)
    tmp = tracker_path.with_name(tracker_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_V1_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in _V1_FIELDS})
    tmp.replace(tracker_path)
    print(f"==> migrated {tracker_path.name}: added 'salary' column to "
          f"{len(rows)} existing rows (preserved intact).")


def migrate_tracker_v2(tracker_path: Path) -> None:
    """Migrate v1 schema (status col, no funnel cols) → v2 funnel schema.

    Idempotent: skips if 'stage' column already present or file absent.
    Maps: status 'applied' → stage 'applied', 'rejected' → 'rejected',
    'withdrawn' → 'withdrawn', everything else (incl. 'new') → 'sourced'.
    Removes the old 'status' column. All new funnel columns default to "".
    Prints row count and duplicate-URL check.
    """
    if not tracker_path.exists():
        return
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = reader.fieldnames or []
        if "stage" in existing_fields:
            return
        rows = list(reader)

    _STAGE_MAP = {"applied": "applied", "rejected": "rejected", "withdrawn": "withdrawn"}

    def _stage(old: str) -> str:
        return _STAGE_MAP.get((old or "").lower().strip(), "sourced")

    urls = [r.get("url", "") for r in rows if r.get("url")]
    dup_count = len(urls) - len(set(urls))

    tmp = tracker_path.with_name(tracker_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in rows:
            new_row = {k: row.get(k, "") for k in FIELDS}
            new_row["stage"] = _stage(row.get("status", ""))
            w.writerow(new_row)
    tmp.replace(tracker_path)
    dup_msg = f"WARNING: {dup_count} duplicate URLs." if dup_count else "Zero duplicate URLs."
    print(
        f"==> migrated {tracker_path.name} to funnel schema v2: "
        f"{len(rows)} rows preserved, {dup_msg}"
    )


def migrate_tracker_v3(tracker_path: Path) -> None:
    """Migrate funnel schema v2 → v3: insert 'deadline' column after 'salary'.

    Idempotent: no-op if 'deadline' already present or file absent.
    'deadline' is an ISO date string (YYYY-MM-DD) or '' (unknown).
    All existing rows get '' — the fetcher will populate real values on next run.
    """
    if not tracker_path.exists():
        return
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = list(reader.fieldnames or [])
        if "deadline" in existing_fields:
            return
        rows = list(reader)

    # Insert 'deadline' right after 'salary' (or at end if salary absent)
    if "salary" in existing_fields:
        idx = existing_fields.index("salary") + 1
        new_fields = existing_fields[:idx] + ["deadline"] + existing_fields[idx:]
    else:
        new_fields = existing_fields + ["deadline"]

    urls = [r.get("url", "") for r in rows if r.get("url")]
    dup_count = len(urls) - len(set(urls))
    dup_msg = (f"WARNING: {dup_count} duplicate URLs." if dup_count
               else "Zero duplicate URLs.")

    tmp = tracker_path.with_name(tracker_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=new_fields)
        w.writeheader()
        for row in rows:
            new_row = {k: row.get(k, "") for k in new_fields}
            new_row["deadline"] = ""
            w.writerow(new_row)
    tmp.replace(tracker_path)
    print(
        f"==> migrated {tracker_path.name} to v3 schema: "
        f"added 'deadline' column to {len(rows)} existing rows (empty — populated on next fetch). "
        f"{dup_msg}"
    )


def migrate_tracker_v4(tracker_path: Path) -> None:
    """Migrate to v4 schema: add 'score' column at end.

    Idempotent: no-op if 'score' already present or file absent.
    The score column starts empty and is populated by score.py --write-scores.
    """
    if not tracker_path.exists():
        return
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = list(reader.fieldnames or [])
        if "score" in existing_fields:
            return
        rows = list(reader)

    # Insert 'score' right after 'company' (canonical position) so a migrated
    # legacy file ends up in the SAME column order as a freshly-created one.
    if "company" in existing_fields:
        idx = existing_fields.index("company") + 1
        new_fields = existing_fields[:idx] + ["score"] + existing_fields[idx:]
    else:
        new_fields = existing_fields + ["score"]
    tmp = tracker_path.with_name(tracker_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=new_fields)
        w.writeheader()
        for row in rows:
            new_row = {k: row.get(k, "") for k in new_fields}
            new_row["score"] = ""
            w.writerow(new_row)
    tmp.replace(tracker_path)
    print(
        f"==> migrated {tracker_path.name} to v4 schema: "
        f"added 'score' column to {len(rows)} existing rows "
        f"(empty — populated by score.py --write-scores)."
    )


def migrate_tracker_v5(tracker_path: Path) -> None:
    """Migrate to v5 schema: add exp_years and exp_match columns at end.

    Idempotent: no-op if exp_match already present or file absent.
    Existing rows get empty defaults; new fetches populate via parse_experience().
    """
    if not tracker_path.exists():
        return
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = list(reader.fieldnames or [])
        if "exp_match" in existing_fields:
            return
        rows = list(reader)

    new_fields = list(existing_fields) + ["exp_years", "exp_match"]
    tmp = tracker_path.with_name(tracker_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=new_fields)
        w.writeheader()
        for row in rows:
            new_row = {k: row.get(k, "") for k in new_fields}
            new_row.setdefault("exp_years", "")
            new_row.setdefault("exp_match", "")
            w.writerow(new_row)
    tmp.replace(tracker_path)
    print(
        f"==> migrated {tracker_path.name} to v5 schema: "
        f"added exp_years, exp_match to {len(rows)} existing rows."
    )


def migrate_tracker_v6(tracker_path: Path) -> None:
    """Migrate to v6 schema: add 'link_status' column at end.

    Idempotent: no-op if link_status already present or file absent. Holds the
    dead-link verdict (live / dead / expired) written by link_check.py; empty
    until the first link check runs.
    """
    if not tracker_path.exists():
        return
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = list(reader.fieldnames or [])
        if "link_status" in existing_fields:
            return
        rows = list(reader)

    new_fields = list(existing_fields) + ["link_status"]
    tmp = tracker_path.with_name(tracker_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=new_fields)
        w.writeheader()
        for row in rows:
            new_row = {k: row.get(k, "") for k in new_fields}
            new_row.setdefault("link_status", "")
            w.writerow(new_row)
    tmp.replace(tracker_path)
    print(
        f"==> migrated {tracker_path.name} to v6 schema: "
        f"added link_status to {len(rows)} existing rows."
    )


# ---- Normalized role-key dedup --------------------------------------------
# Aggregators (especially Adzuna) re-list the SAME posting under different ad-id
# URLs and `se=` query tokens, so URL-dedup alone lets functionally-identical
# rows pile up (e.g. ~20 "Infosys Finacle / Java Backend Developer / Chennai").
# We collapse those by a normalized (company, role, location) identity key.
#
# Stages that represent an un-triaged sourced row, safe to collapse away. ANY
# other stage (applied, oa, phone, tech, onsite, offer, not_applicable,
# rejected, withdrawn, ...) carries the user's manual work and is NEVER dropped.
_COLLAPSIBLE_STAGES = frozenset({"sourced", "new", ""})


def _dedup_keep_sort_key(row: dict) -> tuple:
    """Sort key choosing which collapsible row to KEEP within a cluster.

    Lower sorts first = preferred. Priority (matches the documented keeping
    rules): row HAS a salary, then row HAS a deadline, then earliest date_found.
    """
    has_salary = 0 if (row.get("salary") or "").strip() else 1
    has_deadline = 0 if (row.get("deadline") or "").strip() else 1
    date_found = (row.get("date_found") or "").strip() or "9999-99-99"
    return (has_salary, has_deadline, date_found)


def dedup_tracker_by_role(tracker_path: Path, *, use_location: bool = True,
                          dry_run: bool = False) -> int:
    """Collapse duplicate rows in the tracker, keeping ONE row per cluster.

    Two rows are the SAME posting (and cluster together) when EITHER:
      * they share a canonical (company, role, location) identity key —
        location-sensitive by default, so genuinely distinct city postings stay
        separate (identical company+role+location still collapses); OR
      * they share a canonical URL (tracking params / fragments stripped) — the
        same ad shared under different `?utm_*` / `?se=` links, INCLUDING one
        posting spammed under many city labels behind a single URL.
    These relations are unioned (transitively) so a chain of near-duplicates
    collapses to one row.

    Keeping rules (priority order):
      1. NEVER drop a row whose stage is outside sourced/new/empty — applied, oa,
         phone, tech, onsite, offer, not_applicable, rejected, withdrawn carry the
         user's manual work and are always preserved (all of them).
      2. Among the remaining collapsible rows, prefer the one that HAS a salary.
      3. Then the one that HAS a deadline.
      4. Then the earliest date_found.

    Idempotent (a second run is a no-op). Returns the number of rows removed.
    """
    if not tracker_path.exists():
        return 0
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or FIELDS
        rows = list(reader)

    n = len(rows)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    key_groups: dict = {}
    url_groups: dict = {}
    for idx, r in enumerate(rows):
        key_groups.setdefault(
            _norm_key(r.get("company"), r.get("role"), r.get("location"),
                      use_location=use_location), []
        ).append(idx)
        cu = _canon_url(r.get("url") or "")
        if cu:
            url_groups.setdefault(cu, []).append(idx)
    for group in (*key_groups.values(), *url_groups.values()):
        for other in group[1:]:
            union(group[0], other)

    components: dict = {}
    for idx in range(n):
        components.setdefault(find(idx), []).append(idx)

    drop_idx: set = set()
    clusters_collapsed = 0
    for idxs in components.values():
        if len(idxs) < 2:
            continue
        protected = [i for i in idxs
                     if (rows[i].get("stage") or "").strip().lower()
                     not in _COLLAPSIBLE_STAGES]
        if protected:
            # Preserve EVERY manual-work row; drop only the plain sourced/new dups.
            keep = set(protected)
        else:
            # Keep the single best collapsible row (salary > deadline > earliest).
            keep = {min(idxs, key=lambda i: _dedup_keep_sort_key(rows[i]))}
        dropped = [i for i in idxs if i not in keep]
        if dropped:
            clusters_collapsed += 1
            drop_idx.update(dropped)

    if not drop_idx:
        return 0

    if not dry_run:
        kept_rows = [r for i, r in enumerate(rows) if i not in drop_idx]
        tmp = tracker_path.with_name(tracker_path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(kept_rows)
        tmp.replace(tracker_path)
    verb = "Would collapse" if dry_run else "Collapsed"
    scope = "company+role+location" if use_location else "company+role (or shared URL)"
    print(f"  {verb} {len(drop_idx)} duplicate rows ({clusters_collapsed} clusters) "
          f"by {scope}.")
    return len(drop_idx)


def load_existing_urls(tracker_path: Path) -> set:
    # All URLs in the tracker (any stage) are added to the dedup set, so any
    # advanced stage — applied, oa, not_applicable, etc. — prevents re-adding.
    # not_applicable rows are kept in the CSV permanently as a dedup blacklist,
    # so their URLs are always present in `seen` and can never be re-fetched.
    urls = set()
    if tracker_path.exists():
        with tracker_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cu = _canon_url(row.get("url") or "")
                if cu:  # store the CANONICAL url so tracking-param variants collide
                    urls.add(cu)
    return urls


def load_existing_keys(tracker_path: Path, *, use_location: bool = True) -> set:
    """Normalized (company, role[, location]) keys already in the tracker — the
    role-identity dedup set that complements load_existing_urls().

    Lets a fresh fetch skip a posting an aggregator re-listed under a NEW ad-id
    URL (which URL-dedup alone would miss). Mirrors dedup_tracker_by_role()'s key.
    """
    keys = set()
    if tracker_path.exists():
        with tracker_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = _norm_key(row.get("company"), row.get("role"),
                                row.get("location"), use_location=use_location)
                if any(key):  # ignore a fully-empty/malformed row
                    keys.add(key)
    return keys


def _row_over_experience(row: dict, *, max_years: float, drop_senior: bool,
                         drop_bad: bool) -> str:
    """Return a drop-reason if the row exceeds fresher experience limits, else ''.

    Looks at the title, the notes (LLM/JD fragments), and the stored exp columns,
    so a role whose JD years landed only in exp_years is still caught.
    """
    title = row.get("role", "") or ""
    if drop_senior and is_senior_title(title):
        return "senior_title"
    yrs, match = parse_experience(title, row.get("notes", "") or "")
    if yrs is not None and yrs > max_years:
        return f"exp_gt_{max_years:g}"
    if drop_bad and match == "bad":
        return "exp_bad"
    col = (row.get("exp_years") or "").strip()
    if col:
        try:
            if float(col) > max_years:
                return f"exp_gt_{max_years:g}"
        except ValueError:
            pass
    if drop_bad and (row.get("exp_match") or "").strip().lower() == "bad":
        return "exp_bad"
    return ""


def prune_over_experience(tracker_path: Path, *, max_years: float = 2.0,
                          drop_senior: bool = True, drop_bad: bool = True,
                          dry_run: bool = False) -> tuple[int, Counter, list]:
    """Drop sourced/new rows that require more than max_years experience (or carry
    a clear seniority title). Advanced rows (applied, oa, ...) are never touched.

    Idempotent. Returns (removed_count, Counter of reasons, up-to-5 examples).
    """
    if not tracker_path.exists():
        return 0, Counter(), []
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or FIELDS
        rows = list(reader)

    kept, reasons, examples = [], Counter(), []
    for r in rows:
        stage = (r.get("stage") or "").strip().lower()
        advanced = bool((r.get("applied_date") or "").strip()) or stage not in ("sourced", "new")
        if advanced:
            kept.append(r)
            continue
        reason = _row_over_experience(
            r, max_years=max_years, drop_senior=drop_senior, drop_bad=drop_bad,
        )
        if reason:
            reasons[reason] += 1
            if len(examples) < 5:
                examples.append({
                    "company": r.get("company", ""), "role": r.get("role", ""),
                    "exp_years": r.get("exp_years", ""), "reason": reason,
                })
        else:
            kept.append(r)

    pruned = len(rows) - len(kept)
    if pruned and not dry_run:
        tmp = tracker_path.with_name(tracker_path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(kept)
        tmp.replace(tracker_path)
    return pruned, reasons, examples


def prune_sourced_failures(tracker_path: Path, min_lpa_inr: float,
                           remote_floor_inr: float):
    """Re-apply the current keep/drop rules to EXISTING rows that are still in the
    triage queue (stage in {sourced,new} AND no applied_date) and drop the ones
    that now fail — e.g. foreign-locked remote roles kept under the old flat rule.

    Rows that have ADVANCED are preserved untouched: any row with a non-empty
    applied_date, or a stage outside {sourced,new}, is never modified or removed.
    This includes not_applicable rows — they are handled separately by
    _prune_not_applicable() which runs before this function.
    Idempotent (a second run is a no-op) and safe to run on every fetch. Returns
    (pruned_count, Counter of drop-reasons).
    """
    if not tracker_path.exists():
        return 0, Counter()
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or FIELDS
        rows = list(reader)

    kept_rows, reasons = [], Counter()
    for r in rows:
        stage = (r.get("stage") or "").strip().lower()
        # Stages outside {sourced, new} (applied, oa, not_applicable, etc.) are
        # treated as advanced and preserved here unconditionally.
        advanced = bool((r.get("applied_date") or "").strip()) or stage not in ("sourced", "new")
        if advanced:
            kept_rows.append(r)
            continue
        annual_inr = _inr_from_display(r.get("salary", ""))
        result = _geo_salary_result(
            r.get("role", ""), r.get("location", ""), annual_inr,
            min_lpa_inr, remote_floor_inr,
            salary_display=r.get("salary", ""),
        )
        if result in DROP_RESULTS:
            reasons[result] += 1
        else:
            kept_rows.append(r)

    pruned = len(rows) - len(kept_rows)
    if pruned:
        tmp = tracker_path.with_name(tracker_path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(kept_rows)
        tmp.replace(tracker_path)
    return pruned, reasons


def repair_company_fields(tracker_path: Path, *, dry_run: bool = False) -> int:
    """Fix rows whose company is still an aggregator source name but URL has real employer."""
    if not tracker_path.exists():
        return 0
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or FIELDS
        rows = list(reader)

    repaired = 0
    for r in rows:
        co = (r.get("company") or "").strip()
        src = (r.get("source") or "").strip().lower()
        url = r.get("url") or ""
        needs_repair = (
            co.lower() in SOURCE_NAMES
            or is_invalid_company(co, url)
        )
        if not needs_repair:
            continue
        new_co, new_role = normalize_job_fields(co, r.get("role", ""), url, src)
        if not new_co or new_co.lower() in SOURCE_NAMES or is_invalid_company(new_co, url):
            continue
        if new_co != co or new_role != (r.get("role") or ""):
            r["company"] = new_co
            if new_role:
                r["role"] = new_role
            repaired += 1

    if repaired and not dry_run:
        tmp = tracker_path.with_name(tracker_path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(tracker_path)
    return repaired


def prune_junk_listings(tracker_path: Path, *, dry_run: bool = False) -> tuple[int, Counter]:
    """Remove sourced/new rows that are category pages, spam, or junk titles.

    Advanced stages (applied, oa, not_applicable, etc.) are never touched.
    When dry_run=True, counts and reasons are returned without writing.
    """
    if not tracker_path.exists():
        return 0, Counter()
    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or FIELDS
        rows = list(reader)

    kept_rows, reasons = [], Counter()
    for r in rows:
        stage = (r.get("stage") or "").strip().lower()
        advanced = bool((r.get("applied_date") or "").strip()) or stage not in ("sourced", "new")
        if advanced:
            kept_rows.append(r)
            continue
        src = (r.get("source") or "").strip().lower()
        co, role = normalize_job_fields(
            r.get("company", ""), r.get("role", ""), r.get("url", ""), src,
        )
        ok, reason = accept_job(
            co,
            role,
            r.get("url", ""),
            src,
            description=r.get("notes", ""),
        )
        if ok:
            r = dict(r)
            r["company"] = co
            r["role"] = role
            kept_rows.append(r)
        else:
            reasons[reason or "junk"] += 1

    pruned = len(rows) - len(kept_rows)
    if pruned and not dry_run:
        tmp = tracker_path.with_name(tracker_path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(kept_rows)
        tmp.replace(tracker_path)
    return pruned, reasons


def _prune_not_applicable(tracker_path: Path) -> int:
    """Count not_applicable rows in the tracker (informational only — no writes).

    not_applicable rows are kept in CSV as a permanent dedup blacklist; they are
    excluded from the Sheet push so they don't clutter the working view. By
    staying in the CSV their URLs/keys remain in load_existing_urls() and
    load_existing_keys(), so the same job can never be re-fetched. Returns the
    count for informational printing.
    """
    if not tracker_path.exists():
        return 0
    with tracker_path.open(newline="", encoding="utf-8") as f:
        count = sum(
            1 for row in csv.DictReader(f)
            if (row.get("stage") or "").strip() == "not_applicable"
        )
    return count


# Human-readable labels for the keep/drop result vocabulary (used in summaries).
RESULT_LABELS = {
    "remote_keep": "remote (India-eligible)",
    "india_keep": "India onsite",
    "remote_foreign_drop": "foreign-locked remote",
    "remote_salary_drop": "remote below floor",
    "india_salary_drop": "India onsite below floor",
    "foreign_drop": "foreign onsite",
}


def _fmt_reasons(reasons: Counter) -> str:
    return ", ".join(f"{RESULT_LABELS.get(k, k)}: {v}"
                     for k, v in reasons.most_common()) or "none"


def _is_on_cooldown(state: dict, source: str) -> tuple:
    """Return (on_cooldown: bool, hours_elapsed: float). Never raises."""
    last_str = state.get(source)
    if not last_str:
        return False, 0.0
    try:
        last = dt.datetime.fromisoformat(last_str)
        elapsed_h = (dt.datetime.now(dt.timezone.utc) - last.replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
        return elapsed_h < COOLDOWN_HOURS, elapsed_h
    except Exception:
        return False, 0.0


def _load_fetch_state(state_path: Path) -> dict:
    """Load {source_name: iso_timestamp_str} from .fetch_state.json."""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_fetch_state(state_path: Path, state: dict) -> None:
    """Persist the fetch state dict."""
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pull_sheet_if_configured(tracker_path: Path) -> None:
    """Pull Google Sheet → tracker.csv before fetching.

    Ensures manually-added rows participate in dedup and pruning.
    Silently skips when GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_JSON are absent.
    Never raises — a failed pull is logged as a warning and the fetch continues.
    """
    if not (os.environ.get("GOOGLE_SHEETS_ID", "").strip()
            and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()):
        return
    result = subprocess.run(
        ["uv", "run", "sheets_sync.py", "--pull"],
        cwd=str(tracker_path.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  [sheets] Pulled Sheet → CSV before fetch.")
    else:
        print(
            f"  [sheets] WARNING: Sheet → CSV pull failed "
            f"(exit {result.returncode}); continuing fetch.\n"
            f"  stderr: {result.stderr.strip()}"
        )


def _run_link_check(tracker_path: Path, filters: dict, *, dry_run: bool,
                    drop_dead=None, limit=None):
    """Run the dead-link check over the tracker using sources.yaml knobs.

    Never raises: a link-check failure must not break the fetch.
    """
    try:
        import link_check
    except Exception as e:  # pragma: no cover - defensive import guard
        print(f"  [links] link_check unavailable ({type(e).__name__}); skipped.")
        return None
    dd = filters.get("link_check_drop_dead", True) if drop_dead is None else drop_dead
    lim = int(filters.get("link_check_limit", 0)) if limit is None else limit
    try:
        summary = link_check.check_tracker_links(
            tracker_path,
            drop_dead=bool(dd),
            dry_run=dry_run,
            limit=lim,
            concurrency=int(filters.get("link_check_concurrency", 8)),
            per_host_delay=float(filters.get("link_check_per_host_delay", 1.0)),
            timeout=float(filters.get("link_check_timeout", 15)),
            ttl_days=int(filters.get("link_check_ttl_days", 7)),
            dead_ttl_days=int(filters.get("link_check_dead_ttl_days", 30)),
            backup=False,
        )
    except Exception as e:  # pragma: no cover - network/runtime guard
        print(f"  [links] link check failed ({type(e).__name__}: {e}); continuing.")
        return None
    v = summary["verdicts"]
    verb = "would flag" if dry_run else ("removed" if dd else "flagged")
    print(f"  [links] {summary['checked']} checked — live={v.get('live', 0)} "
          f"dead={v.get('dead', 0)} expired={v.get('expired', 0)} "
          f"unknown={v.get('unknown', 0)}; {verb} "
          f"{v.get('dead', 0) + v.get('expired', 0)} dead/expired"
          + (f" ({summary['removed']} rows removed)"
             if dd and not dry_run else "") + ".")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default="sources.yaml")
    ap.add_argument("--tracker", default="tracker.csv")
    ap.add_argument("--force", action="store_true",
                    help="Bypass the 20-hour cooldown for paid API sources "
                         "(jsearch, adzuna, serpapi).")
    ap.add_argument("--dedup-only", action="store_true",
                    help="Collapse normalized (company,role,location) duplicate "
                         "rows in the tracker and rescore, then exit (no fetching).")
    ap.add_argument("--cleanup-junk", action="store_true",
                    help="Remove category-page/spam/junk rows from tracker and exit "
                         "(no fetching). Safe for applied/advanced rows.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write tracker.csv (works with fetch or --cleanup-junk).")
    args = ap.parse_args()

    base = Path(__file__).parent
    cfg = yaml.safe_load((base / args.sources).read_text())
    filters = cfg.get("filters", {})
    # Dedup keeps every genuinely distinct posting; only TRUE duplicates (same
    # canonical URL, or identical company+role+location) collapse. Set
    # filters.dedup_use_location: false to also merge the same role across cities.
    use_loc = bool(filters.get("dedup_use_location", True))
    tracker_path = base / args.tracker
    migrate_tracker(tracker_path)
    migrate_tracker_v2(tracker_path)
    migrate_tracker_v3(tracker_path)
    migrate_tracker_v4(tracker_path)
    migrate_tracker_v5(tracker_path)
    migrate_tracker_v6(tracker_path)

    # --cleanup-junk: drop category pages / spam without fetching.
    if args.cleanup_junk:
        junk_pruned, junk_reasons = prune_junk_listings(
            tracker_path, dry_run=args.dry_run,
        )
        prefix = "Would remove" if args.dry_run else "Removed"
        if junk_pruned:
            print(f"  {prefix} {junk_pruned} junk row(s): "
                  f"{', '.join(f'{k}={v}' for k, v in junk_reasons.most_common())}")
        else:
            print("  No junk listings to remove.")
        if not args.dry_run:
            dedup_tracker_by_role(tracker_path, use_location=use_loc)
            subprocess.run(["uv", "run", "score.py", "--write-scores"],
                           cwd=str(tracker_path.parent), capture_output=True, text=True)
        return 0

    # --dedup-only: collapse role-duplicates + rescore, no fetching/Sheet sync.
    if args.dedup_only:
        removed = dedup_tracker_by_role(tracker_path, use_location=use_loc)
        if not removed:
            print("  No normalized (company+role+location) duplicates to collapse.")
        subprocess.run(["uv", "run", "score.py", "--write-scores"],
                       cwd=str(tracker_path.parent), capture_output=True, text=True)
        print("  Scores updated in tracker (dedup-only run; no fetch, no Sheet sync).")
        return 0

    # Pull Sheet → CSV first so manually-added rows participate in dedup and pruning
    _pull_sheet_if_configured(tracker_path)

    state_path = tracker_path.parent / ".fetch_state.json"
    fetch_state = _load_fetch_state(state_path)

    na_count = _prune_not_applicable(tracker_path)
    if na_count:
        print(f"  {na_count} not_applicable row(s) in tracker (permanent dedup blacklist).")

    # Collapse aggregator re-listings of the SAME role (different ad-id URLs) into
    # one row per (company, role, location) BEFORE loading the dedup sets, so new
    # fetches dedupe against the cleaned tracker. Runs after the Sheet pull and
    # not_applicable prune so manually-added/dismissed rows are accounted for.
    dedup_tracker_by_role(tracker_path, use_location=use_loc)

    threshold_lpa = float(filters.get("min_salary_lpa", DEFAULT_MIN_SALARY_LPA))
    remote_floor_lpa = float(filters.get("remote_floor_lpa", DEFAULT_REMOTE_FLOOR_LPA))
    threshold_inr = threshold_lpa * 1e5
    remote_floor_inr = remote_floor_lpa * 1e5

    # Optional: re-apply geo rules to existing sourced rows (OFF by default — you
    # dismiss junk via stage=not_applicable in Sheet instead).
    if filters.get("auto_prune_geo_failures", False):
        pruned, prune_reasons = prune_sourced_failures(
            tracker_path, threshold_inr, remote_floor_inr
        )
        if pruned:
            print(f"==> removed {pruned} sourced row(s) failing geo rules "
                  f"[{_fmt_reasons(prune_reasons)}] (US onsite / region-locked remote).")

    repaired = repair_company_fields(tracker_path)
    if repaired:
        print(f"==> repaired company name on {repaired} aggregator row(s) from job URLs.")

    junk_pruned, junk_reasons = prune_junk_listings(tracker_path)
    if junk_pruned:
        print(f"==> pruned {junk_pruned} junk listing(s) "
              f"[{', '.join(f'{k}: {v}' for k, v in junk_reasons.most_common())}] "
              f"(category pages, spam, bad titles).")

    seen = load_existing_urls(tracker_path)
    # Parallel role-key dedup set: an incoming job is skipped if its URL is in
    # `seen` OR its normalized (company, role, location) key is in `seen_keys`.
    seen_keys = load_existing_keys(tracker_path, use_location=use_loc)

    geo = Counter()  # keep/drop result tallies across all sources
    drop_stats = Counter()

    new_rows, report = [], []
    company_tasks: list[tuple[str, object, tuple, dict]] = []
    company_labels: dict[str, tuple[str, str, str]] = {}
    for c in cfg.get("companies", []):
        name, ats, token = c.get("name"), c.get("ats"), c.get("token")
        fetch = FETCHERS.get(ats)
        if not fetch:
            report.append(f"  ! {name}: unknown ats '{ats}'")
            continue
        label = f"{name}:{ats}"
        company_tasks.append((label, fetch, (token,), {}))
        company_labels[label] = (name, ats, token)

    if company_tasks:
        for label, result in run_parallel_fetch(company_tasks, max_workers=8):
            name, ats, token = company_labels[label]
            if isinstance(result, BaseException):
                report.append(
                    f"  ! {name} ({ats}/{token}): {type(result).__name__} - skipped (check token)"
                )
                continue
            jobs = result
            kept = 0
            for j in jobs:
                row = _process_fetched_job(
                    j, name, ats, filters, threshold_inr, remote_floor_inr,
                    seen, seen_keys, geo, drop_stats,
                )
                if row:
                    kept += 1
                    new_rows.append(row)
            report.append(f"  - {name} ({ats}): {len(jobs)} open, {kept} new matches")

    # ---- aggregators: cross-employer APIs (parallel where not on cooldown) ----
    report.append("  -- aggregators --")
    agg_tasks: list[tuple[str, object, tuple, dict]] = []
    agg_labels: dict[str, str] = {}
    agg_skipped_reports: list[str] = []
    for agg in cfg.get("aggregators", DEFAULT_AGGREGATORS):
        src = agg.get("source")
        fetch_agg = AGGREGATOR_FETCHERS.get(src)
        if not fetch_agg:
            agg_skipped_reports.append(f"  ! aggregator '{src}': unknown source")
            continue
        if src in PAID_SOURCES and not args.force:
            on_cd, hours_ago = _is_on_cooldown(fetch_state, src)
            if on_cd:
                agg_skipped_reports.append(
                    f"  ~ {src}: skipped (last run {hours_ago:.0f}h ago, "
                    f"cooldown 20h). Use --force to override."
                )
                continue
        agg_cfg = {**agg, "_fetch_state": fetch_state, "_force": args.force}
        agg_tasks.append((src, fetch_agg, (agg_cfg,), {}))
        agg_labels[src] = src

    if agg_tasks:
        for label, result in run_parallel_fetch(agg_tasks, max_workers=6):
            src = agg_labels[label]
            if isinstance(result, BaseException):
                if isinstance(result, SkipSource):
                    agg_skipped_reports.append(f"  ~ {src}: {result}")
                else:
                    agg_skipped_reports.append(
                        f"  ! {src} (aggregator): {type(result).__name__} - skipped"
                    )
                continue
            if src in PAID_SOURCES:
                fetch_state[src] = dt.datetime.now(dt.timezone.utc).isoformat()
                _save_fetch_state(state_path, fetch_state)
            jobs = result
            kept = 0
            for j in jobs:
                row = _process_fetched_job(
                    j,
                    j.get("company") or "",
                    src,
                    filters,
                    threshold_inr,
                    remote_floor_inr,
                    seen,
                    seen_keys,
                    geo,
                    drop_stats,
                )
                if row:
                    kept += 1
                    new_rows.append(row)
            agg_skipped_reports.append(
                f"  - {src} (aggregator): {len(jobs)} fetched, {kept} new matches"
            )

    report.extend(agg_skipped_reports)
    expired_drops = drop_stats.get("expired", 0)

    # Append new rows in the EXISTING file's column order (read by NAME), so a
    # tracker whose columns were reordered upstream (e.g. by a Sheet pull or a
    # manual Sheet edit) can never be misaligned. FIELDS is used only when the
    # file is being created fresh. extrasaction="ignore" tolerates schema drift.
    print("\n".join(report))
    if args.dry_run:
        print(
            f"\n==> DRY RUN: {len(new_rows)} new roles would be added to "
            f"{tracker_path.name} ({len(seen)} total tracked; not written).\n"
            f"    Kept:    {geo['remote_keep']} remote (India-eligible), "
            f"{geo['india_keep']} India onsite.\n"
            f"    Dropped: {geo['remote_foreign_drop']} foreign-locked remote, "
            f"{geo['remote_salary_drop']} remote < {remote_floor_lpa:g} LPA, "
            f"{geo['india_salary_drop']} India onsite < {threshold_lpa:g} LPA, "
            f"{geo['foreign_drop']} foreign onsite.\n"
            f"    {expired_drops} role(s) dropped (expired)."
        )
        if drop_stats:
            top = ", ".join(f"{k}={v}" for k, v in drop_stats.most_common(6))
            print(f"    Fresher/LLM filter drops: {top}")
        return 0

    write_header = not tracker_path.exists()
    if write_header:
        out_fields = list(FIELDS)
    else:
        with tracker_path.open(newline="", encoding="utf-8") as f:
            out_fields = next(csv.reader(f), None) or list(FIELDS)
    with tracker_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for r in new_rows:
            w.writerow(r)

    print(
        f"\n==> {len(new_rows)} new roles added to {tracker_path.name} "
        f"({len(seen)} total tracked).\n"
        f"    Kept:    {geo['remote_keep']} remote (India-eligible), "
        f"{geo['india_keep']} India onsite.\n"
        f"    Dropped: {geo['remote_foreign_drop']} foreign-locked remote, "
        f"{geo['remote_salary_drop']} remote < {remote_floor_lpa:g} LPA, "
        f"{geo['india_salary_drop']} India onsite < {threshold_lpa:g} LPA, "
        f"{geo['foreign_drop']} foreign onsite.\n"
        f"    {expired_drops} role(s) dropped (expired).\n"
        f"    Ranking tiers (T1 remote>={threshold_lpa:g} > T2 onsite>={threshold_lpa:g} "
        f"> T3 remote {remote_floor_lpa:g}-{threshold_lpa:g} > T4 unknown salary)."
    )
    if drop_stats:
        top = ", ".join(f"{k}={v}" for k, v in drop_stats.most_common(6))
        print(f"    Fresher/LLM filter drops: {top}")

    # Post-fetch cleanup + scoring (all idempotent; advanced/applied rows are
    # never touched by any of these):
    if not args.dry_run:
        # Collapse any duplicates the fetch introduced (URL or role+location).
        dedup_tracker_by_role(tracker_path, use_location=use_loc)
        # Drop sourced rows that require > max_exp_years or carry a senior title.
        if filters.get("prune_over_experience", True):
            from profile_config import drop_senior_titles as _psd
            exp_pruned, exp_reasons, _ = prune_over_experience(
                tracker_path,
                max_years=float(filters.get("max_exp_years", 2)),
                drop_senior=_psd(),
                drop_bad=bool(filters.get("drop_exp_bad", True)),
            )
            if exp_pruned:
                print(f"==> pruned {exp_pruned} over-experience/senior row(s) "
                      f"[{', '.join(f'{k}: {v}' for k, v in exp_reasons.most_common())}].")
        # Verify job links: flag link_status and (by default) remove dead/expired
        # sourced rows. Never removes UNKNOWN (bot-walled) or applied/advanced rows.
        if filters.get("check_links", True):
            _run_link_check(tracker_path, filters, dry_run=False)

        # Score all rows and write back to CSV so Sheet has sortable scores
        subprocess.run(["uv", "run", "score.py", "--write-scores"],
                       cwd=str(tracker_path.parent), capture_output=True, text=True)
        print("  Scores updated in tracker.")

        # Push clean CSV to Google Sheets if configured (silently skipped when env
        # vars are absent). We use --push (overwrite) rather than --sync (merge)
        # because the pull at the top of this run already captured all user edits;
        # a sync would re-import Sheet rows that were intentionally pruned (e.g.
        # not_applicable rows) and inflate the Sheet count on every run.
        if (os.environ.get("GOOGLE_SHEETS_ID", "").strip()
                and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()):
            sync_script = base / "sheets_sync.py"
            print("\n==> Pushing clean CSV to Google Sheets (--push)...")
            try:
                subprocess.run(["uv", "run", str(sync_script), "--push"], check=False)
            except FileNotFoundError:
                print("    (uv not found — Sheets push skipped; run manually: uv run sheets_sync.py --push)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
