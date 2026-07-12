# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests",
# ]
# ///
"""
find_contacts.py — discover engineering contacts for target companies.

For each company in tracker.csv, queries Hunter.io (if key set) and generates
standard email patterns, writing results to outreach/contacts.csv.

Usage:
    uv run find_contacts.py                       # all stage=sourced/applied rows
    uv run find_contacts.py --company "Stripe"    # single company
    uv run find_contacts.py --limit 10            # top N companies (by tracker order)
    uv run find_contacts.py --force               # ignore 25-domain Hunter limit
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests

from staffing_filter import is_staffing_listing

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
TRACKER_CSV = SCRIPT_DIR / "tracker.csv"
CONTACTS_CSV = REPO_ROOT / "outreach" / "contacts.csv"

CONTACTS_COLUMNS = [
    "company", "domain", "first_name", "last_name",
    "email", "position", "confidence", "source", "verified", "date_found",
]

# Engineering / eng-leadership signals for Hunter.io position filtering.
ENG_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "sde", "backend",
    "frontend", "platform", "infrastructure", "devops", "site reliability",
    "sre", "ml engineer", "machine learning", "technical lead", "tech lead",
    "staff engineer", "principal engineer", "engineering manager",
    "engineering director", "head of engineering", "vp of engineering",
    "vp engineering", "cto", "chief technology",
}

NON_ENG_KEYWORDS = {
    "sales", "account executive", "business development", "marketing",
    "talent acquisition", "recruiter", "recruiting", "human resources",
    " hr ", "people operations", "fraud", "customer success", "support",
    "legal", "finance", "operations manager", "product manager",
    "product marketing", "partnerships",
}

EMAIL_PATTERN_TEMPLATES = [
    "{First}.{Last}",
    "{F}{Last}",
    "{First}",
    "{F}.{Last}",
]

HUNTER_API_BASE = "https://api.hunter.io/v2"
HUNTER_FREE_LIMIT = 25

JOB_BOARD_DOMAINS = {
    "linkedin.com", "in.linkedin.com", "glassdoor.co.in", "glassdoor.com",
    "indeed.com", "remoteok.com", "ashbyhq.com", "greenhouse.io",
    "job-boards.greenhouse.io", "boards.greenhouse.io",
    "builtin.com", "jobright.ai", "jobgether.com", "monster.com",
    "dailyremote.com", "bebee.com", "himalayas.app", "internshala.com",
    "apna.co", "cosmoquick.com", "remotejobs.org", "theelitejob.com",
    "rockerstop.com", "naukri.com", "cutshort.io", "hirist.com",
    "wellfound.com", "angel.co", "jobs.lever.co", "lever.co",
    "hackajob.co", "hackajob.com",
}

KNOWN_DOMAINS: dict[str, str] = {
    "stripe": "stripe.com",
    "databricks": "databricks.com",
    "coinbase": "coinbase.com",
    "dropbox": "dropbox.com",
    "airbnb": "airbnb.com",
    "notion": "notion.so",
    "amazon": "amazon.com",
    "oracle": "oracle.com",
    "google": "google.com",
    "microsoft": "microsoft.com",
    "netflix": "netflix.com",
    "meta": "meta.com",
    "apple": "apple.com",
    "visa": "visa.com",
    "docusign": "docusign.com",
    "infosys": "infosys.com",
    "accenture": "accenture.com",
    "wipro": "wipro.com",
    "tcs": "tcs.com",
    "ramp": "ramp.com",
    "anaplan": "anaplan.com",
    "morningstar": "morningstar.com",
    "rakuten": "rakuten.com",
    "siemens": "siemens.com",
    "pwc": "pwc.com",
    "ust": "ust.com",
    "sabre": "sabre.com",
    "harman": "harman.com",
    "nike": "nike.com",
    "nasdaq": "nasdaq.com",
    "trimble": "trimble.com",
    "phonepe": "phonepe.com",
    "razorpay": "razorpay.com",
    "zerodha": "zerodha.com",
    "digitap": "digitap.ai",
    "quantiphi": "quantiphi.com",
    "phonepe limited": "phonepe.com",
}

# Greenhouse board token → employer domain (job-boards.greenhouse.io/<token>).
GREENHOUSE_BOARD_DOMAINS: dict[str, str] = {
    "phonepe": "phonepe.com",
    "stripe": "stripe.com",
    "dropbox": "dropbox.com",
    "coinbase": "coinbase.com",
    "airbnb": "airbnb.com",
    "databricks": "databricks.com",
    "notion": "notion.so",
    "ramp": "ramp.com",
    "inmobi": "inmobi.com",
}


def _api_key() -> str | None:
    return os.environ.get("HUNTER_API_KEY") or os.environ.get("HUNTERAPI_KEY")


def _domain_from_greenhouse_url(url: str) -> str | None:
    m = re.search(
        r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_-]+)",
        url, re.I,
    )
    if not m:
        return None
    token = m.group(1).lower()
    return GREENHOUSE_BOARD_DOMAINS.get(token, f"{token}.com")


def _extract_domain(url: str, company: str) -> str | None:
  """
  Extract the actual company email domain.

  Returns None when the listing should be skipped (staffing / unresolvable).
  """
  if is_staffing_listing(company, url):
    return None

  m = re.search(r"\(([a-z0-9.-]+\.[a-z]{2,})\)", (company or "").lower())
  if m:
    return m.group(1)

  gh = _domain_from_greenhouse_url(url or "")
  if gh:
    return gh

  c_lower = (company or "").lower()
  for keyword, domain in KNOWN_DOMAINS.items():
    if keyword in c_lower:
      return domain

  try:
    netloc = urlparse(url).netloc.lower().lstrip("www.")
    if netloc:
      if netloc in JOB_BOARD_DOMAINS:
        pass
      elif netloc.endswith(".greenhouse.io"):
        pass
      elif not any(netloc.endswith(d) for d in JOB_BOARD_DOMAINS):
        parts = netloc.split(".")
        if parts[0] == "www":
          parts = parts[1:]
        return ".".join(parts)
  except Exception:
    pass

  slug = "".join(c for c in c_lower if c.isalnum())
  if not slug or slug in ("tbd", "na"):
    return None
  return f"{slug}.com"


def _is_eng_contact(position: str) -> bool:
  pos = (position or "").lower()
  if any(kw in pos for kw in NON_ENG_KEYWORDS):
    return False
  return any(kw in pos for kw in ENG_KEYWORDS)


def _filter_hunter_results(contacts: list[dict]) -> list[dict]:
  """Prefer engineering ICs and eng leadership; drop sales/TA/fraud."""
  eng = [c for c in contacts if _is_eng_contact(c.get("position", ""))]
  if eng:
    return eng[:5]
  return contacts[:2]


def _score_row(row: dict) -> int:
    try:
        return int(row.get("score", "") or "")
    except (ValueError, TypeError):
        pass
    keywords = {
        "backend": 5, "back end": 5, "distributed": 5, "platform": 4,
        "infrastructure": 4, "go": 4, "golang": 4, "python": 3,
        "kubernetes": 4, "redis": 3, "mongodb": 3, "fastapi": 4,
        "applied ai": 5, "ai engineer": 4, "agent": 4, "llm": 4,
        "machine learning": 3, "mle": 5, "ml": 2, "mcp": 4,
        "new grad": 6, "graduate": 5, "entry": 5, "fresher": 6,
        "associate": 4, "junior": 4, "sde": 4, "sde 1": 5,
    }
    negatives = {
        "senior": -6, "sr.": -6, "staff": -7, "principal": -8,
        "manager": -6, "director": -8, "lead": -5, "head of": -8, "vp": -6,
    }
    role = (row.get("role") or "").lower()
    loc = (row.get("location") or "").lower()
    s = sum(w for k, w in keywords.items() if k in role)
    s += sum(w for k, w in negatives.items() if k in role)
    s += max((w for k, w in {
        "india": 3, "bengaluru": 3, "bangalore": 3, "remote": 2,
        "hyderabad": 3, "pune": 3, "delhi": 3, "noida": 3, "mumbai": 3,
    }.items() if k in loc), default=0)
    return s


def _get_stage_col(fieldnames: list[str]) -> str | None:
    for col in ("stage", "status"):
        if col in fieldnames:
            return col
    return None


def _get_job_id(row: dict) -> str:
    if row.get("job_id"):
        return row["job_id"]
    url = row.get("url", "")
    segments = [s for s in url.rstrip("/").split("/") if s]
    return segments[-1] if segments else ""


def _load_existing_contacts() -> tuple[set[str], list[dict]]:
    if not CONTACTS_CSV.exists():
        return set(), []
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    emails = {(r.get("email") or "").lower() for r in rows}
    return emails, rows


def _cached_domains(existing_rows: list[dict]) -> set[str]:
    return {(r.get("domain") or "").lower() for r in existing_rows if r.get("domain")}


def _write_contacts(rows: list[dict]) -> None:
    CONTACTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(CONTACTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CONTACTS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_tracker(stage_filter: set[str] | None = None) -> list[dict]:
    rows: list[dict] = []
    with open(TRACKER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        stage_col = _get_stage_col(fieldnames)
        for row in reader:
            if stage_filter and stage_col:
                val = (row.get(stage_col) or "").lower()
                if val not in stage_filter:
                    continue
            rows.append(row)
    return rows


def _hunter_domain_search(domain: str, api_key: str) -> list[dict]:
    params = {
        "domain": domain,
        "api_key": api_key,
        "limit": 10,
        "type": "personal",
    }
    try:
        resp = requests.get(
            f"{HUNTER_API_BASE}/domain-search", params=params, timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or {}
            return [
                {
                    "first_name": e.get("first_name") or "",
                    "last_name": e.get("last_name") or "",
                    "email": e.get("value") or "",
                    "position": e.get("position") or "",
                    "confidence": int(e.get("confidence") or 0),
                    "source": "hunter",
                }
                for e in (data.get("emails") or [])
                if e.get("value")
            ]
        elif resp.status_code == 401:
            print("  [!] Hunter.io: Unauthorized — check HUNTER_API_KEY")
        elif resp.status_code == 429:
            print("  [!] Hunter.io: Rate-limit hit — free tier exhausted for this month")
        elif resp.status_code == 403:
            print(f"  [!] Hunter.io: Forbidden (403) for {domain} — may be blocked domain")
        else:
            print(f"  [!] Hunter.io: HTTP {resp.status_code} for {domain}")
    except requests.Timeout:
        print(f"  [!] Hunter.io request timed out for {domain}")
    except requests.RequestException as exc:
        print(f"  [!] Hunter.io request failed for {domain}: {exc}")
    return []


def _pattern_contacts(domain: str) -> list[dict]:
    return [
        {
            "first_name": "{First}",
            "last_name": "{Last}",
            "email": f"{tmpl}@{domain}".replace("{First}", "{First}")
                                        .replace("{Last}", "{Last}")
                                        .replace("{F}", "{F}"),
            "position": "unknown",
            "confidence": 0,
            "source": "pattern",
        }
        for tmpl in EMAIL_PATTERN_TEMPLATES
    ]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--company", help="Process only this company (case-insensitive)")
    p.add_argument("--limit", type=int,
                     help="Process only the top N companies by tracker score")
    p.add_argument("--force", action="store_true",
                     help="Ignore the 25-domain Hunter.io per-run limit")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    api_key = _api_key()
    today = date.today().isoformat()

    if not api_key:
        print("Note: HUNTER_API_KEY not set — Hunter.io calls skipped; "
              "pattern generation will still run.\n")

    stage_filter = {"sourced", "applied"}
    tracker_rows = _load_tracker(stage_filter=stage_filter)
    if not tracker_rows:
        print("No rows with stage=sourced/applied found. "
              "Falling back to all tracker rows.")
        tracker_rows = _load_tracker(stage_filter=None)

    if args.company:
        needle = args.company.lower()
        tracker_rows = [r for r in tracker_rows
                        if (r.get("company") or "").lower() == needle]
        if not tracker_rows:
            print(f"No tracker rows found for company '{args.company}'.")
            sys.exit(1)

    companies: dict[str, dict] = {}
    for row in tracker_rows:
        company = (row.get("company") or "").strip()
        if company and company not in companies:
            companies[company] = row

    if args.limit:
        ranked = sorted(companies.items(),
                        key=lambda kv: _score_row(kv[1]), reverse=True)
        companies = dict(ranked[: args.limit])

    existing_emails, existing_rows = _load_existing_contacts()
    cached_domains = _cached_domains(existing_rows)

    output_rows = list(existing_rows)
    companies_processed = 0
    verified_count = 0
    pattern_count = 0
    hunter_queried = 0
    hunter_run_limit = HUNTER_FREE_LIMIT if not args.force else 9_999
    skipped_staffing = 0

    for company, row in companies.items():
        url = row.get("url") or ""
        domain = _extract_domain(url, company)
        if not domain:
            skipped_staffing += 1
            print(f"\n→ {company}  [skipped — staffing or unresolved domain]")
            continue

        companies_processed += 1
        print(f"\n→ {company}  [{domain}]")

        hunter_contacts: list[dict] = []
        if not api_key:
            pass
        elif domain in cached_domains:
            print("  ↩ Domain already cached — skipping Hunter.io call")
        elif hunter_queried >= hunter_run_limit:
            print(f"  ⚠  Hunter.io run limit ({hunter_run_limit}) reached. "
                  "Use --force to override.")
        else:
            print("  Querying Hunter.io …")
            raw = _hunter_domain_search(domain, api_key)
            hunter_queried += 1
            hunter_contacts = _filter_hunter_results(raw)
            if hunter_contacts:
                print(f"  ✓ {len(hunter_contacts)} engineering contact(s)")
            else:
                print("  – No engineering contacts via Hunter.io")

        for c in hunter_contacts:
            email = (c.get("email") or "").lower()
            if not email or email in existing_emails:
                continue
            conf = c.get("confidence", 0)
            verified = conf >= 70
            if verified:
                verified_count += 1
            output_rows.append({
                "company": company,
                "domain": domain,
                "first_name": c.get("first_name", ""),
                "last_name": c.get("last_name", ""),
                "email": email,
                "position": c.get("position", ""),
                "confidence": conf,
                "source": "hunter",
                "verified": str(verified).lower(),
                "date_found": today,
            })
            existing_emails.add(email)

        patterns = _pattern_contacts(domain)
        for p in patterns:
            email = p["email"].lower()
            if email in existing_emails:
                continue
            output_rows.append({
                "company": company,
                "domain": domain,
                "first_name": p["first_name"],
                "last_name": p["last_name"],
                "email": email,
                "position": p["position"],
                "confidence": 0,
                "source": "pattern",
                "verified": "false",
                "date_found": today,
            })
            existing_emails.add(email)
            pattern_count += 1

        cached_domains.add(domain)

    _write_contacts(output_rows)

    print(f"\n{'─' * 52}")
    print(f"  Companies processed : {companies_processed}")
    print(f"  Skipped (staffing)  : {skipped_staffing}")
    print(f"  Verified emails      : {verified_count}  (Hunter confidence ≥ 70)")
    print(f"  Pattern emails added : {pattern_count}")
    print(f"  Total rows in file   : {len(output_rows)}")
    print(f"  Output               : {CONTACTS_CSV}")


if __name__ == "__main__":
    main()
