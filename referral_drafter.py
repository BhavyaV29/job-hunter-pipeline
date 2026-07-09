# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
referral_drafter.py — generate referral ask drafts (LinkedIn DM + email).

For each company in the top-N scored tracker rows (stage=sourced or empty),
drafts two ready-to-review messages:
  • LinkedIn DM  (<300 chars — connection request note limit)
  • Email        (~5 lines — full referral request)

Output → outreach/referral_drafts.md, one section per company.  NEVER auto-sends.

Usage:
    uv run referral_drafter.py                          # top 20 companies by score
    uv run referral_drafter.py --top 10                 # top N companies
    uv run referral_drafter.py --company "Stripe"       # single company only
    uv run referral_drafter.py --startups               # startups/remote only, top 20
    uv run referral_drafter.py --startups --top 5       # startups/remote only, top 5
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from staffing_filter import is_staffing_listing

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).parent
REPO_ROOT    = SCRIPT_DIR.parent
TRACKER_CSV  = SCRIPT_DIR / "tracker.csv"
CONTACTS_CSV = REPO_ROOT / "outreach" / "contacts.csv"
DRAFTS_MD    = REPO_ROOT / "outreach" / "referral_drafts.md"

# ---------------------------------------------------------------------------
# Candidate constants
# ---------------------------------------------------------------------------
CANDIDATE_NAME  = "Bhavya Vashisht"
# Prefer CANDIDATE_EMAIL env; fall back to personal Gmail (never hardcode college email).
CANDIDATE_EMAIL = os.environ.get(
    "CANDIDATE_EMAIL", "bhavyavashisht119@gmail.com"
).strip() or "bhavyavashisht119@gmail.com"
GITHUB          = "github.com/BhavyaV29"
PORTFOLIO       = "bhavyaportfolio.site"

# Resume variants. Resumes are NOT hosted on the portfolio site, so the draft
# emits a clearly-labelled placeholder (not a dead link) — attach the real PDF
# from resume/out/resume_<variant>.pdf by hand before sending.
RESUME_VARIANT_NAMES = ("backend", "ai_platform", "master")
DEFAULT_VARIANT = "backend"


def _resume_attach(variant: str) -> str:
    v = variant if variant in RESUME_VARIANT_NAMES else DEFAULT_VARIANT
    return f"[ATTACH RESUME: resume_{v}.pdf]"

# Internship highlights embedded in every email draft
_HIGHLIGHT_K8S = (
    "K8s multi-agent deployment: orchestrated a production MCP-based "
    "multi-agent system on Kubernetes with distributed task routing"
)
_HIGHLIGHT_MONGO = (
    "MongoDB optimisation: redesigned aggregation pipeline — "
    "99.5% payload reduction in a high-traffic read path"
)

# ---------------------------------------------------------------------------
# Startup / remote-first detection
# ---------------------------------------------------------------------------

BIG_CORPS: set[str] = {
    "infosys", "wipro", "tcs", "accenture", "capgemini", "cognizant",
    "hcl", "tech mahindra", "persistent", "mphasis", "ibm", "oracle",
    "sap", "cisco", "intel", "qualcomm", "samsung", "deloitte", "kpmg", "pwc",
}

_STARTUP_SOURCES: set[str] = {
    "remoteok", "remotive", "arbeitnow", "linkedin_guest", "serpapi", "themuse",
}

_REMOTE_KEYWORDS: tuple[str, ...] = (
    "remote", "anywhere", "worldwide", "distributed", "work from home",
)


def _is_startup_or_remote(row: dict) -> bool:
    """Return True if the job looks like a startup or remote-first role."""
    if is_staffing_listing(
        row.get("company", ""), row.get("url", ""), row.get("role", "")
    ):
        return False

    source = (row.get("source") or "").strip().lower()
    # Aggregators that skew heavily startup/remote — always include
    if source in _STARTUP_SOURCES:
        return True

    location = (row.get("location") or "").lower()
    if any(kw in location for kw in _REMOTE_KEYWORDS):
        return True

    # Not a big corp AND no salary listed → startup-eligible
    company = (row.get("company") or "").strip().lower()
    salary = (row.get("salary") or "").strip()
    if company not in BIG_CORPS and not salary:
        return True

    return False


# ---------------------------------------------------------------------------
# Scoring  (uses pre-computed 'score' column; falls back to inline calc)
# ---------------------------------------------------------------------------
_KEYWORDS = {
    "backend": 5, "back end": 5, "distributed": 5, "platform": 4,
    "infrastructure": 4, "go": 4, "golang": 4, "python": 3,
    "kubernetes": 4, "redis": 3, "mongodb": 3, "fastapi": 4,
    "applied ai": 5, "ai engineer": 4, "agent": 4, "llm": 4,
    "machine learning": 3, "mle": 5, "ml": 2, "mcp": 4,
    "new grad": 6, "graduate": 5, "entry": 5, "fresher": 6,
    "associate": 4, "junior": 4, "sde": 4, "sde 1": 5,
}
_NEGATIVES = {
    "senior": -6, "sr.": -6, "staff": -7, "principal": -8,
    "manager": -6, "director": -8, "lead": -5, "head of": -8, "vp": -6,
}


def _score(row: dict) -> int:
    try:
        return int(row.get("score") or 0)
    except (ValueError, TypeError):
        pass
    role = (row.get("role") or "").lower()
    loc  = (row.get("location") or "").lower()
    s  = sum(w for k, w in _KEYWORDS.items() if k in role)
    s += sum(w for k, w in _NEGATIVES.items() if k in role)
    s += max(
        (w for k, w in {
            "india": 3, "bengaluru": 3, "bangalore": 3, "remote": 2,
            "hyderabad": 3, "pune": 3, "delhi": 3, "noida": 3, "mumbai": 3,
        }.items() if k in loc),
        default=0,
    )
    return s


# ---------------------------------------------------------------------------
# Job-ID extraction
# ---------------------------------------------------------------------------

def _job_id(row: dict) -> str:
    if row.get("job_id"):
        return row["job_id"].strip()
    url = row.get("url") or ""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for param in ("gh_jid", "jl", "jobId", "job_id", "id"):
        if param in qs:
            return qs[param][0]
    segs = [s for s in parsed.path.rstrip("/").split("/") if s]
    if segs:
        seg = segs[-1].split(".")[0]
        m = re.search(r"(\d{6,})$", seg)
        if m:
            return m.group(1)
        return seg[:40]
    return "N/A"


# ---------------------------------------------------------------------------
# Resume-variant inference
# ---------------------------------------------------------------------------

def _resume_variant(row: dict) -> str:
    v = (row.get("resume_variant") or "").strip().lower()
    if v in RESUME_VARIANT_NAMES:
        return v
    role = (row.get("role") or "").lower()
    if any(kw in role for kw in (
        "ml", "ai", "agent", "llm", "applied", "machine learning",
        "mle", "gen ai", "genai", "mcp",
    )):
        return "ai_platform"
    if any(kw in role for kw in (
        "backend", "back end", "infra", "platform",
        "distributed", "sre", "reliability", "go", "golang",
    )):
        return "backend"
    return DEFAULT_VARIANT


# ---------------------------------------------------------------------------
# Contact lookup
# ---------------------------------------------------------------------------

def _load_contacts() -> list[dict]:
    if not CONTACTS_CSV.exists():
        return []
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _best_contact(company: str, contacts: list[dict]) -> dict | None:
    matches = [
        c for c in contacts
        if (c.get("company") or "").lower() == company.lower()
    ]
    if not matches:
        return None

    def _rank(c: dict) -> tuple[int, int, int]:
        verified  = (c.get("verified") or "").lower() == "true"
        is_hunter = (c.get("source") or "") == "hunter"
        conf      = int(c.get("confidence") or 0)
        return (int(verified), int(is_hunter), conf)

    return max(matches, key=_rank)


# ---------------------------------------------------------------------------
# LinkedIn URLs  (no scraping — clickable search URLs only)
# ---------------------------------------------------------------------------

def _linkedin_company_people_url(company: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    return f"https://www.linkedin.com/company/{slug}/people/"


def _linkedin_search_url(company: str, role_hint: str = "") -> str:
    q = f"{company} {role_hint}".strip()
    return (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={quote_plus(q)}"
    )


# ---------------------------------------------------------------------------
# Draft builders
# ---------------------------------------------------------------------------

_DM_TEMPLATE = (
    "Hi {first}, I came across the {role} opening at {company} (ID: {jid}). "
    "I'm a backend/ML engineer with production K8s + MCP work. "
    "Would love a referral if you think it's a fit — "
    "happy to share my resume. Thanks!"
)


def _linkedin_dm(first: str, role: str, company: str, jid: str) -> str:
    dm = _DM_TEMPLATE.format(first=first, role=role, company=company, jid=jid)
    if len(dm) <= 300:
        return dm
    # Truncate role to fit within 300 chars
    overflow = len(dm) - 300
    trole = role[: max(8, len(role) - overflow - 4)] + "..."
    dm = _DM_TEMPLATE.format(first=first, role=trole, company=company, jid=jid)
    return dm[:300]


def _email_body(
    contact_name: str,
    company: str,
    role: str,
    jid: str,
    variant: str,
) -> str:
    first = (
        contact_name.split()[0]
        if contact_name and not contact_name.startswith("{")
        else "{Name}"
    )
    resume_attach = _resume_attach(variant)
    return (
        f"Hi {first},\n\n"
        f"I'm Bhavya Vashisht, a backend/ML engineer from Thapar Institute "
        f"(class of 2026), reaching out about the **{role}** role at "
        f"{company} (Job ID: {jid}).\n\n"
        f"Two internship highlights directly relevant to this role:\n"
        f"- {_HIGHLIGHT_K8S}\n"
        f"- {_HIGHLIGHT_MONGO}\n\n"
        f"Would you be willing to refer me, or forward my application to the "
        f"hiring team? Happy to send a short blurb for the referral form — "
        f"zero effort on your end.\n\n"
        f"Resume ({variant}): {resume_attach}\n"
        f"Portfolio: {PORTFOLIO}  |  GitHub: {GITHUB}\n\n"
        f"Thanks so much,\n"
        f"{CANDIDATE_NAME}\n"
        f"{CANDIDATE_EMAIL}"
    )


# ---------------------------------------------------------------------------
# Markdown section
# ---------------------------------------------------------------------------

def _md_section(
    company: str,
    role: str,
    jid: str,
    contact_name: str,
    contact_position: str,
    li_people_url: str,
    li_search_url: str,
    dm: str,
    email: str,
    variant: str,
    other_roles: list[str],
    today: str,
) -> str:
    contact_line = contact_name
    if contact_position and not contact_position.startswith("unknown"):
        contact_line += f" — _{contact_position}_"

    other_note = ""
    if other_roles:
        roles_str = ", ".join(f"_{r}_" for r in other_roles[:3])
        suffix = " _(+ more)_" if len(other_roles) > 3 else ""
        other_note = f"\n> Also hiring: {roles_str}{suffix}\n"

    dm_quoted = dm.replace("\n", "\n> ")
    email_quoted = email.replace("\n", "\n> ")

    return (
        f"## {company}\n\n"
        f"**Top role:** {role}  \n"
        f"**Job ID:** `{jid}`  \n"
        f"**Contact:** {contact_line}  \n"
        f"**LinkedIn:** [company/people]({li_people_url})  "
        f"| [people search]({li_search_url})  \n"
        f"**Resume variant:** `{variant}`  \n"
        f"**Date drafted:** {today}\n"
        f"{other_note}\n"
        f"### LinkedIn DM\n\n"
        f"> {dm_quoted}\n\n"
        f"_{len(dm)}/300 chars_\n\n"
        f"### Email\n\n"
        f"> {email_quoted}\n\n"
        f"---\n\n"
        f"- [ ] Contact found on LinkedIn  "
        f"- [ ] Resume attached (variant: **{variant}**)  "
        f"- [ ] Sent on: ___  "
        f"- [ ] Logged in outreach_log.csv\n\n"
    )


# ---------------------------------------------------------------------------
# Tracker loading
# ---------------------------------------------------------------------------

def _load_tracker(stage_filter: set[str] | None) -> list[dict]:
    rows: list[dict] = []
    with open(TRACKER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        stage_col = next(
            (c for c in ("stage", "status") if c in fieldnames), None
        )
        for row in reader:
            if stage_filter is not None and stage_col:
                val = (row.get(stage_col) or "").strip().lower()
                if val not in stage_filter:
                    continue
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--top", type=int, default=20,
        help=(
            "Number of top companies to draft for (default: 20). "
            "Use --startups to filter to startup/remote roles first."
        ),
    )
    p.add_argument(
        "--company",
        help="Generate for this company only (case-insensitive substring match)",
    )
    p.add_argument(
        "--startups", action="store_true", default=False,
        help=(
            "Only include jobs from companies that look like startups or "
            "remote-first companies (filters by source, location keywords, "
            "and absence of big-corp signals)."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    today = date.today().isoformat()

    # Load tracker — stage=sourced or empty
    stage_filter: set[str] | None = {"sourced", ""}
    tracker_rows = _load_tracker(stage_filter)
    if not tracker_rows:
        print("No rows with stage=sourced or empty. Falling back to all rows.")
        tracker_rows = _load_tracker(None)

    # Company filter
    if args.company:
        needle = args.company.lower()
        tracker_rows = [
            r for r in tracker_rows
            if needle in (r.get("company") or "").lower()
        ]
        if not tracker_rows:
            print(f"No tracker rows found for company '{args.company}'.")
            sys.exit(1)

    # Sort by score descending, then optionally filter to startups/remote
    tracker_rows.sort(key=_score, reverse=True)

    if args.startups:
        before = len(tracker_rows)
        tracker_rows = [r for r in tracker_rows if _is_startup_or_remote(r)]
        print(f"Startup/remote filter: {len(tracker_rows)}/{before} rows pass.")

    # Deduplicate by company
    company_to_rows: dict[str, list[dict]] = {}
    ordered: list[str] = []
    for row in tracker_rows:
        company = (row.get("company") or "").strip()
        if not company:
            continue
        if company not in company_to_rows:
            ordered.append(company)
        company_to_rows.setdefault(company, []).append(row)

    ordered = ordered[: args.top]

    # Load contacts
    contacts = _load_contacts()
    if not contacts:
        print(
            "Note: outreach/contacts.csv not found or empty. "
            "Run find_contacts.py first for better contact matching.\n"
        )

    # Build sections
    sections: list[str] = []
    for company in ordered:
        rows_for_co = company_to_rows[company]
        best_row    = rows_for_co[0]           # highest-scored row
        other_roles = [
            (r.get("role") or "").strip()
            for r in rows_for_co[1:]
            if (r.get("role") or "").strip()
        ]

        role    = (best_row.get("role") or "").strip()
        jid     = _job_id(best_row)
        variant = _resume_variant(best_row)

        # Contact: prefer referral_contact from tracker, then contacts.csv
        contact_name     = (best_row.get("referral_contact") or "").strip()
        contact_position = ""
        if not contact_name:
            c = _best_contact(company, contacts)
            if c:
                fn = (c.get("first_name") or "").strip()
                ln = (c.get("last_name")  or "").strip()
                contact_name     = f"{fn} {ln}".strip() or "{Name}"
                contact_position = (c.get("position") or "").strip()
        if not contact_name:
            contact_name = "{Name}"

        contact_first = contact_name.split()[0]
        if contact_first.startswith("{"):
            contact_first = "{Name}"

        role_hint   = role.split(",")[0].strip() if role else ""
        li_people   = _linkedin_company_people_url(company)
        li_search   = _linkedin_search_url(company, role_hint)
        dm          = _linkedin_dm(contact_first, role, company, jid)
        email       = _email_body(contact_name, company, role, jid, variant)

        sections.append(_md_section(
            company, role, jid,
            contact_name, contact_position,
            li_people, li_search,
            dm, email,
            variant, other_roles, today,
        ))
        print(f"  ✓ {company:<30s}  score={_score(best_row):4d}  [{len(dm)} chars DM]")

    DRAFTS_MD.parent.mkdir(parents=True, exist_ok=True)
    filter_note = " · startup/remote filter active" if args.startups else ""
    header = (
        f"# Referral Drafts\n\n"
        f"_Generated {today} · top {len(ordered)} companies by score{filter_note}. "
        f"**Review, personalise, and find the contact before sending.**_\n\n"
        f"---\n\n"
    )
    with open(DRAFTS_MD, "w", encoding="utf-8") as f:
        f.write(header)
        f.writelines(sections)

    print(f"\nWrote {len(ordered)} draft(s) → {DRAFTS_MD}")
    print("\nNext steps:")
    print(f"  1. Open {DRAFTS_MD}")
    print("  2. Find each contact on LinkedIn (links provided above each draft)")
    print("  3. Personalise — fill {Name}, add 1 sentence of specific context")
    print("  4. Send manually — NEVER auto-send")
    print("  5. Log it:  uv run outreach_log.py --add company='X' person='Y' ...")


if __name__ == "__main__":
    main()
