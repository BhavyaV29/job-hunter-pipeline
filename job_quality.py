"""Validate job listings: reject category pages, spam aggregators, junk titles."""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from staffing_filter import is_staffing_listing

# Third-party repost/spam boards — not primary employer career pages.
SPAM_DOMAINS = frozenset({
    "bebee.com", "jobrapido.com", "theelitejob.com", "digitalxnode.com",
    "html-5.me", "is-great.net", "taskbyte.is-great.net", "hiremesh.html-5.me",
    "nextleap.app", "in.jobrapido.com", "us.jobrapido.com",
    "jobs.google.com", "google.com",
    "hackajob.co", "hackajob.com", "liveblog365.com", "7f.liveblog365.com",
    "careersprint.com",
})

SOURCE_NAMES = frozenset({
    "naukri", "wellfound", "cutshort", "hirist", "serpapi", "jsearch",
    "adzuna", "linkedin_guest", "linkedin", "greenhouse", "themuse",
})

# Category / search-listing URL patterns (not individual postings).
_CATEGORY_URL_RES = [
    re.compile(r"naukri\.com/(?:fresher-)?[a-z0-9-]+-jobs(?:-in-|$)", re.I),
    re.compile(r"naukri\.com/[a-z0-9-]+-jobs-in-", re.I),
    re.compile(r"wellfound\.com/role/", re.I),
    re.compile(r"cutshort\.io/jobs/", re.I),
    re.compile(r"hirist\.com/k/", re.I),
    re.compile(r"hirist\.com/jobfeed", re.I),
    re.compile(r"/jobs\?"),  # search pages
    re.compile(r"/search\?"),  # stripe-style search (keep gh_jid in query — handled below)
]

# Titles that are search-result pages, not a single role.
_JUNK_TITLE_RES = [
    re.compile(r"^\d{3,}\s+\w", re.I),                          # "54489 Software Developer..."
    re.compile(r"\d+\+\s+.*\bJobs\b", re.I),                    # "50+ Backend Developer Jobs"
    re.compile(r"\bJob Vacancies\b", re.I),
    re.compile(r"\bJobs in \d{4}\b", re.I),
    re.compile(r"\bJobs in .+, India - \d{4}\b", re.I),
    re.compile(r"^Remote .+ Jobs in \d{4}$", re.I),
    re.compile(r"^Fresher .+ Jobs(?: In|$)", re.I),
    re.compile(r"^.+\sJobs$", re.I),  # "Backend Development Jobs" category titles
]

# Individual job URL requirements per source (regex on full URL).
_INDIVIDUAL_URL_RES: dict[str, re.Pattern] = {
    "naukri": re.compile(
        r"naukri\.com/(?:job-listings-|job-details/|jobapi/v3/job/)", re.I
    ),
    "wellfound": re.compile(r"wellfound\.com/jobs/\d+-", re.I),
    "cutshort": re.compile(r"cutshort\.io/job/[^/?#]+", re.I),
    "hirist": re.compile(r"hirist\.com/j/[a-z0-9-]+", re.I),
}


def is_spam_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    if host.endswith(".railway.app") or host.endswith(".up.railway.app"):
        return True
    return any(host == d or host.endswith("." + d) for d in SPAM_DOMAINS)


def is_invalid_company(company: str, url: str = "") -> bool:
    """Company field is a hostname, placeholder, or job-board artifact."""
    co = (company or "").strip()
    if not co or co.upper() == "TBD":
        return True
    low = co.lower()
    if low in SOURCE_NAMES:
        return True
    if ".railway.app" in low or ".up.railway.app" in low:
        return True
    if "liveblog365" in low or "hackajob" in low:
        return True
    # Company equals URL host (scraped board hostname as employer name).
    try:
        host = (urlparse(url).netloc or "").lower().removeprefix("www.")
        if host and (low == host or low.replace(" ", "") == host.replace(".", "")):
            return True
    except Exception:
        pass
    return False


def is_category_url(url: str) -> bool:
    if not url:
        return True
    # Greenhouse/Ashby individual jobs embedded in search URLs.
    if "gh_jid=" in url or re.search(r"gh_jid=\d+", url):
        return False
    if re.search(r"/jobs/\d+-", url):
        return False
    for pat in _CATEGORY_URL_RES:
        if pat.search(url):
            return True
    path = urlparse(url).path.lower()
    if path in ("", "/", "/jobs", "/jobs/"):
        return True
    return False


def is_junk_title(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < 4:
        return True
    return any(p.search(t) for p in _JUNK_TITLE_RES)


def requires_individual_url(source: str) -> bool:
    return source in _INDIVIDUAL_URL_RES


def is_individual_job_url(url: str, source: str = "") -> bool:
    """True when URL looks like a single job posting, not a category/search page."""
    if not url or is_spam_url(url) or is_category_url(url):
        return False
    src = (source or "").lower()
    pat = _INDIVIDUAL_URL_RES.get(src)
    if pat:
        return bool(pat.search(url))
    # Generic: reject bare domain roots and /jobs category slugs
    path = urlparse(url).path.lower()
    if path in ("", "/", "/jobs", "/jobs/"):
        return False
    return True


# Indian city/region tokens in Cutshort URL slugs (between role and company).
_CUTSHORT_CITY_TOKENS = frozenset({
    "bengaluru", "bangalore", "delhi", "ncr", "noida", "pune", "mumbai", "navi",
    "hyderabad", "chennai", "gurugram", "gurgaon", "ghaziabad", "faridabad",
    "trivandrum", "thiruvananthapuram", "ahmedabad", "coimbatore", "kochi",
    "cochin", "kolkata", "calcutta", "india", "remote", "chandigarh", "jaipur",
    "lucknow", "indore", "bhopal", "nagpur", "surat", "vadodara", "visakhapatnam",
    "vizag", "mysore", "mysuru", "kerala", "karnataka", "maharashtra", "telangana",
    "tamilnadu", "tamil", "nadu", "haryana", "uttar", "pradesh", "west", "bengal",
})

_CUTSHORT_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9]{6,12}$")

# Trailing company-name tokens (multi-word employers without a city segment).
_CUTSHORT_COMPANY_SUFFIX_TOKENS = frozenset({
    "private", "limited", "ltd", "inc", "llc", "technologies", "technology",
    "tech", "solutions", "hub", "labs", "consulting", "services", "group",
})


def _is_cutshort_city_token(token: str) -> bool:
    return token.lower() in _CUTSHORT_CITY_TOKENS


def parse_cutshort_url_slug(url: str) -> tuple[str, str]:
    """Parse company + role from cutshort.io/job/slug URLs.

    Slug shape: {role}-{cities}-{company}-{jobId}
    Job id is a trailing 6-12 char alphanumeric token. City tokens are stripped
    from the middle; company is the trailing non-city block before cities.
    """
    m = re.search(r"cutshort\.io/job/([^/?#]+)", url, re.I)
    if not m:
        return "", ""
    parts = m.group(1).split("-")
    if parts and _CUTSHORT_JOB_ID_RE.fullmatch(parts[-1]):
        parts.pop()
    if not parts:
        return "", ""

    has_cities = any(_is_cutshort_city_token(p) for p in parts)

    if has_cities:
        company_parts: list[str] = []
        i = len(parts) - 1
        while i >= 0 and not _is_cutshort_city_token(parts[i]):
            company_parts.insert(0, parts[i])
            i -= 1
        if not company_parts:
            return "", " ".join(parts).replace("-", " ").title()
        while i >= 0 and _is_cutshort_city_token(parts[i]):
            i -= 1
        role_parts = parts[: i + 1]
    else:
        company_parts = [parts[-1]]
        i = len(parts) - 2
        while i >= 0 and parts[i].lower() in _CUTSHORT_COMPANY_SUFFIX_TOKENS:
            company_parts.insert(0, parts[i])
            i -= 1
        role_parts = parts[: i + 1] if i >= 0 else []

    role = " ".join(role_parts).replace("-", " ").title()
    company = " ".join(company_parts).replace("-", " ").title()
    return company, role or company


def parse_hirist_url_slug(url: str) -> tuple[str, str]:
    """Parse company + role from hirist.com/j/slug-id.html URLs."""
    m = re.search(r"hirist\.com/j/([^/?#]+)", url, re.I)
    if not m:
        return "", ""
    slug = m.group(1).replace(".html", "")
    parts = slug.split("-")
    # trailing numeric job id
    while parts and parts[-1].isdigit():
        parts.pop()
    if not parts:
        return "", ""
    eng = ("engineer", "developer", "dev", "analyst", "manager", "intern",
           "associate", "sde", "backend", "frontend", "lead", "senior", "java",
           "python", "node", "ml", "ai", "architect", "consultant")
    # find first part that looks like role keyword
    split_at = len(parts)
    for i, p in enumerate(parts):
        if p.lower() in eng or any(p.lower().startswith(x) for x in ("backend", "frontend")):
            split_at = i
            break
    if split_at == 0:
        return "", " ".join(parts).replace("-", " ").title()
    company = " ".join(parts[:split_at]).replace("-", " ").title()
    role = " ".join(parts[split_at:]).replace("-", " ").title()
    return company, role or " ".join(parts).replace("-", " ").title()


def parse_company_role(title: str, source: str = "", url: str = "") -> tuple[str, str]:
    """Extract (company, role) from SerpApi / board title strings."""
    t = (title or "").strip()
    if not t:
        return "", ""

    # "Role at Company | Wellfound"
    if " at " in t.lower():
        parts = re.split(r"\s+at\s+", t, maxsplit=1, flags=re.IGNORECASE)
        role = parts[0].strip()
        company = re.split(r"\s*[\|–-]\s*", parts[1])[0].strip()
        return company, role

    # "Company - Role" or "Company – Role" (Hirist SerpApi style)
    if " - " in t or " – " in t:
        parts = re.split(r"\s+[-–]\s+", t, maxsplit=1)
        left, right = parts[0].strip(), parts[1].strip()
        # Heuristic: if left looks like a company (short, no "Engineer"/"Developer")
        eng_words = ("engineer", "developer", "analyst", "manager", "intern",
                     "associate", "sde", "backend", "frontend", "ml", "ai")
        if not any(w in left.lower() for w in eng_words):
            return left, right
        return "", t

    # "Company • Location" prefix on Wellfound cards (role in snippet elsewhere)
    if " • " in t:
        company = t.split(" • ")[0].strip()
        return company, t

    return "", re.sub(
        r"\s*[\|–-]\s*(Wellfound|Cutshort|Hirist|Naukri).*$", "", t, flags=re.I
    )


def normalize_job_fields(
    company: str,
    title: str,
    url: str,
    source: str,
) -> tuple[str, str]:
    """Return cleaned (company, title); parse from title when company is missing."""
    co = (company or "").strip()
    role = (title or "").strip()

    if co.lower() in SOURCE_NAMES or not co:
        parsed_co, parsed_role = parse_company_role(role, source, url)
        if not parsed_co and "hirist.com" in (url or ""):
            parsed_co, parsed_role = parse_hirist_url_slug(url)
        if not parsed_co and "cutshort.io/job/" in (url or ""):
            parsed_co, parsed_role = parse_cutshort_url_slug(url)
        if parsed_co:
            co = parsed_co
        if parsed_role and parsed_role != role:
            role = parsed_role

    # Strip trailing " -" artifacts from Cutshort titles
    role = re.sub(r"\s+-\s*$", "", role)

    return co, role


def accept_job(
    company: str,
    title: str,
    url: str,
    source: str = "",
    *,
    description: str = "",
) -> tuple[bool, str]:
    """Return (keep, reject_reason). Empty reason when keep=True."""
    src = (source or "").lower()

    if is_spam_url(url):
        return False, "spam_domain"

    if is_staffing_listing(company, url, title):
        return False, "staffing_agency"

    co, role = normalize_job_fields(company, title, url, src)
    if is_invalid_company(co, url):
        return False, "invalid_company"

    if is_junk_title(title):
        return False, "junk_title"

    if is_category_url(url):
        return False, "category_url"

    if requires_individual_url(src) and not is_individual_job_url(url, src):
        return False, "not_individual_job"

    if is_junk_title(role):
        return False, "junk_title"

    if co.lower() in SOURCE_NAMES:
        return False, "no_real_company"

    if len(role) < 5:
        return False, "title_too_short"

    # Must match at least one engineering keyword (light sanity check).
    eng = (
        "engineer", "developer", "sde", "backend", "frontend", "ml", "ai",
        "machine learning", "software", "platform", "sre", "devops", "python",
        "golang", "fresher", "graduate", "entry",
    )
    text = f"{role} {description}".lower()
    if not any(k in text for k in eng):
        return False, "not_engineering"

    if os.environ.get("PREFERENCES_FILTER") == "1":
        from llm_filter import filter_job
        ok, reason = filter_job(co, role, description)
        if not ok:
            return False, reason

    return True, ""
