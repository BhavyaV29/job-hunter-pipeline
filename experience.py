"""Parse the required years-of-experience from a title + description.

good/warn/bad thresholds come from sources.yaml `profile:` (experience_bands),
defaulting to the fresher bands (<=1 good, <=2 warn) when unset.

Semantics (matter most for an early-career profile):
  * An EXPLICIT year requirement always wins over a stray "fresher"/"entry level"
    word, so "not an entry-level role, 5+ years" parses to 5 (bad), not 0. This
    is the main reason senior roles used to slip through the filter.
  * The parsed value is the MINIMUM lower bound seen ("2-4 years" -> 2, "3+" -> 3,
    "5+ YoE" -> 5). Taking the min is deliberately generous: a JD that states any
    low bound is kept even if it also mentions a higher number, so a single stray
    "5 years" reference can't drop an otherwise-junior role.
"""
from __future__ import annotations

import re

from profile_config import experience_bands

# Fresher / entry-level signals — treated as 0 years required, but ONLY when no
# explicit numeric requirement is present (explicit numbers win over these).
_FRESHER_RE = re.compile(
    r"\b(?:fresher(?:s)?|fresh\s+graduate|entry[\s-]?level|new[\s-]?grad(?:uate)?s?|"
    r"campus\s+hire|early\s+career|no\s+(?:prior\s+)?experience\s+(?:required|needed)|"
    r"0\s*[-\u2013\u2014to]+\s*1\s+years?)\b",
    re.IGNORECASE,
)

# Years unit: year(s), yr(s), yoe, y.o.e — optional trailing period.
_YEARS_UNIT = r"(?:years?|yrs?\.?|y\.?\s?o\.?\s?e\.?|yoe)"

# "3 - 5 years", "2 to 4 yrs", "0-1 years" (a range → use the lower bound).
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|\u2013|\u2014|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*" + _YEARS_UNIT + r"\b",
    re.IGNORECASE,
)

# "3+ years", "5+ YoE", "minimum 5 years", "at least 3 yrs", "3 years experience".
_SINGLE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\+?\s*" + _YEARS_UNIT + r"\b",
    re.IGNORECASE,
)


def _classify_match(years: float | None) -> str:
    if years is None:
        return "unknown"
    good_max, warn_max = experience_bands()
    if years <= good_max:
        return "good"
    if years <= warn_max:
        return "warn"
    return "bad"


def _lower_bounds(text: str) -> list[float]:
    """All explicit year lower-bounds in text (ranges contribute their low end)."""
    bounds: list[float] = []
    consumed: list[tuple[int, int]] = []
    for m in _RANGE_RE.finditer(text):
        try:
            bounds.append(min(float(m.group(1)), float(m.group(2))))
            consumed.append(m.span())
        except (TypeError, ValueError):
            continue
    for m in _SINGLE_RE.finditer(text):
        # Skip singles already covered by a range match (avoid double counting).
        if any(s <= m.start() < e for s, e in consumed):
            continue
        try:
            bounds.append(float(m.group(1)))
        except (TypeError, ValueError):
            continue
    return bounds


def parse_experience(title: str, description: str = "") -> tuple[float | None, str]:
    """Return (exp_years, exp_match) from title + description text.

    exp_years is the minimum years required (float) or None when unparseable.
    exp_match is one of: good, warn, bad, unknown.
    """
    text = f"{title or ''} {description or ''}".strip()
    if not text:
        return None, "unknown"

    bounds = _lower_bounds(text)
    if bounds:
        req = min(bounds)
        return req, _classify_match(req)

    if _FRESHER_RE.search(text):
        return 0.0, "good"

    return None, "unknown"
