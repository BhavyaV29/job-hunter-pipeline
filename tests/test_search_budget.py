"""Regression tests for paid-provider quota, cache, and rotation controls."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest

from search_budget import SearchBudget, SearchBudgetExhausted


def _budget(tmp_path, *, monthly: int = 10, per_run: int = 3, now=None):
    current = now or [dt.datetime(2026, 7, 12, tzinfo=dt.timezone.utc)]
    broker = SearchBudget(
        "serpapi",
        state_path=tmp_path / "state.json",
        cache_path=tmp_path / "cache.json",
        monthly_budget=monthly,
        max_calls_per_run=per_run,
        cache_ttl_hours=72,
        now=lambda: current[0],
    )
    return broker, current


def test_concurrent_routes_share_one_hard_run_budget(tmp_path) -> None:
    broker, _ = _budget(tmp_path, per_run=3)

    def reserve(route: str) -> bool:
        try:
            broker.reserve(route)
            return True
        except SearchBudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        allowed = list(pool.map(reserve, [f"route-{i}" for i in range(10)]))

    assert sum(allowed) == 3
    assert broker.snapshot()["run_calls"] == 3
    assert broker.snapshot()["month_used"] == 3


def test_monthly_budget_is_persisted_across_brokers(tmp_path) -> None:
    first, current = _budget(tmp_path, monthly=2, per_run=2)
    first.reserve("wellfound")
    first.reserve("naukri")

    second, _ = _budget(tmp_path, monthly=2, per_run=2, now=current)
    with pytest.raises(SearchBudgetExhausted, match="monthly budget exhausted"):
        second.reserve("hirist")


def test_month_rollover_resets_usage_but_keeps_rotation(tmp_path) -> None:
    broker, current = _budget(tmp_path, monthly=1, per_run=2)
    broker.rotate("wellfound", ["q1", "q2", "q3"], 1)
    broker.reserve("wellfound")
    current[0] = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)

    refreshed, _ = _budget(tmp_path, monthly=1, per_run=2, now=current)
    assert refreshed.rotate("wellfound", ["q1", "q2", "q3"], 1) == ["q2"]
    refreshed.reserve("wellfound")
    assert refreshed.snapshot()["month_used"] == 1


def test_cache_hit_uses_no_budget_and_hides_api_key(tmp_path) -> None:
    broker, _ = _budget(tmp_path)
    params = {"engine": "google", "q": "backend", "api_key": "secret-a"}
    broker.store("google", params, {"organic_results": [{"title": "Role"}]})

    cached = broker.cached(
        "google",
        {"q": "backend", "api_key": "secret-b", "engine": "google"},
    )

    assert cached == {"organic_results": [{"title": "Role"}]}
    assert broker.snapshot()["run_calls"] == 0
    assert "secret-a" not in (tmp_path / "cache.json").read_text(encoding="utf-8")


def test_expired_cache_misses(tmp_path) -> None:
    broker, current = _budget(tmp_path)
    broker.store("google", {"q": "backend"}, {"ok": True})
    current[0] += dt.timedelta(hours=73)

    assert broker.cached("google", {"q": "backend"}) is None


def test_query_rotation_covers_all_values_before_repeat(tmp_path) -> None:
    broker, _ = _budget(tmp_path)
    queries = [f"q{i}" for i in range(8)]

    batches = [broker.rotate("wellfound", queries, 2) for _ in range(4)]

    assert [q for batch in batches for q in batch] == queries
    assert broker.rotate("wellfound", queries, 2) == ["q0", "q1"]


def test_quota_circuit_blocks_sibling_routes(tmp_path) -> None:
    broker, _ = _budget(tmp_path)
    broker.reserve("wellfound")
    broker.open_circuit("monthly quota exhausted", until_next_month=True)

    with pytest.raises(SearchBudgetExhausted, match="circuit open"):
        broker.reserve("hirist")


def test_zero_run_budget_disables_provider(tmp_path) -> None:
    broker, _ = _budget(tmp_path, per_run=0)

    with pytest.raises(SearchBudgetExhausted, match="disabled"):
        broker.reserve("wellfound")
