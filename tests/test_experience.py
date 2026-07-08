"""Tests for the experience parser and the fresher experience filter.

Bands come from the sources.yaml `profile:` block; the repo default is the
`fresher` preset (<=1 good, <=2 warn), which these expectations assume.
"""
import pytest

from experience import parse_experience
from fresher_filter import is_senior_title, passes_fresher_filter


# ---- parse_experience: numeric requirements --------------------------------

@pytest.mark.parametrize(
    "text, years, match",
    [
        ("3+ years", 3.0, "bad"),
        ("2-4 years", 2.0, "warn"),
        ("2 to 4 yrs", 2.0, "warn"),
        ("minimum 5 years", 5.0, "bad"),
        ("at least 3 yrs", 3.0, "bad"),
        ("5+ YoE", 5.0, "bad"),
        ("5 y.o.e.", 5.0, "bad"),
        ("1 year of experience", 1.0, "good"),
        ("0-1 years", 0.0, "good"),
        ("2 years experience required", 2.0, "warn"),
    ],
)
def test_parse_numeric(text, years, match):
    y, m = parse_experience("Software Engineer", text)
    assert y == years
    assert m == match


def test_parse_title_embedded_range():
    y, m = parse_experience("Backend Engineer (3-5 years)", "")
    assert y == 3.0
    assert m == "bad"


def test_no_statement_is_unknown():
    y, m = parse_experience("Software Engineer", "Join our team building great products.")
    assert y is None
    assert m == "unknown"


def test_explicit_number_beats_fresher_word():
    # The main leak: a JD that mentions "entry level" but really wants 5+ years.
    y, m = parse_experience(
        "Software Engineer",
        "This is not an entry-level role. Requires 5+ years of experience.",
    )
    assert y == 5.0
    assert m == "bad"


def test_fresher_word_only_is_good():
    y, m = parse_experience("Software Engineer", "Freshers welcome to apply.")
    assert y == 0.0
    assert m == "good"


def test_min_lower_bound_across_mentions():
    # A stray high number must not drop an otherwise-junior role.
    y, _ = parse_experience("SDE", "0-2 years preferred; 5 years is a plus")
    assert y == 0.0


# ---- passes_fresher_filter -------------------------------------------------

def test_filter_drops_over_two_years():
    keep, reason = passes_fresher_filter("Backend Engineer", "Requires 3+ years")
    assert keep is False
    assert reason in ("exp_bad", "exp_gt_2")


def test_filter_keeps_two_years():
    keep, _ = passes_fresher_filter("Backend Engineer", "2 years experience")
    assert keep is True


def test_filter_keeps_unknown():
    keep, _ = passes_fresher_filter("Backend Engineer", "Build cool things.")
    assert keep is True


def test_filter_keeps_fresher():
    keep, _ = passes_fresher_filter("Graduate Software Engineer", "Freshers welcome")
    assert keep is True


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Sr. Backend Developer",
        "Staff Engineer",
        "Principal Engineer",
        "Engineering Lead",
        "Engineering Manager",
        "Director of Engineering",
        "Backend Engineer III",
    ],
)
def test_filter_drops_senior_titles(title):
    assert is_senior_title(title)
    keep, reason = passes_fresher_filter(title, drop_senior_titles=True)
    assert keep is False
    assert reason == "senior_title"


def test_senior_drop_is_configurable():
    keep, _ = passes_fresher_filter("Senior Software Engineer", drop_senior_titles=False)
    assert keep is True  # no numeric requirement, seniority check disabled


def test_threshold_is_configurable():
    keep, _ = passes_fresher_filter("Backend Engineer", "3 years", max_exp_years=5,
                                    drop_exp_bad=False)
    assert keep is True


def test_non_senior_fresher_title_not_flagged():
    assert not is_senior_title("Associate Software Engineer")
    assert not is_senior_title("Software Engineer I")
