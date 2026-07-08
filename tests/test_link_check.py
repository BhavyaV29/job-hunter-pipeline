"""Tests for the dead-link classifier and the async checker (mocked HTTP only)."""
import asyncio
import csv
import datetime as dt
import json

import httpx
import pytest

from dedup_keys import norm_url
from link_check import (
    DEAD,
    EXPIRED,
    LIVE,
    UNKNOWN,
    check_tracker_links,
    check_urls,
    classify,
)

LIVE_URL = "https://boards.greenhouse.io/acme/jobs/123"


# ---- classify(): status-based ---------------------------------------------

def test_200_live():
    assert classify(200, LIVE_URL, "<h1>Backend Engineer</h1> Apply now",
                    request_url=LIVE_URL) == LIVE


def test_404_dead():
    assert classify(404, LIVE_URL, "Not Found", request_url=LIVE_URL) == DEAD


def test_410_dead():
    assert classify(410, LIVE_URL, "Gone", request_url=LIVE_URL) == DEAD


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_blocked_or_transient_is_unknown(status):
    assert classify(status, LIVE_URL, "", request_url=LIVE_URL) == UNKNOWN


# ---- classify(): 200-with-expired-text, one realistic phrase per ATS -------

@pytest.mark.parametrize(
    "body",
    [
        # Greenhouse
        "This job is no longer available. We are no longer accepting applications.",
        # Lever
        "This posting is no longer active. Check our other openings.",
        # Ashby
        "This job posting is no longer active.",
        # Workday
        "The job you are looking for is no longer available.",
        # SmartRecruiters
        "This position has been filled. Thank you for your interest.",
        # LinkedIn
        "No longer accepting applications for this job.",
        # Indeed
        "This job has expired on Indeed. The job may no longer be available.",
    ],
)
def test_200_expired_per_ats(body):
    assert classify(200, LIVE_URL, body, request_url=LIVE_URL) == EXPIRED


def test_200_soft_404_is_dead():
    assert classify(200, "https://acme.com/x", "Oops — page not found",
                    request_url="https://acme.com/x") == DEAD


# ---- classify(): redirect-to-home -----------------------------------------

def test_redirect_to_board_root_is_dead():
    assert classify(200, "https://jobs.lever.co/acme", "Browse roles",
                    request_url="https://jobs.lever.co/acme/abc-123") == DEAD


def test_redirect_to_careers_home_is_dead():
    assert classify(200, "https://acme.com/careers", "All jobs",
                    request_url="https://acme.com/careers/eng/9") == DEAD


def test_no_redirect_live_posting_stays_live():
    assert classify(200, LIVE_URL, "Apply for this role", request_url=LIVE_URL) == LIVE


# ---- async check_urls() via httpx.MockTransport (no real network) ----------

def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/live"):
        return httpx.Response(200, text="Backend Engineer — Apply now")
    if path.endswith("/gone"):
        return httpx.Response(404, text="Not Found")
    if path.endswith("/removed"):
        return httpx.Response(410, text="Gone")
    if path.endswith("/expired"):
        return httpx.Response(200, text="This job is no longer available.")
    if path.endswith("/moved"):
        return httpx.Response(301, headers={"Location": "https://acme.com/"})
    if path == "/":
        return httpx.Response(200, text="Careers home")
    if path.endswith("/boom"):
        raise httpx.ConnectError("boom")
    return httpx.Response(200, text="ok")


def _check(urls):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            return await check_urls(urls, client=client, per_host_delay=0, retries=0)
    return asyncio.run(run())


def test_async_end_to_end_verdicts():
    urls = [
        "https://acme.com/live",
        "https://acme.com/gone",
        "https://acme.com/removed",
        "https://acme.com/expired",
        "https://acme.com/moved",
        "https://acme.com/boom",
    ]
    res = _check(urls)
    assert res["https://acme.com/live"]["verdict"] == LIVE
    assert res["https://acme.com/gone"]["verdict"] == DEAD
    assert res["https://acme.com/removed"]["verdict"] == DEAD
    assert res["https://acme.com/expired"]["verdict"] == EXPIRED
    assert res["https://acme.com/moved"]["verdict"] == DEAD  # redirect to home
    assert res["https://acme.com/boom"]["verdict"] == UNKNOWN  # network error


# ---- check_tracker_links(): drop-by-default, from cache (no network) --------

_TRACKER_ROWS = [
    ("Acme", "Live SWE", "sourced", "https://acme.com/live"),
    ("Acme", "Dead SWE", "sourced", "https://acme.com/dead"),
    ("Acme", "Expired SWE", "sourced", "https://acme.com/expired"),
    ("Acme", "Blocked SWE", "sourced", "https://acme.com/unknown"),
    ("Acme", "Applied Dead", "applied", "https://acme.com/applieddead"),
]
_VERDICTS = {
    "https://acme.com/live": LIVE,
    "https://acme.com/dead": DEAD,
    "https://acme.com/expired": EXPIRED,
    "https://acme.com/unknown": UNKNOWN,
    "https://acme.com/applieddead": DEAD,  # protected stage -> must survive
}


def _seed_tracker(tmp_path):
    tracker = tmp_path / "tracker.csv"
    with tracker.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "role", "stage", "url"])
        w.writerows(_TRACKER_ROWS)
    now = dt.datetime.now().isoformat(timespec="seconds")
    cache = {norm_url(u): {"verdict": v, "status": 200, "final_url": u, "checked": now}
             for u, v in _VERDICTS.items()}
    (tmp_path / ".linkcheck_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    return tracker


def _read(tracker):
    with tracker.open(newline="", encoding="utf-8") as f:
        return {r["role"]: r for r in csv.DictReader(f)}


def test_drop_dead_default_removes_dead_expired_keeps_unknown_and_protected(tmp_path):
    tracker = _seed_tracker(tmp_path)
    summary = check_tracker_links(tracker, drop_dead=True, backup=False)

    assert summary["removed"] == 2  # only the sourced dead + expired rows
    rows = _read(tracker)
    assert set(rows) == {"Live SWE", "Blocked SWE", "Applied Dead"}
    assert rows["Live SWE"]["link_status"] == LIVE
    assert rows["Blocked SWE"]["link_status"] == ""     # UNKNOWN never flagged/dropped
    assert rows["Applied Dead"]["stage"] == "applied"    # protected row preserved


def test_mark_only_keeps_all_rows(tmp_path):
    tracker = _seed_tracker(tmp_path)
    summary = check_tracker_links(tracker, drop_dead=False, backup=False)

    assert summary["removed"] == 0
    rows = _read(tracker)
    assert len(rows) == len(_TRACKER_ROWS)
    assert rows["Dead SWE"]["link_status"] == DEAD
    assert rows["Expired SWE"]["link_status"] == EXPIRED
    assert rows["Live SWE"]["link_status"] == LIVE
