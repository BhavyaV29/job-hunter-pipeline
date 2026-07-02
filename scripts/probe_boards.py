#!/usr/bin/env python3
"""Hit-and-trial probes for Naukri / Hirist / Cutshort / Wellfound.

Run: cd jobsearch && python3 scripts/probe_boards.py
Does NOT touch tracker.csv or paid APIs (JSearch/Adzuna/SerpApi).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 25


def ok(name: str, detail: str, sample: str = "") -> None:
    print(f"  ✅ {name}: {detail}")
    if sample:
        print(f"     sample: {sample[:120]}")


def fail(name: str, detail: str) -> None:
    print(f"  ❌ {name}: {detail}")


def skip(name: str, detail: str) -> None:
    print(f"  ⏭  {name}: {detail}")


def probe_hirist_api() -> bool:
    print("\n=== HIRIST: direct jobfeed API ===")
    headers = {
        "User-Agent": UA,
        "Host": "jobseeker-api.hirist.com",
        "Accept": "application/json",
        "Origin": "https://www.hirist.com",
        "Referer": "https://www.hirist.com/",
        "Authorization": "Bearer undefined",
    }
    variants = [
        ("SO params pageNo=0", {
            "pageNo": "0", "query": "backend", "loc": "17",
            "minexp": "0", "maxexp": "0", "range": "0", "boost": "0",
            "searchRange": "4", "searchOp": "AND", "jobType": "1",
        }),
        ("SO params pageNo=1", {
            "pageNo": "1", "query": "software engineer", "loc": "",
            "minexp": "0", "maxexp": "0", "range": "0", "boost": "0",
            "searchRange": "4", "searchOp": "AND", "jobType": "1",
        }),
        ("old params page=1 size=10", {"query": "backend", "page": "1", "size": "10"}),
        (".tech origin", None),  # special — swap origin below
    ]
    url = "https://jobseeker-api.hirist.com/jobfeed/-1/search"
    for label, params in variants:
        h = dict(headers)
        if label == ".tech origin":
            h["Origin"] = "https://www.hirist.tech"
            h["Referer"] = "https://www.hirist.tech/"
            params = {"pageNo": "1", "query": "backend", "loc": "", "minexp": "0",
                      "maxexp": "0", "range": "0", "boost": "0", "searchRange": "4",
                      "searchOp": "AND", "jobType": "1"}
        try:
            r = requests.get(url, headers=h, params=params, timeout=TIMEOUT)
        except Exception as e:
            fail(label, f"{type(e).__name__}: {e}")
            continue
        if r.status_code != 200:
            fail(label, f"HTTP {r.status_code} — {r.text[:80]!r}")
            continue
        try:
            data = r.json()
        except ValueError:
            fail(label, f"non-JSON: {r.text[:80]!r}")
            continue
        jobs = data if isinstance(data, list) else (data.get("jobs") or [])
        if not jobs:
            fail(label, "200 but 0 jobs")
            continue
        j = jobs[0]
        title = j.get("title", "?")
        link = j.get("jobDetailUrl") or j.get("jobUrl") or "?"
        ok(label, f"{len(jobs)} jobs", f"{title} | {link}")
        return True
    return False


def probe_naukri_api() -> bool:
    print("\n=== NAUKRI: jobapi/v3/search ===")
    session = requests.Session()
    nk = (
        "Ppy0YK9uSHqPtG3bEejYc04RTpUN2CjJOrqA68tzQt0SKJHXZKzz9M8cZtKLVkoOuQmfe4cTb1r2CwfHaxW5Tg=="
    )
    session.headers.update({
        "User-Agent": UA,
        "accept": "application/json",
        "appid": "109",
        "clientid": "d3skt0p",
        "systemid": "Naukri",
        "Nkparam": nk,
    })
    try:
        session.get("https://www.naukri.com/", timeout=TIMEOUT)
        time.sleep(1)
    except Exception as e:
        skip("warmup", str(e))

    params = {
        "noOfResults": "5",
        "urlType": "search_by_keyword",
        "searchType": "adv",
        "keyword": "backend developer",
        "pageNo": "1",
        "k": "backend developer",
        "seoKey": "backend-developer-jobs",
        "src": "jobsearchDesk",
        "latLong": "",
        "location": "Bangalore",
        "experience": "0",
    }
    try:
        r = session.get("https://www.naukri.com/jobapi/v3/search", params=params, timeout=TIMEOUT)
    except Exception as e:
        fail("jobapi", f"{type(e).__name__}: {e}")
        return False

    if r.status_code != 200:
        fail("jobapi", f"HTTP {r.status_code} — {r.text[:120]!r}")
        return False
    try:
        data = r.json()
    except ValueError:
        fail("jobapi", f"non-JSON: {r.text[:80]!r}")
        return False
    jobs = data.get("jobDetails") or []
    if not jobs:
        fail("jobapi", "200 but 0 jobDetails")
        return False
    j = jobs[0]
    ok("jobapi", f"{len(jobs)} jobs", f"{j.get('title')} @ {j.get('companyName')}")
    return True


def probe_naukri_rss() -> bool:
    print("\n=== NAUKRI: RSS feed ===")
    try:
        r = requests.get(
            "https://www.naukri.com/rss/jobsearch.php",
            params={"keyword": "backend", "experience": 0, "noOfResults": 5},
            headers={"User-Agent": UA, "Accept": "application/rss+xml"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        fail("RSS", str(e))
        return False
    if r.status_code != 200:
        fail("RSS", f"HTTP {r.status_code}")
        return False
    if r.text.strip().startswith("<!") or "DOCTYPE html" in r.text[:200]:
        fail("RSS", "HTML/captcha instead of XML")
        return False
    if "<item>" in r.text:
        ok("RSS", "valid XML with items")
        return True
    fail("RSS", "XML but no items")
    return False


def probe_cutshort() -> bool:
    print("\n=== CUTSHORT: endpoints ===")
    urls = [
        ("HTML /jobs/backend-jobs", "https://cutshort.io/jobs/backend-jobs"),
        ("HTML /job/ slug page", "https://cutshort.io/jobs/backend-developer-jobs-in-bangalore-bengaluru"),
        ("JSON-LD in HTML", "https://cutshort.io/jobs/backend-jobs"),
    ]
    api_paths = [
        "https://cutshort.io/api/v1/jobs/search?query=backend&limit=5",
        "https://cutshort.io/api/public/jobs?skills=backend&limit=5",
        "https://cutshort.io/api/job/list?skills=backend",
    ]
    worked = False
    for label, url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        except Exception as e:
            fail(label, str(e))
            continue
        if r.status_code != 200:
            fail(label, f"HTTP {r.status_code}")
            continue
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
        jobs_in_nd = 0
        if m:
            try:
                nd = json.loads(m.group(1))
                blob = json.dumps(nd)
                jobs_in_nd = blob.count('"title"')  # rough signal
            except json.JSONDecodeError:
                pass
        job_links = len(re.findall(r'href="(/job/[^"]+)"', r.text))
        ld_json = len(re.findall(r'application/ld\+json', r.text))
        detail = f"__NEXT_DATA__ titles~{jobs_in_nd}, /job/ links={job_links}, ld+json={ld_json}"
        if job_links > 0 or jobs_in_nd > 3:
            ok(label, detail)
            worked = True
        else:
            fail(label, f"client-rendered empty — {detail}")

    for path in api_paths:
        try:
            r = requests.get(path, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=TIMEOUT)
        except Exception as e:
            fail(path, str(e))
            continue
        if r.status_code == 200 and r.text.strip().startswith("{"):
            ok(f"API {path.split('/')[-1]}", r.text[:100])
            worked = True
        else:
            fail(f"API {path}", f"HTTP {r.status_code} — {r.text[:60]!r}")
    return worked


def probe_wellfound() -> bool:
    print("\n=== WELLFOUND: direct fetch ===")
    urls = [
        "https://wellfound.com/role/l/software-engineer/india?page=1",
        "https://wellfound.com/jobs/3647101-backend-engineer",
    ]
    worked = False
    for url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=TIMEOUT)
        except Exception as e:
            fail(url, str(e))
            continue
        if r.status_code != 200:
            fail(url, f"HTTP {r.status_code}")
            continue
        if "cloudflare" in r.text.lower() or "challenge" in r.text.lower():
            fail(url, "Cloudflare challenge page")
            continue
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
        apollo_jobs = 0
        if m:
            blob = m.group(1)
            apollo_jobs = blob.count("JobListing") + blob.count("StartupResult")
        job_links = len(re.findall(r'wellfound\.com/jobs/\d+', r.text))
        if apollo_jobs > 0 or job_links > 0:
            ok(url.split("/")[-1], f"apollo refs~{apollo_jobs}, job links={job_links}")
            worked = True
        else:
            fail(url.split("/")[-1], f"200 but no Apollo/job data — len={len(r.text)}")
    return worked


def probe_playwright() -> dict[str, bool]:
    print("\n=== PLAYWRIGHT intercept (only if installed) ===")
    results = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        skip("playwright", "not installed — uv pip install playwright && playwright install chromium")
        return results

    probes = [
        ("hirist", "https://www.hirist.tech/k/backend-jobs", "jobseeker-api.hirist"),
        ("naukri", "https://www.naukri.com/backend-developer-jobs?k=backend+developer&experience=0", "jobapi/v3/search"),
        ("cutshort", "https://cutshort.io/jobs/backend-jobs", "cutshort.io"),
        ("wellfound", "https://wellfound.com/role/l/software-engineer/india?page=1", "wellfound.com"),
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        page = ctx.new_page()
        for name, url, intercept in probes:
            captured = []
            def on_resp(response, _i=intercept):
                if _i in response.url and response.status == 200:
                    try:
                        captured.append(response.json())
                    except Exception:
                        try:
                            captured.append({"html_len": len(response.text())})
                        except Exception:
                            pass
            page.on("response", on_resp)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(5000)
            except Exception as e:
                fail(f"PW {name}", f"nav {type(e).__name__}")
                results[name] = False
            else:
                nd = page.evaluate("() => document.querySelector('#__NEXT_DATA__')?.textContent?.length || 0")
                job_links = page.evaluate("""() =>
                    document.querySelectorAll("a[href*='/jobs/'], a[href*='/job/'], a[href*='/j/']").length
                """)
                api_jobs = 0
                for c in captured:
                    if isinstance(c, dict):
                        jobs = c.get("jobs") or c.get("jobDetails") or []
                        if isinstance(jobs, list):
                            api_jobs += len(jobs)
                if api_jobs > 0:
                    ok(f"PW {name}", f"intercepted API: {api_jobs} jobs, links={job_links}")
                    results[name] = True
                elif job_links > 0:
                    ok(f"PW {name}", f"DOM links={job_links}, __NEXT_DATA__ len={nd}")
                    results[name] = True
                elif nd > 500:
                    ok(f"PW {name}", f"__NEXT_DATA__ len={nd} (may need Apollo parse)")
                    results[name] = True
                else:
                    title = page.title()
                    fail(f"PW {name}", f"blocked/empty — title={title!r}, captured={len(captured)}")
                    results[name] = False
            page.remove_listener("response", on_resp)
        browser.close()
    return results


def main():
    print("Board probe — hit-and-trial, no pipeline, no paid APIs\n")
    wins = {
        "hirist_api": probe_hirist_api(),
        "naukri_api": probe_naukri_api(),
        "naukri_rss": probe_naukri_rss(),
        "cutshort": probe_cutshort(),
        "wellfound": probe_wellfound(),
    }
    pw = probe_playwright()
    wins.update({f"pw_{k}": v for k, v in pw.items()})

    print("\n=== SUMMARY (integrate only ✅) ===")
    for k, v in wins.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print("""
Proven from hit-and-trial (2026-06-26):
  ✅ cutshort  → city paths __NEXT_DATA__ (HTTP, ~50 jobs/path, no key)
  ✅ naukri    → Playwright HEADED intercepts jobapi/v3/search (~20/query)
  ❌ hirist    → API 503 from this IP; __NEXT_DATA__ empty; use SerpApi
  ❌ wellfound → HTTP 403 + Playwright blocked; use SerpApi
  ❌ naukri jobapi direct → 406 recaptcha (needs fresh Nkparam)
  ❌ naukri RSS → captcha HTML
""")


if __name__ == "__main__":
    main()
