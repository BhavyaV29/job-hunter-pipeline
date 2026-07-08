"""Normalized identity keys for tracker dedup (URL complement).

Collapses the same role re-listed under different aggregator URLs, slightly
different company spellings, work-mode / req-id suffixes, or per-city location
splits. Different roles at the same company stay separate.

Pieces:
  norm_company  — legal-suffix / noise-insensitive company slug.
  norm_role     — case/punctuation-insensitive title with work-mode noise,
                  requisition IDs and years-of-experience tokens stripped, while
                  distinguishing words (backend vs frontend, SDE 1 vs SDE 2,
                  senior vs not) are kept so genuinely different openings differ.
  norm_url      — URL with tracking params (utm_*, ref, se, ...) and fragments
                  stripped, so one posting shared under different tracking links
                  collapses to a single key.
  canonical_key — the dedup identity: (company, role, location), location-sensitive
                  by default so genuinely distinct city postings stay separate;
                  true duplicates still collapse via the shared canonical URL.
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

# Work-mode / recruiting noise in titles — same role whether or not present.
_ROLE_NOISE_RE = re.compile(
    r"\b(?:"
    r"remote|hybrid|onsite|on\s?site|wfh|work\s+from\s+home|"
    r"full[\s-]?time|part[\s-]?time|contractual|permanent|"
    r"immediate\s+joiner(?:s)?|urgent(?:ly)?\s+hiring|urgently|hiring|"
    r"walk[\s-]?in|c2h|"
    r"multiple\s+locations?|any\s+location|pan\s+india"
    r")\b",
    re.I,
)

# Requisition / job-id tokens: "REQ12345", "Job Id: 998877", "#4432809".
_REQ_ID_RE = re.compile(
    r"(?:\b(?:req(?:uisition)?|job\s*id|posting|jr|jd|id)\b\s*[-#:]?\s*)?"
    r"#?[a-z]{0,4}\d{4,}\b",
    re.I,
)

# Years-of-experience tokens: "3+ years", "2-4 yrs", "5 yoe", "0-1 years".
_YEARS_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:[-\u2013\u2014]|to)?\s*\d*\s*\+?\s*(?:years?|yrs?|yoe)\b",
    re.I,
)

# Query params that never identify a posting — dropped when canonicalizing URLs.
# NOTE: greenhouse/ashby carry the real job id in gh_jid / other ids, which are
# intentionally *not* listed here and therefore preserved.
_URL_TRACKING_RE = re.compile(
    r"(?i)^(?:utm_.*|se|src|source|ref|referrer|gh_src|campaign|fbclid|gclid|"
    r"mc_.*|trk|trkid|igshid|origin|spm|_ga|cid|aff|from)$"
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
    """Canonical role slug: same title survives work-mode / req-id / YoE noise,
    but distinct titles (and levels) stay distinct.

    Conservative on purpose — seniority words and level numbers (SDE 1 vs 2) are
    preserved so genuinely different openings are never merged.
    """
    r = norm_text(role)
    r = _ROLE_NOISE_RE.sub(" ", r)
    r = _YEARS_RE.sub(" ", r)
    r = _REQ_ID_RE.sub(" ", r)
    r = re.sub(r"[^\w\s]", " ", r)        # unify / + . - separators
    r = re.sub(r"\b\d{3,}\b", " ", r)     # leftover req numbers (keep 1-2 digit levels)
    r = r.replace("back end", "backend").replace("front end", "frontend")
    r = re.sub(r"\s+", " ", r).strip()
    return r or norm_text(role)


def norm_url(url: str) -> str:
    """Canonical URL for dedup: drop scheme/www, tracking params and fragments.

    The same posting shared under different tracking links (adzuna `?se=…&utm_*`,
    board `?ref=…`) collapses to one key. Returns "" for empty input.
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = u.split("#", 1)[0]
    if "?" in u:
        base, query = u.split("?", 1)
        kept = [
            part for part in query.split("&")
            if part and not _URL_TRACKING_RE.match(part.split("=", 1)[0])
        ]
        u = base + ("?" + "&".join(kept) if kept else "")
    m = re.match(r"(?i)^(?:https?://)?(?:www\.)?([^/]+)(.*)$", u)
    if m:
        u = m.group(1).lower() + m.group(2)
    return u.rstrip("/")


def canonical_key(company: str, role: str, location: str = "",
                  *, use_location: bool = True) -> tuple:
    """Dedup identity for a posting.

    Default (use_location=True) is location-sensitive: every genuinely distinct
    posting — including the same (company, role) in a different city — stays its
    own key. TRUE duplicates still collapse: postings spammed under multiple city
    labels that share ONE canonical URL are merged by dedup_tracker_by_role via
    the URL, and identical company+role+location rows share this key. Pass
    use_location=False to also merge the same role across different cities.
    """
    key = [norm_company(company), norm_role(role)]
    if use_location:
        key.append(norm_text(location))
    return tuple(key)


def role_location_key(company: str, role: str, location: str) -> tuple[str, str, str]:
    """Location-sensitive identity key (kept for callers that want city splits)."""
    return canonical_key(company, role, location, use_location=True)  # type: ignore[return-value]
