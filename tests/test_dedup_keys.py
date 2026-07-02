"""Tests for dedup_keys — company fuzzy match, role preservation."""
from dedup_keys import norm_company, role_location_key


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
