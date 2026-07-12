# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""
Rank tracker.csv by likelihood of earning a screen: posting freshness, explicit
experience fit, official employer application path, and profile-aware stack
alignment. Compensation/remote tiers remain visible desirability signals, but
no longer bury a fresh direct role beneath a stale aggregator listing.

Usage:
    uv run score.py            # rank stage=sourced roles (your triage queue)
    uv run score.py --all      # rank every row, any stage
    uv run score.py --top 40

Edit KEYWORDS / NEGATIVE below to match your strengths.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from pathlib import Path
from urllib.parse import urlsplit

from geo import (
    DEFAULT_MIN_SALARY_LPA,
    DEFAULT_REMOTE_FLOOR_LPA,
    geo_class,
    salary_display_to_inr,
)
from dedup_keys import norm_company
from fresher_filter import fresher_title_boost
from staffing_filter import is_staffing_listing
from profile_config import weight_overrides, scalar

# Positive title signals; defaults suit a fresher SDE/ML search.
# Override wholesale via profile.boost_keywords in sources.yaml.
KEYWORDS = weight_overrides("boost_keywords") or {
    # core strengths
    "backend": 5, "back end": 5, "distributed": 5, "platform": 4, "infrastructure": 4,
    "go": 4, "golang": 4, "python": 3, "kubernetes": 4, "k8s": 4, "redis": 3,
    "mongodb": 3, "postgres": 3, "fastapi": 4, "docker": 3, "mcp": 4,
    "applied ai": 5, "ai engineer": 4, "agent": 4, "llm": 4, "machine learning": 3,
    "mle": 5, "ml": 2, "sre": 3, "reliability": 3,
    "api": 2, "microservice": 3, "systems": 3,
    # fresher / new-grad / entry-level boosts
    "new grad": 6, "graduate": 5, "entry": 5, "fresher": 6, "early career": 5,
    "trainee": 5, "campus": 4,
    "associate": 4, "junior": 4, "sde": 4, "sde 1": 5, "sde i": 5,
    "software engineer 1": 5, "software engineer i": 5,
}
NEGATIVE = weight_overrides("negative_keywords") or {
    "senior": -6, "sr.": -6, "staff": -7, "principal": -8, "manager": -6,
    "director": -8, "lead": -5, "head of": -8, "vp": -6, "intern": -4,
    "architect": -5, " ii": -5, " iii": -6, " iv": -7, "sde 2": -5, "sde ii": -5,
}

BRAND_TIERS = {
    # Tier A: top-paying global companies, India offices well-known for high comp
    "tier_a": {
        "google", "meta", "microsoft", "apple", "amazon", "netflix",
        "stripe", "coinbase", "databricks", "openai", "anthropic",
        "deepmind", "nvidia", "uber", "airbnb", "linkedin", "salesforce",
        "atlassian", "adobe", "twilio", "datadog", "cloudflare",
        "figma", "notion", "vercel", "hashicorp",
    },
    # Tier B: well-funded, known to pay competitively in India
    "tier_b": {
        "flipkart", "swiggy", "zomato", "razorpay", "cred", "zepto",
        "meesho", "groww", "slice", "browserstack", "freshworks",
        "chargebee", "postman", "hasura", "setu", "niyo", "fi",
        "phonepe", "paytm", "navi", "smallcase", "zerodha", "upstox",
        "goldman sachs", "jp morgan", "jpmorgan", "morgan stanley",
        "deutsche bank", "wells fargo", "barclays", "hsbc",
        "oracle", "sap", "cisco", "qualcomm", "samsung", "intel",
        "thoughtworks", "publicis sapient", "persistent", "mphasis",
        "linear", "ramp", "rippling",
    },
}
BRAND_A_BOOST = scalar("brand_a_boost", 10)
BRAND_B_BOOST = scalar("brand_b_boost", 5)
DREAM_BOOST = scalar("dream_boost", 15)
# Staffing/bootcamp/repost listings cap below real product-company roles.
STAFFING_SCORE_CAP = 20

# Geography boosts (override via profile.location_boosts).
LOCATION_BOOST = weight_overrides("location_boosts") or {
    # Top tech hubs — higher pay density, more eng roles
    "bengaluru": 10, "bangalore": 10, "gurgaon": 10, "gurugram": 10,
    "hyderabad": 10, "pune": 10,
    # Other Indian cities
    "delhi": 4, "noida": 4, "mumbai": 4, "chennai": 4, "india": 3,
    "remote": 2, "anywhere": 2, "worldwide": 1,
}

# Remote is strongly PREFERRED (not required): onsite roles stay in the list but
# rank lower WITHIN their tier. Any of these signals in the location OR title adds
# a big boost.
REMOTE_BOOST = scalar("remote_boost", 8)
REMOTE_SIGNALS = ("remote", "worldwide", "anywhere", "work from home", "wfh",
                  "distributed")

# Freshness is the strongest screen-likelihood feature. `posted_date` is used
# when available; older rows fall back to `date_found`.
RECENCY_BUCKETS = (
    (3, 45),
    (7, 30),
    (14, 12),
    (21, 0),
    (30, -20),
)

# Compensation desirability is useful but subordinate to freshness/eligibility.
TIER_SCORE = {1: 25, 2: 20, 3: 10, 4: 0, 0: -100}

# Urgency boost for T1/T2 roles whose deadline is within EXPIRY_WARN_DAYS days.
EXPIRY_WARN_DAYS = 7
URGENCY_BOOST = 15

# Experience-match adjustments (override via profile.exp_match_adjust).
EXP_MATCH_ADJUST = weight_overrides("exp_match_adjust") or {
    "good": 25, "warn": 5, "bad": -80, "unknown": -8,
}
OFFICIAL_APPLY_BOOST = scalar("official_apply_boost", 25)
SHORTLISTED_BOOST = scalar("shortlisted_boost", 15)
DIRECT_ATS_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "workable", "recruitee",
    "workday", "smartrecruiters",
})
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "recruitee.com", "smartrecruiters.com", "myworkdayjobs.com",
)
AGGREGATOR_HOSTS = (
    "linkedin.com", "naukri.com", "cutshort.io", "wellfound.com",
    "hirist.com", "remotive.com", "remoteok.com", "jobicy.com",
    "himalayas.app", "adzuna.", "themuse.com", "arbeitnow.com",
)


def _recency(date_found: str) -> int:
    try:
        d = dt.date.fromisoformat((date_found or "").strip())
    except ValueError:
        return 0
    age = (dt.date.today() - d).days
    if age < 0:
        return 0
    for max_days, value in RECENCY_BUCKETS:
        if age <= max_days:
            return value
    return -45


def _is_remote(role: str, location: str) -> bool:
    blob = f"{role or ''} {location or ''}".lower()
    return any(sig in blob for sig in REMOTE_SIGNALS)


def score(role: str, location: str = "", date_found: str = "", description: str = "") -> int:
    """Fine-grained stack/keyword FIT score (the within-tier tiebreaker)."""
    t = (role or "").lower()
    loc = (location or "").lower()
    base = sum(w for k, w in KEYWORDS.items() if k in t) + sum(
        w for k, w in NEGATIVE.items() if k in t
    )
    base += max((w for k, w in LOCATION_BOOST.items() if k in loc), default=0)
    if _is_remote(role, location):
        base += REMOTE_BOOST
    base += fresher_title_boost(role, description)
    return base + _recency(date_found)


def salary_to_inr(disp: str):
    """Parse tracker salary display → annual INR (shared logic in geo.py)."""
    return salary_display_to_inr(disp)


def _company_name_matches(company: str, target: str) -> bool:
    company_slug = norm_company(company)
    target_slug = norm_company(target)
    if not company_slug or not target_slug:
        return False
    return (
        company_slug == target_slug
        or company_slug.startswith(target_slug + " ")
        or company_slug.endswith(" " + target_slug)
    )


def _brand_boost(company: str, salary_display: str) -> int:
    """Return prestige boost when salary is unknown, using brand as a pay proxy.

    Skipped entirely when salary is known — the actual salary already captures
    compensation quality and we don't want to double-count.
    """
    if salary_to_inr(salary_display) is not None:
        return 0
    for name in BRAND_TIERS["tier_a"]:
        if _company_name_matches(company, name):
            return BRAND_A_BOOST
    for name in BRAND_TIERS["tier_b"]:
        if _company_name_matches(company, name):
            return BRAND_B_BOOST
    return 0


def _official_apply_boost(row: dict) -> int:
    if (row.get("source") or "").strip().lower() in DIRECT_ATS_SOURCES:
        return OFFICIAL_APPLY_BOOST
    try:
        parsed = urlsplit((row.get("url") or "").strip())
    except ValueError:
        return 0
    host = (parsed.hostname or "").lower()
    if any(marker in host for marker in ATS_HOSTS):
        return OFFICIAL_APPLY_BOOST
    if any(marker in host for marker in AGGREGATOR_HOSTS):
        return 0
    path = (parsed.path or "").lower()
    if any(marker in path for marker in ("/careers", "/jobs", "/apply")):
        return OFFICIAL_APPLY_BOOST
    return 0


def tier_of(role: str, location: str, salary_display: str,
            min_inr: float, remote_floor_inr: float) -> int:
    """Return the ranking tier 1..4 (0 = below floor / foreign — shouldn't appear
    in a pruned tracker, sorted to the bottom)."""
    geo = geo_class(role, location, salary_display)
    if geo == "foreign":
        return 0
    sal = salary_to_inr(salary_display)
    if sal is None:
        return 4  # unknown salary, kept on benefit-of-the-doubt
    if geo == "remote":
        if sal >= min_inr:
            return 1
        if sal >= remote_floor_inr:
            return 3
        return 0
    # India onsite
    return 2 if sal >= min_inr else 0


def load_thresholds(sources_path) -> tuple[float, float]:
    """Read (min_salary_lpa, remote_floor_lpa) from sources.yaml as annual-INR
    figures, falling back to defaults. Tiny regex reader keeps score.py dep-free."""
    try:
        text = Path(sources_path).read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_MIN_SALARY_LPA * 1e5, DEFAULT_REMOTE_FLOOR_LPA * 1e5

    def _read(key: str, default: float) -> float:
        m = re.search(rf"^\s*{key}\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.M)
        return float(m.group(1)) if m else default

    return (_read("min_salary_lpa", DEFAULT_MIN_SALARY_LPA) * 1e5,
            _read("remote_floor_lpa", DEFAULT_REMOTE_FLOOR_LPA) * 1e5)


def load_expiry_warn_days(sources_path) -> int:
    """Read expiry_warn_days from sources.yaml (filters block), fallback to EXPIRY_WARN_DAYS."""
    try:
        text = Path(sources_path).read_text(encoding="utf-8")
    except OSError:
        return EXPIRY_WARN_DAYS
    m = re.search(r"^\s*expiry_warn_days\s*:\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else EXPIRY_WARN_DAYS


def load_dream_companies(sources_path) -> frozenset[str]:
    """Personal target list from sources.yaml dream_companies: block."""
    try:
        import yaml
        data = yaml.safe_load(Path(sources_path).read_text(encoding="utf-8"))
        raw = data.get("dream_companies") or []
        return frozenset(str(n).strip().lower() for n in raw if str(n).strip())
    except Exception:
        return frozenset()


def _dream_boost(company: str, dreams: frozenset[str]) -> int:
    if not dreams:
        return 0
    for name in dreams:
        if _company_name_matches(company, name):
            return DREAM_BOOST
    return 0


def _exp_match_adjust(row: dict) -> int:
    """Penalize roles requiring too much experience for a fresher profile."""
    return EXP_MATCH_ADJUST.get((row.get("exp_match") or "").strip().lower(), 0)


def total_score(row: dict, min_inr: float, remote_floor_inr: float,
                warn_days: int = EXPIRY_WARN_DAYS,
                dreams: frozenset[str] | None = None) -> int:
    """Screen-likelihood score plus a smaller compensation/desirability signal."""
    if dreams is None:
        dreams = load_dream_companies(Path(__file__).parent / "sources.yaml")
    tier = tier_of(row.get("role", ""), row.get("location", ""),
                   row.get("salary", ""), min_inr, remote_floor_inr)
    fit = score(
        row.get("role", ""),
        row.get("location", ""),
        row.get("posted_date") or row.get("date_found", ""),
        row.get("notes", ""),
    )
    base = TIER_SCORE.get(tier, 0) + fit + _brand_boost(
        row.get("company", ""), row.get("salary", "")
    ) + _exp_match_adjust(row) + _dream_boost(
        row.get("company", ""), dreams
    ) + _official_apply_boost(row)
    if (row.get("stage") or "").strip().lower() == "shortlisted":
        base += SHORTLISTED_BOOST
    if is_staffing_listing(
        row.get("company", ""), row.get("url", ""), row.get("role", "")
    ):
        base = min(base, STAFFING_SCORE_CAP)
    # Urgency boost: T1/T2 with a known deadline expiring within warn_days days.
    if tier in (1, 2):
        deadline = (row.get("deadline") or "").strip()
        if deadline:
            try:
                days_left = (dt.date.fromisoformat(deadline) - dt.date.today()).days
                if 0 <= days_left <= warn_days:
                    base += URGENCY_BOOST
            except ValueError:
                pass
    return base


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tracker", default="tracker.csv")
    ap.add_argument("--sources", default="sources.yaml")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--urls", action="store_true",
                    help="Print only the ranked URLs (one per line) - pipe into "
                         "`pipeline.py --applied-file` to mark them applied")
    ap.add_argument("--write-scores", action="store_true",
                    help="Score ALL rows in tracker.csv and write the score back to "
                         "the 'score' column (used by fetch_jobs.py after each run).")
    args = ap.parse_args()

    base = Path(__file__).parent
    path = base / args.tracker
    if not path.exists():
        print(f"{path.name} not found - run `uv run fetch_jobs.py` first.")
        return

    min_inr, remote_floor_inr = load_thresholds(base / args.sources)
    warn_days = load_expiry_warn_days(base / args.sources)

    if args.write_scores:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            all_rows = list(reader)
        if "score" not in fieldnames:
            fieldnames = fieldnames + ["score"]
        for r in all_rows:
            r["score"] = str(total_score(r, min_inr, remote_floor_inr, warn_days))
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(all_rows)
        tmp.replace(path)
        print(f"  Wrote scores for {len(all_rows)} rows.")
        return

    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    if not args.all:
        # Sourced and explicitly shortlisted rows are actionable triage candidates.
        # Applied (and beyond) roles drop out once marked so they don't clutter
        # the queue. not_applicable is intentionally excluded here — those rows
        # are dismissed by the user and pruned automatically on the next fetch.
        rows = [
            r for r in rows
            if r.get("stage") in ("sourced", "new", "shortlisted")
        ]

    def row_tier(r):
        return tier_of(r.get("role", ""), r.get("location", ""),
                       r.get("salary", ""), min_inr, remote_floor_inr)

    def row_total(r):
        return total_score(r, min_inr, remote_floor_inr, warn_days)

    ranked = sorted(rows, key=row_total, reverse=True)

    if args.urls:
        for r in ranked[: args.top]:
            if r.get("url"):
                print(r["url"])
        return

    today = dt.date.today()
    print(f"{'SCORE':>5}  {'TIER':<4}  {'COMPANY':<16}  {'ROLE':<38}  "
          f"{'LOCATION':<24}  SALARY")
    print("-" * 116)
    for r in ranked[: args.top]:
        tier = row_tier(r)
        tier_lbl = f"T{tier}" if tier else "-"
        # Urgency marker: T1/T2 roles closing within warn_days days
        expires_tag = ""
        if tier in (1, 2):
            dl = (r.get("deadline") or "").strip()
            if dl:
                try:
                    days_left = (dt.date.fromisoformat(dl) - today).days
                    if 0 <= days_left <= warn_days:
                        expires_tag = f"  ⚠ EXPIRES: {dl} ({days_left}d)"
                except ValueError:
                    pass
        brand_tag = "  [brand]" if _brand_boost(r.get("company", ""), r.get("salary", "")) > 0 else ""
        print(
            f"{row_total(r):>5}  {tier_lbl:<4}  {(r.get('company','') or '')[:16]:<16}  "
            f"{(r.get('role','') or '')[:38]:<38}  "
            f"{(r.get('location','') or '')[:24]:<24}  "
            f"{(r.get('salary','') or '—')[:18]}{expires_tag}{brand_tag}"
        )
    scope = "all" if args.all else "sourced"
    print(
        f"\nShowing top {min(args.top, len(ranked))} of {len(ranked)} {scope} roles "
        "(ranked for fresh, eligible, direct applications; T1–T4 labels show "
        "compensation/remote desirability only). "
        f"Apply, then `uv run pipeline.py --applied <url>` to drop them from triage."
    )


if __name__ == "__main__":
    main()
