"""Regression: not_applicable blacklist must survive Sheet pull."""
from __future__ import annotations

from sheets_sync import _merge_not_applicable_blacklist


def test_merge_keeps_csv_only_not_applicable() -> None:
    sheet = [
        {"url": "https://a.example/1", "stage": "sourced", "company": "A"},
        {"url": "https://a.example/2", "stage": "applied", "company": "B"},
    ]
    prior = [
        {"url": "https://a.example/1", "stage": "sourced", "company": "A"},
        {"url": "https://dismissed.example/x", "stage": "not_applicable", "company": "X"},
        {"url": "https://a.example/2", "stage": "applied", "company": "B"},
    ]
    merged, kept = _merge_not_applicable_blacklist(sheet, prior)
    assert kept == 1
    assert len(merged) == 3
    urls = {r["url"] for r in merged}
    assert "https://dismissed.example/x" in urls


def test_merge_skips_not_applicable_already_on_sheet() -> None:
    sheet = [
        {"url": "https://dismissed.example/x", "stage": "not_applicable", "company": "X"},
    ]
    prior = [
        {"url": "https://dismissed.example/x", "stage": "not_applicable", "company": "X"},
    ]
    merged, kept = _merge_not_applicable_blacklist(sheet, prior)
    assert kept == 0
    assert len(merged) == 1


def test_merge_ignores_non_blacklist_csv_only_rows() -> None:
    sheet = [{"url": "https://a.example/1", "stage": "sourced"}]
    prior = [
        {"url": "https://a.example/1", "stage": "sourced"},
        {"url": "https://only-csv.example/y", "stage": "sourced"},
    ]
    merged, kept = _merge_not_applicable_blacklist(sheet, prior)
    assert kept == 0
    assert len(merged) == 1


def test_merge_skips_blank_urls() -> None:
    sheet = [{"url": "https://a.example/1", "stage": "sourced"}]
    prior = [{"url": "", "stage": "not_applicable", "company": "Z"}]
    merged, kept = _merge_not_applicable_blacklist(sheet, prior)
    assert kept == 0
    assert len(merged) == 1
