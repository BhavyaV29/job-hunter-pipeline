"""Tests for keyless, high-signal source adapters."""
from __future__ import annotations

import fetch_jobs


def test_remotive_fetches_feed_once(monkeypatch) -> None:
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params))
        return {
            "jobs": [{
                "company_name": "Acme",
                "title": "Junior Backend Engineer",
                "candidate_required_location": "Worldwide",
                "url": "https://remotive.com/jobs/acme-1",
                "publication_date": "2026-07-12",
            }]
        }

    monkeypatch.setattr(fetch_jobs, "_get", fake_get)

    jobs = fetch_jobs.fetch_remotive({"categories": ["software-dev", "data", "devops"]})

    assert len(calls) == 1
    assert jobs[0]["company"] == "Acme"


def test_arbeitnow_honors_page_cap(monkeypatch) -> None:
    pages = []

    def fake_get(_url, params=None):
        page = params["page"]
        pages.append(page)
        return {
            "data": [{
                "company_name": f"Acme {page}",
                "title": "Backend Engineer",
                "location": "Remote",
                "url": f"https://arbeitnow.com/jobs/{page}",
                "created_at": 1783800000,
                "remote": True,
            }]
        }

    monkeypatch.setattr(fetch_jobs, "_get", fake_get)
    monkeypatch.setattr(fetch_jobs.time, "sleep", lambda _seconds: None)

    jobs = fetch_jobs.fetch_arbeitnow({"pages": 2})

    assert pages == [1, 2]
    assert len(jobs) == 2


def test_himalayas_keeps_india_timezone_and_drops_incompatible_timezone(
    monkeypatch,
) -> None:
    def fake_get(_url, _params=None):
        return {
            "jobs": [
                {
                    "companyName": "Acme",
                    "title": "Junior Platform Engineer",
                    "applicationLink": "https://boards.greenhouse.io/acme/jobs/1",
                    "locationRestrictions": ["India"],
                    "timezoneRestrictions": [5.5],
                    "seniority": ["Entry-level"],
                    "description": "<p>Build Kubernetes services.</p>",
                    "pubDate": 1783814400000,
                    "expiryDate": 1786406400000,
                    "minSalary": None,
                    "maxSalary": None,
                },
                {
                    "companyName": "Other",
                    "title": "Backend Engineer",
                    "applicationLink": "https://jobs.lever.co/other/2",
                    "locationRestrictions": [],
                    "timezoneRestrictions": [-8, -5],
                    "seniority": ["Entry-level"],
                    "description": "US hours only",
                    "pubDate": 1783814400000,
                    "expiryDate": 1786406400000,
                },
            ]
        }

    monkeypatch.setattr(fetch_jobs, "_get", fake_get)

    jobs = fetch_jobs.fetch_himalayas({"queries": ["platform"], "pages": 1})

    assert [job["company"] for job in jobs] == ["Acme"]
    assert "India" in jobs[0]["location"]
    assert jobs[0]["deadline"]


def test_jobicy_normalizes_remote_and_salary(monkeypatch) -> None:
    def fake_get(_url, _params=None):
        return {
            "jobs": [{
                "id": 1,
                "url": "https://jobicy.com/jobs/1-junior-backend",
                "jobTitle": "Junior Backend Engineer",
                "companyName": "Acme",
                "jobGeo": "APAC",
                "jobLevel": "Entry Level",
                "jobDescription": "<p>Python and Go.</p>",
                "pubDate": "2026-07-12T00:00:00+00:00",
                "salaryMin": 20000,
                "salaryMax": 30000,
                "salaryCurrency": "USD",
                "salaryPeriod": "yearly",
            }]
        }

    monkeypatch.setattr(fetch_jobs, "_get", fake_get)
    monkeypatch.setattr(fetch_jobs.time, "sleep", lambda _seconds: None)

    jobs = fetch_jobs.fetch_jobicy({"geos": ["apac"], "count": 100})

    assert len(jobs) == 1
    assert jobs[0]["location"] == "Remote — APAC"
    assert jobs[0]["salary"]["period"] == "YEARLY"


def test_hn_hiring_uses_official_thread_and_prefers_apply_url(monkeypatch) -> None:
    def fake_get(url, params=None):
        if url.endswith("search_by_date"):
            assert params["tags"] == "story,author_whoishiring"
            return {
                "hits": [{
                    "title": "Ask HN: Who is hiring? (July 2026)",
                    "objectID": "123",
                    "created_at": "2026-07-01T00:00:00Z",
                }]
            }
        assert url.endswith("/items/123")
        return {
            "children": [{
                "id": 456,
                "created_at": "2026-07-02T00:00:00Z",
                "text": (
                    "Acme | REMOTE (Worldwide) | Junior Backend Engineer | Python, Go"
                    '<p>Apply: <a href="https://jobs.ashbyhq.com/acme/role-1">'
                    "https://jobs.ashbyhq.com/acme/role-1</a>"
                ),
            }]
        }

    monkeypatch.setattr(fetch_jobs, "_get", fake_get)

    jobs = fetch_jobs.fetch_hn_hiring({})

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["title"] == "Junior Backend Engineer"
    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/acme/role-1"
