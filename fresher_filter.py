"""Seniority/experience filtering, tuned per person via the sources.yaml profile.

Defaults reproduce the fresher (~0–1 yr) behaviour; a mid/senior profile keeps
senior titles and lifts the experience ceiling.
"""
from __future__ import annotations

import re

from experience import parse_experience
from profile_config import drop_senior_titles as _profile_drop_senior
from profile_config import max_exp_years as _profile_max_exp

# Hard-drop title patterns — senior / clearly out of range before ATS noise.
_SENIOR_TITLE_RE = re.compile(
    r"\b(?:senior|staff|principal|director|head of|vp|vice president|"
    r"architect|lead engineer|tech lead|engineering manager|"
    r"sde\s*(?:iii|iv|3|4)|engineer\s*(?:iii|iv|3|4)|"
    r"\d+\s*\+\s*years?)\b",
    re.I,
)

_FRESHER_BOOST_RE = re.compile(
    r"\b(?:fresher|entry[\s-]?level|new[\s-]?grad|graduate|campus|"
    r"0[\s-]?1\s*years?|6\s*months?|trainee|early\s+career|"
    r"sde[\s-]?1|sde\s+i\b|software engineer\s+i\b|associate)\b",
    re.I,
)

# Staffing / contract noise — not direct employer hires.
_BODY_SHOP_RE = re.compile(
    r"\b(?:c2h|contract[\s-]to[\s-]hire|payroll of|on bench|bench resource|"
    r"client location|third[\s-]party|staffing agency|body shop|vendor role|"
    r"contract[\s-]only|short[\s-]term contract)\b",
    re.I,
)


def passes_fresher_filter(
    title: str,
    description: str = "",
    *,
    drop_exp_bad: bool = True,
    max_exp_years: float | None = None,
    drop_senior_titles: bool | None = None,
) -> tuple[bool, str]:
    """Return (keep, reason). max_exp_years / drop_senior_titles fall back to the
    profile when None; max_exp_years=None means no ceiling."""
    if drop_senior_titles is None:
        drop_senior_titles = _profile_drop_senior()
    if max_exp_years is None:
        max_exp_years = _profile_max_exp(fallback=2.0)

    t = (title or "").strip()
    if drop_senior_titles and _SENIOR_TITLE_RE.search(t):
        return False, "senior_title"

    blob = f"{t} {(description or '')}".lower()
    if _BODY_SHOP_RE.search(blob):
        return False, "body_shop"

    exp_years, exp_match = parse_experience(t, description)
    if drop_exp_bad and exp_match == "bad":
        return False, "exp_bad"
    if max_exp_years is not None and exp_years is not None and exp_years > max_exp_years:
        return False, f"exp_gt_{max_exp_years:g}"

    return True, ""


def fresher_title_boost(title: str, description: str = "") -> int:
    """Small score tiebreaker boost for explicit fresher/entry signals."""
    blob = f"{title} {description}".lower()
    if _FRESHER_BOOST_RE.search(blob):
        return 8
    return 0
