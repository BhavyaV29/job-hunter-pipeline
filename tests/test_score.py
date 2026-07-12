"""Tests for profile-aware screen-likelihood ranking."""
from __future__ import annotations

import datetime as dt

import profile_config
import score


MIN_INR = 10 * 1e5
REMOTE_FLOOR_INR = 7 * 1e5


def _row(**overrides):
    row = {
        "company": "Acme",
        "role": "Backend Engineer",
        "location": "Bengaluru, India",
        "salary": "",
        "source": "linkedin_guest",
        "url": "https://www.linkedin.com/jobs/view/1",
        "date_found": dt.date.today().isoformat(),
        "posted_date": dt.date.today().isoformat(),
        "deadline": "",
        "exp_match": "unknown",
        "stage": "sourced",
        "notes": "",
    }
    row.update(overrides)
    return row


def test_profile_weight_overrides_remain_wired() -> None:
    assert score.BRAND_A_BOOST == profile_config.scalar("brand_a_boost", 10)
    assert score.DREAM_BOOST == profile_config.scalar("dream_boost", 15)
    configured = profile_config.weight_overrides("exp_match_adjust")
    assert score.EXP_MATCH_ADJUST == (
        configured or {"good": 25, "warn": 5, "bad": -80, "unknown": -8}
    )


def test_fresh_direct_eligible_role_beats_stale_high_salary_aggregator() -> None:
    stale = _row(
        company="Stripe",
        location="Remote — Worldwide",
        salary="$150k/yr",
        posted_date=(dt.date.today() - dt.timedelta(days=45)).isoformat(),
        exp_match="unknown",
    )
    fresh = _row(
        source="greenhouse",
        url="https://boards.greenhouse.io/acme/jobs/1",
        exp_match="good",
    )

    assert score.total_score(fresh, MIN_INR, REMOTE_FLOOR_INR, dreams=frozenset()) > (
        score.total_score(stale, MIN_INR, REMOTE_FLOOR_INR, dreams=frozenset())
    )


def test_official_ats_url_receives_screen_likelihood_boost() -> None:
    direct = _row(url="https://jobs.ashbyhq.com/acme/role-1")
    board = _row(url="https://jobicy.com/jobs/role-1")

    assert score._official_apply_boost(direct) == score.OFFICIAL_APPLY_BOOST
    assert score._official_apply_boost(board) == 0


def test_brand_matching_does_not_use_unsafe_substrings() -> None:
    assert score._brand_boost("Metaverse Labs", "") == 0
    assert score._brand_boost("Meta Platforms India", "") == score.BRAND_A_BOOST


def test_posted_date_controls_recency_not_discovery_date() -> None:
    old_post = _row(
        date_found=dt.date.today().isoformat(),
        posted_date=(dt.date.today() - dt.timedelta(days=40)).isoformat(),
    )
    new_post = _row(
        date_found=(dt.date.today() - dt.timedelta(days=40)).isoformat(),
        posted_date=dt.date.today().isoformat(),
    )

    assert score.total_score(new_post, MIN_INR, REMOTE_FLOOR_INR) > (
        score.total_score(old_post, MIN_INR, REMOTE_FLOOR_INR)
    )


def test_shortlisted_role_gets_small_execution_boost() -> None:
    sourced = _row(stage="sourced")
    shortlisted = _row(stage="shortlisted")

    difference = (
        score.total_score(shortlisted, MIN_INR, REMOTE_FLOOR_INR)
        - score.total_score(sourced, MIN_INR, REMOTE_FLOOR_INR)
    )
    assert difference == score.SHORTLISTED_BOOST
