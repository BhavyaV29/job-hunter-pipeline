"""LLM enrichment must fail fast and never delay deterministic filtering."""
from __future__ import annotations

from collections import Counter

import pytest
import requests

import fetch_jobs
import llm_http
import llm_jd


def _reset_llm_state(monkeypatch) -> None:
    monkeypatch.setattr(llm_jd, "_LLM_CALLS_THIS_RUN", 0)
    monkeypatch.setattr(llm_jd, "_LLM_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(llm_jd, "_LLM_BLOCKED_REASON", "")


def _response(status: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://example.test/llm"
    response._content = b"{}"
    return response


def test_final_429_does_not_sleep(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    sleeps: list[float] = []
    calls = 0

    def fake_post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _response(429)

    monkeypatch.setattr(llm_http.requests, "post", fake_post)
    monkeypatch.setattr(llm_http.time, "sleep", sleeps.append)

    with pytest.raises(llm_http.LLMRateLimitError):
        llm_http.post_json("https://example.test/llm")

    assert calls == 2
    assert sleeps == [5.0]


def test_rate_limit_disables_enrichment_for_rest_of_run(monkeypatch) -> None:
    _reset_llm_state(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_RUN", "8")
    monkeypatch.setattr(llm_jd, "_load_cache", lambda *_args, **_kwargs: {})
    calls = 0

    def rate_limited(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise llm_http.LLMRateLimitError(
            "rate limited",
            response=_response(429),
        )

    monkeypatch.setattr(llm_jd, "_call_gemini", rate_limited)

    first = llm_jd.enrich_job(
        company="Acme",
        title="Junior Backend Engineer",
        description="Build Python and Go services. " * 5,
        url="https://example.test/jobs/1",
    )
    second = llm_jd.enrich_job(
        company="Acme",
        title="Junior Platform Engineer",
        description="Build Kubernetes platform services. " * 5,
        url="https://example.test/jobs/2",
    )

    assert first == {"keep": True}
    assert second == {"keep": True}
    assert calls == 1
    assert not llm_jd.llm_enabled()


def test_per_run_call_budget_bounds_enrichment(monkeypatch) -> None:
    _reset_llm_state(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_RUN", "1")
    monkeypatch.setattr(llm_jd, "_load_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(llm_jd, "_save_cache", lambda *_args, **_kwargs: None)
    calls = 0

    def successful(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"keep": true, "stack": "Python, Go"}'

    monkeypatch.setattr(llm_jd, "_call_gemini", successful)

    llm_jd.enrich_job(
        company="Acme",
        title="Backend Engineer",
        description="Build reliable backend services. " * 5,
        url="https://example.test/jobs/1",
    )
    llm_jd.enrich_job(
        company="Other",
        title="Platform Engineer",
        description="Build reliable platform services. " * 5,
        url="https://example.test/jobs/2",
    )

    assert calls == 1
    assert "call budget" in llm_jd._LLM_BLOCKED_REASON


def test_geo_rejection_happens_before_llm_enrichment(monkeypatch) -> None:
    llm_calls = 0

    monkeypatch.setattr(fetch_jobs, "matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        fetch_jobs,
        "passes_fresher_filter",
        lambda *_args, **_kwargs: (True, "ok"),
    )
    monkeypatch.setattr(
        fetch_jobs,
        "classify_job",
        lambda *_args, **_kwargs: ("blocked", ""),
    )
    monkeypatch.setattr(fetch_jobs, "DROP_RESULTS", {"blocked"})
    monkeypatch.setattr(fetch_jobs, "llm_enabled", lambda: True)

    def track_enrichment(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return {"keep": True}

    monkeypatch.setattr(fetch_jobs, "enrich_job", track_enrichment)

    row = fetch_jobs._process_fetched_job(
        {
            "company": "Acme",
            "title": "Junior Backend Engineer",
            "location": "US only",
            "url": "https://example.test/jobs/blocked",
            "description": "Build backend services. " * 5,
        },
        "Acme",
        "test",
        {
            "dedup_use_location": True,
            "drop_exp_bad": True,
            "max_exp_years": 2,
        },
        0,
        0,
        set(),
        set(),
        Counter(),
        Counter(),
    )

    assert row is None
    assert llm_calls == 0
