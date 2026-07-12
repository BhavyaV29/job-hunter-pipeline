"""Aggregate fetch telemetry must be useful without leaking row-level data."""
from __future__ import annotations

import json
from collections import Counter

from fetch_jobs import _append_fetch_metrics, _record_source_metrics


def test_fetch_metrics_are_aggregate_only(tmp_path) -> None:
    sources = {}
    _record_source_metrics(
        sources,
        "himalayas",
        fetched=20,
        added=3,
        outcomes=Counter({"accepted": 3, "duplicate": 17}),
        geo=Counter({"remote_keep": 3}),
    )
    path = tmp_path / "metrics.jsonl"

    _append_fetch_metrics(
        path,
        {
            "schema_version": 1,
            "timestamp": "2026-07-12T00:00:00+00:00",
            "new_rows": 3,
            "tracked_before": 100,
            "sources": sources,
            "providers": {},
            "funnel_by_source": {"himalayas": {"sourced": 3}},
        },
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["sources"]["himalayas"]["fetched"] == 20
    assert payload["sources"]["himalayas"]["outcomes"]["duplicate"] == 17
    serialized = json.dumps(payload).lower()
    for forbidden in ("company", "role", "url", "query", "api_key", "email"):
        assert forbidden not in serialized
