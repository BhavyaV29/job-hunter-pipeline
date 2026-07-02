"""Playwright fetchers that intercept internal JSON APIs (Hirist, Naukri, Cutshort, Wellfound).

These boards are React SPAs — the reliable approach (used by Apify actors) is to
load the search page in a real browser and capture the XHR/fetch responses, or
read __NEXT_DATA__ Apollo state (Wellfound).

Enable: uv pip install playwright && playwright install chromium
Optional: PLAYWRIGHT_HEADED=1 for non-headless (helps anti-bot)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable
from urllib.parse import quote

from fetch_jobs import BROWSER_UA, POLITE_DELAY, SkipSource, clean

_TIMEOUT_MS = 45_000


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _launch_browser(p, *, force_headed: bool = False):
    headed = force_headed or os.environ.get("PLAYWRIGHT_HEADED", "").strip() in ("1", "true", "yes")
    return p.chromium.launch(
        headless=not headed,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )


def _new_context(browser):
    return browser.new_context(
        user_agent=BROWSER_UA,
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1280, "height": 900},
        extra_http_headers={
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )


def _intercept_json(
    page,
    url_substrings: tuple[str, ...],
    *,
    wait_ms: int = 5000,
) -> list[Any]:
    """Collect JSON bodies from matching network responses."""
    captured: list[Any] = []

    def on_response(response):
        try:
            url = response.url
            if not any(s in url for s in url_substrings):
                return
            if response.status != 200:
                return
            ct = (response.headers.get("content-type") or "").lower()
            if "json" not in ct and "javascript" not in ct:
                return
            captured.append(response.json())
        except Exception:
            pass

    page.on("response", on_response)
    page.wait_for_timeout(wait_ms)
    return captured


def _extract_next_data(page) -> dict | None:
    raw = page.evaluate("""() => {
        const el = document.querySelector('#__NEXT_DATA__');
        return el ? el.textContent : null;
    }""")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _walk_apollo_jobs(state: dict) -> list[dict]:
    """Extract job-like nodes from Wellfound Apollo cache."""
    found: list[dict] = []

    def walk(obj, depth=0):
        if depth > 30:
            return
        if isinstance(obj, dict):
            title = obj.get("title") or obj.get("primaryRoleTitle")
            slug = obj.get("slug") or obj.get("id")
            if title and (obj.get("__typename") in (
                "JobListing", "StartupResult", "JobPosting", None
            ) or obj.get("primaryRoleTitle")):
                startup = obj.get("startup") or obj.get("company") or {}
                if isinstance(startup, dict):
                    company = startup.get("name") or startup.get("slug") or ""
                else:
                    company = str(startup or "")
                job_id = obj.get("id") or obj.get("jobListingId") or slug
                if job_id:
                    url = f"https://wellfound.com/jobs/{job_id}"
                    if isinstance(slug, str) and slug.isdigit():
                        url = f"https://wellfound.com/jobs/{slug}"
                    found.append({
                        "company": str(company),
                        "title": str(title),
                        "location": obj.get("locationTagline") or "India",
                        "url": url,
                        "updated": "",
                        "description": clean(obj.get("description") or ""),
                    })
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    apollo = (state.get("props") or {}).get("pageProps") or {}
    for key in ("apolloState", "dehydratedState", "initialApolloState"):
        if key in apollo:
            walk(apollo[key])
    walk(state)
    return found


def fetch_hirist_playwright(cfg: dict) -> list[dict]:
    if not _playwright_available():
        raise SkipSource("Playwright not installed")

    from playwright.sync_api import sync_playwright

    keywords = cfg.get("keywords") or ["software engineer", "backend", "python"]
    out: list[dict] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        browser = _launch_browser(p)
        ctx = _new_context(browser)
        page = ctx.new_page()
        try:
            for kw in keywords:
                slug = kw.replace(" ", "-").lower()
                url = f"https://www.hirist.tech/k/{slug}-jobs"
                captured: list[Any] = []

                def on_resp(response):
                    if "jobseeker-api.hirist" not in response.url:
                        return
                    try:
                        if response.status == 200:
                            captured.append(response.json())
                    except Exception:
                        pass

                page.on("response", on_resp)
                try:
                    page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                page.wait_for_timeout(3000)
                page.remove_listener("response", on_resp)

                for data in captured:
                    jobs = data if isinstance(data, list) else (
                        data.get("jobs") or data.get("data") or []
                    )
                    for j in jobs:
                        if not isinstance(j, dict):
                            continue
                        detail = j.get("jobDetailUrl") or j.get("jobUrl") or ""
                        if detail and not detail.startswith("http"):
                            detail = f"https://www.hirist.com{detail}"
                        title = j.get("title") or j.get("jobTitle") or ""
                        company = (
                            j.get("companyName") or j.get("company")
                            or (j.get("company") or {}).get("name", "")
                            if isinstance(j.get("company"), dict) else ""
                        )
                        if not detail or not title or detail in seen:
                            continue
                        seen.add(detail)
                        out.append({
                            "company": str(company or ""),
                            "title": str(title),
                            "location": str(j.get("location") or "India"),
                            "url": detail,
                            "updated": str(j.get("createdAt") or ""),
                            "description": clean(j.get("description") or j.get("jobDescription") or ""),
                        })
                time.sleep(POLITE_DELAY)
        finally:
            browser.close()

    if not out:
        raise SkipSource("Hirist Playwright intercepted 0 API responses")
    return out


def fetch_naukri_playwright_intercept(cfg: dict) -> list[dict]:
    """Naukri via Playwright — intercept jobapi/v3/search JSON.

    Probed: headless → Access Denied; headed browser → 20 jobs/intercept.
    Auto-retries with headed=True when headless returns nothing.
    """
    if not _playwright_available():
        raise SkipSource("Playwright not installed")

    from playwright.sync_api import sync_playwright
    from naukri_api import _job_from_api

    keywords = cfg.get("keywords") or ["software engineer", "backend developer"]
    experience = int(cfg.get("experience", 0))
    out: list[dict] = []
    seen: set[str] = set()

    def _run(*, headed: bool) -> list[dict]:
        batch: list[dict] = []
        with sync_playwright() as p:
            browser = _launch_browser(p, force_headed=headed)
            ctx = _new_context(browser)
            page = ctx.new_page()
            try:
                page.goto("https://www.naukri.com/", wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                page.wait_for_timeout(2000)
                for kw in keywords:
                    search_url = (
                        f"https://www.naukri.com/{kw.replace(' ', '-')}-jobs"
                        f"?k={quote(kw)}&experience={experience}"
                    )
                    captured: list[Any] = []

                    def on_resp(response):
                        if "jobapi/v3/search" not in response.url:
                            return
                        try:
                            if response.status == 200:
                                captured.append(response.json())
                        except Exception:
                            pass

                    page.on("response", on_resp)
                    try:
                        page.goto(search_url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                    except Exception:
                        pass
                    page.wait_for_timeout(6000)
                    page.remove_listener("response", on_resp)

                    for data in captured:
                        for job in data.get("jobDetails") or []:
                            row = _job_from_api(job)
                            if row and row["url"] not in seen:
                                seen.add(row["url"])
                                batch.append(row)
                    time.sleep(POLITE_DELAY)
            finally:
                browser.close()
        return batch

    out = _run(headed=False)
    if not out:
        print("  [naukri] Headless blocked — retrying with headed browser (proven to work).")
        out = _run(headed=True)

    if not out:
        raise SkipSource("Naukri Playwright intercept returned 0 jobs (try residential IP)")
    return out


def fetch_wellfound_playwright(cfg: dict) -> list[dict]:
    if not _playwright_available():
        raise SkipSource("Playwright not installed")

    from playwright.sync_api import sync_playwright

    roles = cfg.get("roles") or [
        "software-engineer", "backend-engineer", "machine-learning-engineer",
    ]
    out: list[dict] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        browser = _launch_browser(p)
        ctx = _new_context(browser)
        page = ctx.new_page()
        try:
            for role in roles:
                url = f"https://wellfound.com/role/l/{role}/india?page=1"
                try:
                    page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                page.wait_for_timeout(4000)

                data = _extract_next_data(page)
                if data:
                    for j in _walk_apollo_jobs(data):
                        u = j.get("url") or ""
                        if u and u not in seen and re.search(r"/jobs/\d+", u):
                            seen.add(u)
                            out.append(j)

                # Link scrape fallback
                for a in page.query_selector_all("a[href*='/jobs/']"):
                    href = (a.get_attribute("href") or "").split("?")[0]
                    if not re.search(r"wellfound\.com/jobs/\d+", href):
                        continue
                    if href in seen:
                        continue
                    seen.add(href)
                    title = (a.inner_text() or "").strip() or "Role"
                    out.append({
                        "company": "",
                        "title": title,
                        "location": "India",
                        "url": href if href.startswith("http") else f"https://wellfound.com{href}",
                        "updated": "",
                        "description": "",
                    })
                time.sleep(POLITE_DELAY)
        finally:
            browser.close()

    if not out:
        raise SkipSource("Wellfound Playwright returned 0 individual jobs")
    return out


def fetch_cutshort_playwright(cfg: dict) -> list[dict]:
    if not _playwright_available():
        raise SkipSource("Playwright not installed")

    from playwright.sync_api import sync_playwright

    paths = cfg.get("paths") or [
        "/jobs/backend-jobs",
        "/jobs/machine-learning-jobs",
        "/jobs/software-engineer-jobs",
    ]
    out: list[dict] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        browser = _launch_browser(p)
        ctx = _new_context(browser)
        page = ctx.new_page()
        try:
            for path in paths:
                url = f"https://cutshort.io{path}"
                captured: list[Any] = []

                def on_resp(response):
                    u = response.url
                    if "cutshort.io" not in u:
                        return
                    if not any(x in u for x in ("/api/", "/job", "search", "graphql")):
                        return
                    try:
                        if response.status == 200:
                            captured.append(response.json())
                    except Exception:
                        pass

                page.on("response", on_resp)
                try:
                    page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                page.wait_for_timeout(4000)
                page.remove_listener("response", on_resp)

                def walk_jobs(obj):
                    if isinstance(obj, dict):
                        title = obj.get("title") or obj.get("headline")
                        link = obj.get("url") or obj.get("publicUrl") or obj.get("slug")
                        if title and link:
                            if not link.startswith("http"):
                                link = f"https://cutshort.io/job/{link.lstrip('/')}"
                            if "/job/" in link and link not in seen:
                                company = (
                                    obj.get("companyName")
                                    or (obj.get("company") or {}).get("name", "")
                                    if isinstance(obj.get("company"), dict)
                                    else obj.get("company", "")
                                )
                                seen.add(link)
                                out.append({
                                    "company": str(company or ""),
                                    "title": str(title),
                                    "location": str(obj.get("location") or "India"),
                                    "url": link,
                                    "updated": "",
                                    "description": clean(obj.get("description") or ""),
                                })
                        for v in obj.values():
                            walk_jobs(v)
                    elif isinstance(obj, list):
                        for i in obj:
                            walk_jobs(i)

                for data in captured:
                    walk_jobs(data)

                nd = _extract_next_data(page)
                if nd:
                    walk_jobs(nd)

                for a in page.query_selector_all("a[href*='/job/']"):
                    href = (a.get_attribute("href") or "").split("?")[0]
                    if "/jobs/" in href or href in seen:
                        continue
                    if not href.startswith("http"):
                        href = f"https://cutshort.io{href}"
                    seen.add(href)
                    out.append({
                        "company": "",
                        "title": (a.inner_text() or "").strip() or "Role",
                        "location": "India",
                        "url": href,
                        "updated": "",
                        "description": "",
                    })
                time.sleep(POLITE_DELAY)
        finally:
            browser.close()

    if not out:
        raise SkipSource("Cutshort Playwright returned 0 individual jobs")
    return out
