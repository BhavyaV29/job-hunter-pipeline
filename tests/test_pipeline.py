"""Tests for application-funnel integrity checks."""
from __future__ import annotations

from pipeline import funnel_consistency_issues


def test_funnel_consistency_reports_missing_and_misaligned_dates() -> None:
    rows = [
        {"stage": "applied", "applied_date": ""},
        {"stage": "tech_screen", "applied_date": "2026-07-01"},
        {"stage": "sourced", "applied_date": "2026-07-02"},
        {"stage": "shortlisted", "applied_date": ""},
        {"stage": "rejected", "applied_date": "not-a-date"},
    ]

    assert funnel_consistency_issues(rows) == {
        "advanced_missing_applied_date": 1,
        "pre_application_with_applied_date": 1,
        "invalid_applied_date": 1,
    }


def test_funnel_consistency_accepts_complete_progression() -> None:
    rows = [
        {"stage": "sourced", "applied_date": ""},
        {"stage": "shortlisted", "applied_date": ""},
        {"stage": "applied", "applied_date": "2026-07-01"},
        {"stage": "oa", "applied_date": "2026-07-01"},
    ]

    assert funnel_consistency_issues(rows) == {}
