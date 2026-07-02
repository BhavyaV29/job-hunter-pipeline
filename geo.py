"""Shared geo / remote-eligibility tokens and classification logic.

Single source of truth for fetch_jobs.py (keep/drop) and score.py (tiering).
"""
from __future__ import annotations

import re

# India location signals (country + states + cities).
INDIA_LOCATION_TOKENS = (
    "india",
    "karnataka", "telangana", "maharashtra", "haryana", "tamil nadu", "kerala",
    "gujarat", "madhya pradesh", "uttar pradesh", "west bengal", "odisha",
    "rajasthan", "bihar", "jharkhand", "nagaland", "punjab", "andhra pradesh",
    "goa", "chhattisgarh", "assam", "uttarakhand",
    "bangalore", "bengaluru", "hyderabad", "pune", "delhi", "new delhi", "noida",
    "gurgaon", "gurugram", "mumbai", "chennai", "kolkata", "chandigarh", "jaipur",
    "ahmedabad", "kochi", "indore", "coimbatore", "kozhikode", "thiruvananthapuram",
    "trivandrum", "mysuru", "mysore", "mangaluru", "nagpur", "surat", "vadodara",
    "bhubaneswar", "mohali", "gandhinagar", "ranchi", "patna", "vapi", "ghaziabad",
)

GLOBAL_REMOTE_TOKENS = ("worldwide", "anywhere", "global", "work from anywhere")

REMOTE_TOKENS = ("remote", "work from home", "wfh", "distributed",
                 "worldwide", "anywhere", "global")

# Explicit remote region restrictions (remote-US, remote-Europe, etc.).
REMOTE_REGION_LOCK_TOKENS = (
    "us only", "us-only", "usa only", "us-remote", "us - remote", "remote - us",
    "remote-us", "remote us", "remote (us", "remote-usa", "remote usa",
    "us-based", "remote - usa",
    "uk only", "uk-only", "remote - uk", "remote-uk", "remote uk", "u.k. only",
    "eu only", "eu-only", "eea only", "remote - eu", "remote-eu", "remote europe",
    "europe only", "european only", "emea only", "emea remote", "remote emea",
    "canada only", "canada-only", "remote - canada", "remote-canada", "can-remote",
    "latam only", "apac only", "anz only",
)

# Onsite foreign locations (US states/cities, etc.).
FOREIGN_ONSITE_TOKENS = (
    "united states", "u.s.", "usa", "indiana", "indianapolis",
    "indian river", "indian county", "florida", "texas", "california",
    "new york", "san francisco", "seattle", "chicago", "austin", "boston",
    "los angeles", "canada", "canadian", "toronto", "vancouver", "montreal",
    "united kingdom", "u.k.", "london", "europe", "european", "emea",
    "germany", "france", "spain", "netherlands", "ireland",
    "australia", "singapore", "japan", "china",
    "united arab emirates", "dubai", "uae",
)

FOREIGN_LOCK_TOKENS = REMOTE_REGION_LOCK_TOKENS + FOREIGN_ONSITE_TOKENS
_FOREIGN_WORD_RE = re.compile(r"\b(?:us|eu|uk)\b")

# US place names that falsely match substring "india" / Indian tokens.
_US_INDIA_FALSE_POSITIVES = (
    "indiana", "indian river", "indian county", "indianapolis",
    "indian harbour", "indian harbor", "indian trail", "indian wells",
)

DEFAULT_MIN_SALARY_LPA = 10
DEFAULT_REMOTE_FLOOR_LPA = 7

KEEP_RESULTS = frozenset({"remote_keep", "india_keep"})
DROP_RESULTS = frozenset({"remote_foreign_drop", "remote_salary_drop",
                          "india_salary_drop", "foreign_drop"})


def _has_us_india_false_positive(text: str) -> bool:
    return any(fp in text for fp in _US_INDIA_FALSE_POSITIVES)


def _token_matches(text: str, token: str) -> bool:
    """Word-boundary match so 'india' does not match 'indiana' or 'indian river'."""
    if token == "india":
        return bool(re.search(r"\bindia\b", text))
    if " " in token:
        return token in text
    return bool(re.search(r"\b" + re.escape(token) + r"\b", text))


def is_india_location(text: str) -> bool:
    if not text:
        return False
    if _has_us_india_false_positive(text):
        return False
    return any(_token_matches(text, tok) for tok in INDIA_LOCATION_TOKENS)


def is_remote(text: str, remote_flag: bool = False) -> bool:
    return bool(remote_flag) or any(tok in text for tok in REMOTE_TOKENS)


def has_india_or_global_signal(text: str) -> bool:
    if _has_us_india_false_positive(text):
        return False
    return (
        any(_token_matches(text, tok) for tok in INDIA_LOCATION_TOKENS)
        or any(tok in text for tok in GLOBAL_REMOTE_TOKENS)
    )


def has_remote_region_lock(text: str) -> bool:
    """True when remote is explicitly locked to US/EU/UK/etc. USD salary alone does NOT lock."""
    if any(tok in text for tok in REMOTE_REGION_LOCK_TOKENS):
        return True
    # "Remote" + region word e.g. "Remote, London" or "Remote (US)"
    if is_remote(text):
        if _FOREIGN_WORD_RE.search(text):
            return True
        if any(tok in text for tok in FOREIGN_ONSITE_TOKENS):
            return True
    return False


def has_foreign_lock(text: str) -> bool:
    """Onsite foreign location or explicit remote region lock."""
    if has_remote_region_lock(text):
        return True
    if not is_remote(text) and any(tok in text for tok in FOREIGN_ONSITE_TOKENS):
        return True
    return False


def remote_india_eligibility(title: str, location: str, description: str = "",
                             remote_flag: bool = False, *,
                             salary_display: str = "",
                             salary_currency: str = "") -> str:
    """Classify remote/India workability: not_remote | remote_india_eligible | remote_foreign_locked.

    India-eligible remote includes: Remote-India, worldwide/global/anywhere, bare Remote
    (even with USD pay). Locked only on explicit region restrictions (remote-US, etc.).
    """
    blob = f"{location} {title}".lower()
    if not is_remote(blob, remote_flag):
        return "not_remote"
    if has_india_or_global_signal(blob):
        return "remote_india_eligible"
    if description and has_india_or_global_signal(description.lower()):
        return "remote_india_eligible"
    if has_remote_region_lock(blob):
        return "remote_foreign_locked"
    return "remote_india_eligible"


def geo_class(role: str, location: str, salary_display: str = "") -> str:
    """Return 'remote' (India-eligible), 'india' (onsite), or 'foreign'."""
    blob = f"{location} {role}".lower()
    if any(tok in blob for tok in REMOTE_TOKENS):
        if has_india_or_global_signal(blob):
            return "remote"
        if has_remote_region_lock(blob):
            return "foreign"
        return "remote"
    if is_india_location((location or "").lower()):
        return "india"
    if any(tok in (location or "").lower() for tok in FOREIGN_ONSITE_TOKENS):
        return "foreign"
    return "foreign"


def geo_salary_result(title: str, location: str, salary_inr_annual,
                      min_lpa_inr: float, remote_floor_inr=None, *,
                      description: str = "", remote_flag: bool = False,
                      salary_display: str = "",
                      salary_currency: str = "") -> str:
    """Classify by geo + salary. Returns keep/drop result token."""
    if remote_floor_inr is None:
        remote_floor_inr = DEFAULT_REMOTE_FLOOR_LPA * 1e5
    rc = remote_india_eligibility(
        title, location, description, remote_flag,
        salary_display=salary_display, salary_currency=salary_currency,
    )
    if rc == "remote_foreign_locked":
        return "remote_foreign_drop"
    if rc == "remote_india_eligible":
        if salary_inr_annual is None or salary_inr_annual >= remote_floor_inr:
            return "remote_keep"
        return "remote_salary_drop"
    if is_india_location((location or "").lower()):
        if salary_inr_annual is None or salary_inr_annual >= min_lpa_inr:
            return "india_keep"
        return "india_salary_drop"
    return "foreign_drop"


def passes_geo_salary(title: str, location: str, salary_inr_annual,
                      min_lpa_inr: float, remote_floor_inr=None, *,
                      description: str = "", remote_flag: bool = False,
                      salary_display: str = "",
                      salary_currency: str = "") -> bool:
    return geo_salary_result(
        title, location, salary_inr_annual, min_lpa_inr, remote_floor_inr,
        description=description, remote_flag=remote_flag,
        salary_display=salary_display, salary_currency=salary_currency,
    ) in KEEP_RESULTS


def salary_display_to_inr(
    disp: str,
    *,
    usd_to_inr: float = 83.0,
    eur_to_inr: float = 90.0,
    gbp_to_inr: float = 105.0,
    cad_to_inr: float = 61.0,
    aud_to_inr: float = 55.0,
):
    """Parse tracker salary display string back to annual INR (max of range), or None."""
    if not disp:
        return None
    d = disp.strip()
    low = d.lower()
    nums = re.findall(r"\d+(?:\.\d+)?", d)
    if not nums:
        return None
    hi = max(float(n) for n in nums)
    if "lpa" in low or "lac" in low or "lakh" in low:
        return hi * 1e5
    if "cr" in low:
        return hi * 1e7
    if "₹" in d:
        return hi * 1e5
    if "c$" in low:
        return hi * 1000 * cad_to_inr
    if "a$" in low:
        return hi * 1000 * aud_to_inr
    if "$" in d:
        return hi * 1000 * usd_to_inr
    if "€" in d:
        return hi * 1000 * eur_to_inr
    if "£" in d:
        return hi * 1000 * gbp_to_inr
    return None
