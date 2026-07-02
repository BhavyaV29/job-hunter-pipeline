"""Normalized identity keys for tracker dedup (URL complement).

Collapses the same role re-listed under different aggregator URLs or slightly
different company spellings. Different roles at the same company stay separate.
"""
from __future__ import annotations

import re

# Strip legal suffixes / noise tokens for company matching only.
_COMPANY_NOISE_RE = re.compile(
    r"\b("
    r"private\s+limited|pvt\.?\s*ltd\.?|p\.?\s*ltd\.?|limited|ltd\.?"
    r"|incorporated|inc\.?|corp\.?|corporation|llc|plc"
    r"|technologies|technology|technolgies|tech|software|solutions"
    r"|services|consulting|consultancy|labs|digital|interactive"
    r"|india|indian|global|international"
    r")\b",
    re.I,
)


def norm_text(s: str) -> str:
    """Lowercase, strip punctuation edges, collapse whitespace."""
    t = (s or "").strip().lower()
    t = re.sub(r"[^\w\s/+.-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_company(company: str) -> str:
    """Canonical company slug for dedup — not for display."""
    c = norm_text(company)
    c = _COMPANY_NOISE_RE.sub(" ", c)
    c = re.sub(r"\s+", " ", c).strip()
    return c or norm_text(company)


def norm_role(role: str) -> str:
    """Light role normalization — keeps distinct titles separate."""
    r = norm_text(role)
    r = r.replace("back end", "backend").replace("front end", "frontend")
    r = re.sub(r"\s*-\s*", " ", r)
    return r


def role_location_key(company: str, role: str, location: str) -> tuple[str, str, str]:
    """Identity key: same company+role+location → one row; different roles kept."""
    return (norm_company(company), norm_role(role), norm_text(location))
