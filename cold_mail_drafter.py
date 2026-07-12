# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
cold_mail_drafter.py — generate ready-to-review cold-email drafts.

For each target role, pairs with the best available contact from
outreach/contacts.csv and writes a tailored cold-email draft to
outreach/cold_mail_drafts.md.  NEVER auto-sends.

Usage:
    uv run cold_mail_drafter.py                     # all stage=applied/sourced + no contact_email
    uv run cold_mail_drafter.py --company "Stripe"  # single company
    uv run cold_mail_drafter.py --top 10            # top 10 scored roles
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).parent
REPO_ROOT   = SCRIPT_DIR.parent
TRACKER_CSV = SCRIPT_DIR / "tracker.csv"
CONTACTS_CSV = REPO_ROOT / "outreach" / "contacts.csv"
DRAFTS_MD   = REPO_ROOT / "outreach" / "cold_mail_drafts.md"

# ---------------------------------------------------------------------------
# Candidate profile constants
# ---------------------------------------------------------------------------
CANDIDATE_NAME  = "Bhavya Vashisht"
PORTFOLIO_LINK  = "https://bhavyaportfolio.site"
GITHUB_LINK     = "https://github.com/BhavyaV29"

RESUME_VARIANT_NAMES = ("ai_platform", "backend", "master")


def _resume_attach(variant: str) -> str:
    """Resumes are NOT hosted on the portfolio site — emit a clearly-labelled
    placeholder so the real PDF (resume/out/resume_<variant>.pdf) is attached by
    hand, instead of pasting a dead link into a cold email."""
    v = variant if variant in RESUME_VARIANT_NAMES else "master"
    return f"[ATTACH RESUME: resume_{v}.pdf]"

# role-type → proof-point copy + flagship project hint
PROOF_POINTS = {
    "ai_platform": (
        "scheduling and deployment workflows for AI-agent workloads on "
        "Kubernetes, plus MCP integration in an agent runtime"
    ),
    "backend": (
        "Python/FastAPI and Helm workflows that manage agent services on "
        "Kubernetes, plus Redis-backed scheduling with idempotency across workers"
    ),
    "infra": (
        "Kubernetes lifecycle automation for agent and multi-agent services, "
        "with isolated routing and reliable retry behavior"
    ),
    "storage": (
        "MongoDB conversation-list API optimized via metadata-only projection "
        "and compound indexing — payload ~99.5% smaller, sub-500ms latency"
    ),
}

FLAGSHIP_PROJECT = "JobOps Pipeline"
FLAGSHIP_LINK    = "https://github.com/BhavyaV29/job-hunter-pipeline"


# ---------------------------------------------------------------------------
# Scoring (inline from score.py logic)
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
        return int(row.get("score", "") or "")
    except (ValueError, TypeError):
        pass
    role = (row.get("role") or "").lower()
    loc  = (row.get("location") or "").lower()
    s = sum(w for k, w in _KEYWORDS.items() if k in role)
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
# Resume variant logic
# ---------------------------------------------------------------------------

def _resume_variant(role: str) -> str:
    r = role.lower()
    if any(kw in r for kw in ("ml", "ai", "agent", "llm", "applied",
                               "machine learning", "mle", "gen ai", "genai")):
        return "ai_platform"
    if any(kw in r for kw in ("backend", "back end", "infra", "platform",
                               "distributed", "sre", "reliability")):
        return "backend"
    return "master"


def _proof_point_for_variant(variant: str, role: str) -> str:
    r = role.lower()
    if variant == "ai_platform":
        return PROOF_POINTS["ai_platform"]
    if any(kw in r for kw in ("kubernetes", "k8s", "infra", "scheduler",
                               "sre", "reliability")):
        return PROOF_POINTS["infra"]
    if any(kw in r for kw in ("mongodb", "postgres", "database", "storage", "data")):
        return PROOF_POINTS["storage"]
    return PROOF_POINTS["backend"]


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _stage_col(fieldnames: list[str]) -> str | None:
    for col in ("stage", "status"):
        if col in fieldnames:
            return col
    return None


def _contact_email_col(fieldnames: list[str]) -> str | None:
    for col in ("contact_email", "referral_contact"):
        if col in fieldnames:
            return col
    return None


def _job_id(row: dict) -> str:
    """Extract the shortest clean job reference from a tracker row."""
    if row.get("job_id"):
        return row["job_id"].strip()
    url = row.get("url") or ""
    parsed = urlparse(url)
    # 1. Common ATS query-param IDs
    qs = parse_qs(parsed.query)
    for param in ("gh_jid", "jl", "jobId", "job_id", "id"):
        if param in qs:
            return qs[param][0]
    # 2. Last path segment — prefer trailing numeric ID within a slug
    segs = [s for s in parsed.path.rstrip("/").split("/") if s]
    if segs:
        seg = segs[-1].split(".")[0]        # strip .htm etc.
        m = re.search(r"(\d{6,})$", seg)   # trailing long number
        if m:
            return m.group(1)
        return seg[:40]
    return "N/A"


def _load_tracker(stage_filter: set[str] | None, contact_email_blank: bool) -> list[dict]:
    rows: list[dict] = []
    with open(TRACKER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        stage_col  = _stage_col(fieldnames)
        email_col  = _contact_email_col(fieldnames)
        for row in reader:
            if stage_filter and stage_col:
                val = (row.get(stage_col) or "").lower()
                if val not in stage_filter:
                    continue
            if contact_email_blank and email_col:
                if (row.get(email_col) or "").strip():
                    continue   # already has a contact email
            rows.append(row)
    return rows


def _load_contacts() -> list[dict]:
    if not CONTACTS_CSV.exists():
        return []
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Contact matching
# ---------------------------------------------------------------------------

LEADERSHIP_TITLES = {
    "cto", "chief technology", "vp eng", "vp of engineering",
    "head of engineering", "head of eng", "engineering manager",
    "director of engineering", "tech lead", "technical lead",
    "principal engineer", "staff engineer",
}


def _is_leadership_contact(position: str) -> bool:
    pos = (position or "").lower()
    return any(kw in pos for kw in LEADERSHIP_TITLES)


def _best_contact(company: str, contacts: list[dict]) -> dict | None:
    """
    Pick best contact for a company: prefer verified+leadership, then
    verified, then any, then pattern.
    """
    company_contacts = [
        c for c in contacts
        if (c.get("company") or "").lower() == company.lower()
    ]
    if not company_contacts:
        return None

    def _priority(c: dict) -> tuple[int, int, int]:
        verified  = (c.get("verified") or "").lower() == "true"
        is_lead   = _is_leadership_contact(c.get("position") or "")
        is_hunter = (c.get("source") or "") == "hunter"
        conf      = int(c.get("confidence") or 0)
        return (
            int(verified and is_lead),   # 0 or 1 — best
            int(verified),               # verified
            conf,                        # raw confidence
        )

    return max(company_contacts, key=_priority)


# ---------------------------------------------------------------------------
# Email drafting
# ---------------------------------------------------------------------------

def _subject(role: str, company: str, job_ref: str) -> str:
    return f"Backend/AI Engineer – {role} at {company} (Ref: {job_ref})"


def _body(
    contact_name: str,
    company: str,
    role: str,
    variant: str,
    proof: str,
    resume_url: str,
) -> str:
    """
    Compose a ~120-word cold email body following the hiring-manager template
    from outreach/coldemail_templates.md.
    """
    first = contact_name.split()[0] if contact_name and contact_name != "{First}" else "{Name}"

    # Tailor the company-specific hook per role type
    if variant == "ai_platform":
        team_hook = (
            f"{company}'s applied-AI and agent infrastructure is tackling "
            f"exactly the kind of problems I've been building for"
        )
        proof_label = "MCP multi-agent orchestration system"
    elif "infra" in role.lower() or "reliability" in role.lower() or "sre" in role.lower():
        team_hook = (
            f"{company}'s infrastructure and reliability work is the kind of "
            f"deep systems engineering I want to do full-time"
        )
        proof_label = "Kubernetes scheduler extension"
    else:
        team_hook = (
            f"{company}'s backend platform is solving real scale challenges, "
            f"and it maps closely to what I've been building"
        )
        proof_label = "distributed backend system"

    body = (
        f"Hi {first},\n\n"
        f"{team_hook}: {proof}.\n\n"
        f"I'm a backend/applied-AI engineer (2026 grad) targeting {role}-type roles. "
        f"Two quick proof points:\n"
        f"- **{proof_label}**: {FLAGSHIP_PROJECT} — my work: {FLAGSHIP_LINK}\n"
        f"- Reduced MongoDB query latency 4× through pipeline + index "
        f"redesign in a high-traffic read path.\n\n"
        f"Would a short chat about the team be worth 20 minutes? "
        f"Resume: {resume_url}  |  Portfolio: {PORTFOLIO_LINK}\n\n"
        f"Thanks for your time,\n"
        f"{CANDIDATE_NAME}"
    )
    return body


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------

def _draft_section(
    company: str,
    role: str,
    to_email: str,
    subject: str,
    body: str,
    variant: str,
    today: str,
) -> str:
    return (
        f"## {company} — {role}\n"
        f"**To:** {to_email}  \n"
        f"**Subject:** {subject}  \n"
        f"**Date drafted:** {today}\n\n"
        f"> {body.replace(chr(10), chr(10) + '> ')}\n\n"
        f"---\n"
        f"- [ ] Personalised {{placeholders}}  "
        f"- [ ] Resume attached (variant: **{variant}**)  "
        f"- [ ] Sent on: ___\n\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--company", help="Draft only for this company (case-insensitive)")
    p.add_argument("--top", type=int,
                   help="Draft for the top N scored roles only")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    today = date.today().isoformat()

    # Load data
    stage_filter = {"sourced", "applied"}
    tracker_rows = _load_tracker(stage_filter=stage_filter, contact_email_blank=True)
    if not tracker_rows:
        print("No rows matching stage=sourced/applied (no contact_email set). "
              "Falling back to all tracker rows.")
        tracker_rows = _load_tracker(stage_filter=None, contact_email_blank=False)

    contacts = _load_contacts()
    if not contacts:
        print("Note: outreach/contacts.csv not found or empty. "
              "Run find_contacts.py first for better contact matching.\n")

    # Company filter
    if args.company:
        needle = args.company.lower()
        tracker_rows = [
            r for r in tracker_rows
            if (r.get("company") or "").lower() == needle
        ]
        if not tracker_rows:
            print(f"No tracker rows found for company '{args.company}'.")
            sys.exit(1)

    # Top-N filter (by score)
    if args.top:
        tracker_rows = sorted(tracker_rows, key=_score, reverse=True)[: args.top]

    # Build drafts
    sections: list[str] = []
    no_contact_warnings: list[str] = []
    drafts_written = 0

    for row in tracker_rows:
        company  = (row.get("company") or "").strip()
        role     = (row.get("role")    or "").strip()
        url      = (row.get("url")     or "").strip()
        job_ref  = _job_id(row)

        variant  = _resume_variant(role)
        proof    = _proof_point_for_variant(variant, role)
        resume_url = _resume_attach(variant)

        # Find best contact
        contact = _best_contact(company, contacts)

        # Determine To: and contact name
        if contact:
            fn = (contact.get("first_name") or "").strip()
            ln = (contact.get("last_name")  or "").strip()
            contact_name  = f"{fn} {ln}".strip() or "{First} {Last}"
            to_email      = (contact.get("email") or "{contact_email}").strip()
        else:
            contact_name  = "{Name}"
            to_email      = "{contact_email}"
            no_contact_warnings.append(f"  ⚠  {company} — {role}")

        subject = _subject(role, company, job_ref)
        body    = _body(contact_name, company, role, variant, proof, resume_url)
        section = _draft_section(company, role, to_email,
                                  subject, body, variant, today)
        sections.append(section)
        drafts_written += 1

    # Write output
    DRAFTS_MD.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Cold Mail Drafts\n\n"
        f"_Generated {today} by cold_mail_drafter.py — "
        f"**review every draft before sending**._\n\n"
        f"---\n\n"
    )
    with open(DRAFTS_MD, "w", encoding="utf-8") as f:
        f.write(header)
        f.writelines(sections)

    print(f"Wrote {drafts_written} draft(s) → {DRAFTS_MD}")

    if no_contact_warnings:
        print(f"\nNo contact found for {len(no_contact_warnings)} role(s) "
              f"(placeholder used):")
        for w in no_contact_warnings:
            print(w)

    print(f"\nNext steps:")
    print(f"  1. Open {DRAFTS_MD}")
    print(f"  2. Fill in {{placeholders}}, attach the right resume variant")
    print(f"  3. Tick the checklist boxes before sending")


if __name__ == "__main__":
    main()
