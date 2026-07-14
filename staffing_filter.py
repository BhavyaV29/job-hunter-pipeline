"""Detect staffing agencies, bootcamps, and repost spam — not direct employers."""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Company name substrings (case-insensitive).
STAFFING_COMPANY_TOKENS = (
    "synergistic",
    "talentzo",
    "progressive technology",
    "collabera",
    "genpact",
    "cyient",
    "mindtree",
    "ltimindtree",
    "quess",
    "teamlease",
    "randstad",
    "adecco",
    "manpower",
    "kelly services",
    "hiring for",
    "client of",
    "fortified infotech",
    "blubridge",
    "careersprint",
    "hackajob",
    # Repost/training portals repeatedly seen masking the actual employer.
    "ibrowsejobs",
    "wonksknow",
    "placement services",
    "placement consultancy",
    "recruitment services",
    "recruitment agency",
    "staffing services",
    "confidential client",
)

# Bootcamp / trainee spam titles.
BOOTCAMP_TITLE_RE = re.compile(
    r"\b(?:qa trainee|trainee software|bootcamp|coding bootcamp|"
    r"learn and earn|pay after placement|no experience required|"
    r"job guarantee|placement assistance)\b",
    re.I,
)

# Vague recruiter-style titles (actual employer unnamed).
STAFFING_TITLE_RE = re.compile(
    r"\b(?:at a (?:fintech|startup|company)|hiring for|on behalf of|"
    r"our client|client location|bench|contract role|"
    r"(?:python|java|software|technical|coding|programming|data science|"
    r"machine learning) (?:trainer|instructor|faculty|tutor)|"
    r"(?:reposted|sourced) (?:by|via) (?:a )?"
    r"(?:staffing|recruiting|placement))\b",
    re.I,
)

# Repost / aggregator domains — rarely direct employer career pages.
STAFFING_URL_DOMAINS = frozenset({
    "jobgether.com", "lensa.com", "theelitejob.com", "dailyremote.com",
    "bebee.com", "jobrapido.com", "nextleap.app", "taskbyte.is-great.net",
    "hiremesh.html-5.me", "cosmoquick.com", "rockerstop.com",
    "hackajob.co", "hackajob.com", "liveblog365.com", "7f.liveblog365.com",
})


def is_staffing_listing(company: str, url: str = "", role: str = "") -> bool:
    """True for staffing/bootcamp/repost listings that should not rank as T1 jobs."""
    co = (company or "").lower()
    if any(tok in co for tok in STAFFING_COMPANY_TOKENS):
        return True
    if STAFFING_TITLE_RE.search(role or ""):
        return True
    if BOOTCAMP_TITLE_RE.search(role or ""):
        return True
    host = (urlparse(url or "").netloc or "").lower().removeprefix("www.")
    if host in STAFFING_URL_DOMAINS:
        return True
    if host.endswith(".railway.app") or host.endswith(".up.railway.app"):
        return True
    return False
