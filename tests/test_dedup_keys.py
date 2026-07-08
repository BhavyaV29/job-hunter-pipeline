"""Tests for dedup_keys — company fuzzy match, role canonicalization, URL norm."""
from dedup_keys import (
    canonical_key,
    norm_company,
    norm_role,
    norm_url,
    role_location_key,
)


def test_company_suffix_collapse():
    a = norm_company("Wissen Technology Pvt Ltd")
    b = norm_company("Wissen Technology")
    assert a == b


def test_different_roles_same_company_kept():
    co = "Trendlyne Technologies"
    k1 = role_location_key(co, "Backend Engineer", "Bangalore")
    k2 = role_location_key(co, "Python Developer", "Bangalore")
    assert k1 != k2


def test_same_role_different_url_spellings_collide():
    k1 = role_location_key("HyrHub", "Backend SDE 1", "Bengaluru")
    k2 = role_location_key("HyrHub Technologies Private Limited", "Backend SDE 1", "Bengaluru")
    assert k1 == k2


# ---- role canonicalization (collapse work-mode / req-id / YoE noise) --------

def test_role_strips_remote_suffix():
    assert norm_role("Backend Engineer (Remote)") == norm_role("Backend Engineer")


def test_role_strips_reqid_and_years():
    assert norm_role("Backend Engineer - 3+ years - REQ12345") == norm_role("Backend Engineer")


def test_role_case_whitespace_punct_insensitive():
    assert norm_role("  BACKEND   Engineer!! ") == norm_role("backend engineer")


def test_role_keeps_levels_distinct():
    assert norm_role("SDE 1") != norm_role("SDE 2")


def test_role_keeps_seniority_distinct():
    # Dedup must not merge a junior and senior opening (senior is dropped elsewhere).
    assert norm_role("Software Engineer") != norm_role("Senior Software Engineer")


def test_role_backend_frontend_normalized():
    assert norm_role("Back End Developer") == norm_role("Backend Developer")


# ---- URL canonicalization (strip tracking params + fragment) ----------------

def test_url_strips_tracking_params():
    a = norm_url("https://boards.greenhouse.io/acme/jobs/123?utm_source=x&ref=y&se=abc")
    b = norm_url("https://boards.greenhouse.io/acme/jobs/123")
    assert a == b


def test_url_preserves_real_query_like_gh_jid():
    u = norm_url("https://acme.com/careers?gh_jid=456&utm_campaign=z")
    assert "gh_jid=456" in u
    assert "utm_campaign" not in u


def test_url_strips_fragment_www_and_trailing_slash():
    a = norm_url("https://www.example.com/jobs/9/#apply")
    b = norm_url("http://example.com/jobs/9")
    assert a == b


# ---- canonical_key (location-SENSITIVE by default) --------------------------

def test_canonical_key_location_sensitive_by_default():
    # Different cities are genuinely distinct postings — they must NOT collapse.
    k1 = canonical_key("Acme", "Backend Engineer", "Bangalore")
    k2 = canonical_key("Acme", "Backend Engineer", "Hyderabad")
    assert k1 != k2


def test_canonical_key_merges_cities_only_when_disabled():
    k1 = canonical_key("Acme", "Backend Engineer", "Bangalore", use_location=False)
    k2 = canonical_key("Acme", "Backend Engineer", "Hyderabad", use_location=False)
    assert k1 == k2


def test_canonical_key_collapses_near_duplicates_same_location():
    # Same city; company legal-suffix + role work-mode noise must still collapse.
    k1 = canonical_key("Acme Technologies Pvt Ltd", "Backend Engineer (Remote)", "Bengaluru")
    k2 = canonical_key("Acme Technologies", "Backend Engineer", "Bengaluru")
    assert k1 == k2
