"""Parse the required years-of-experience from a title + description.

good/warn/bad thresholds come from sources.yaml `profile:` (experience_bands),
defaulting to the fresher bands (<=1 good, <=2 warn) when unset.
"""
from __future__ import annotations

import re

from profile_config import experience_bands

# Fresher / entry-level signals — treat as 0 years required.
_FRESHER_RE = re.compile(
    r"\b(?:fresher(?:s)?|entry[\s-]?level|new[\s-]?grad(?:uate)?s?|"
    r"campus\s+hire|early\s+career|no\s+experience\s+required|"
    r"0[\s-]?1\s+years?)\b",
    re.IGNORECASE,
)

# "3+ years", "3 - 5 years", "minimum 2 years", "2 yrs experience", "0-1 years"
_YEARS_PATTERNS = [
    # range first: "3-5 years", "3 - 5 yrs"
    re.compile(
        r"(?:minimum|min\.?|at\s+least|)\s*"
        r"(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*"
        r"(?:\+?\s*)?(?:years?|yrs?\.?)(?:\s+of)?(?:\s+experience)?",
        re.IGNORECASE,
    ),
    # "3+ years", "3 + years"
    re.compile(
        r"(?:minimum|min\.?|at\s+least|)\s*"
        r"(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?\.?)(?:\s+of)?(?:\s+experience)?",
        re.IGNORECASE,
    ),
    # "minimum 2 years", "2 years experience", "2 yrs exp"
    re.compile(
        r"(?:minimum|min\.?|at\s+least|)\s*"
        r"(\d+(?:\.\d+)?)\s*(?:\+?\s*)?(?:years?|yrs?\.?)(?:\s+of)?(?:\s+(?:relevant\s+)?experience|exp\.?)?",
        re.IGNORECASE,
    ),
]


def _classify_match(years: float | None) -> str:
    if years is None:
        return "unknown"
    good_max, warn_max = experience_bands()
    if years <= good_max:
        return "good"
    if years <= warn_max:
        return "warn"
    return "bad"


def parse_experience(title: str, description: str = "") -> tuple[float | None, str]:
    """Return (exp_years, exp_match) from title + description text.

    exp_years is the minimum years required (float) or None when unparseable.
    exp_match is one of: good, warn, bad, unknown.
    """
    text = f"{title or ''} {description or ''}".strip()
    if not text:
        return None, "unknown"

    if _FRESHER_RE.search(text):
        return 0.0, "good"

    best: float | None = None
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(text):
            try:
                lo = float(m.group(1))
                hi = float(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else lo
                req = min(lo, hi)
            except (TypeError, ValueError, IndexError):
                continue
            if best is None or req < best:
                best = req

    return best, _classify_match(best)
