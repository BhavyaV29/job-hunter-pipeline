# /// script
# requires-python = ">=3.9"
# dependencies = ["httpx"]
# ///
"""Validate tracker job links so you stop clicking into dead / expired postings.

Each URL is classified as:
  live     — 200 and the page still looks like a real, open posting
  dead     — 404 / 410, or a redirect to a jobs-home / "not found" page
  expired  — 200 but the page says the role is closed (ATS-specific wording:
             Greenhouse, Lever, Ashby, Workday, SmartRecruiters, LinkedIn, Indeed)
  unknown  — blocked (401/403/429), 5xx, or our own network error — never flagged

Good web citizen: async with a concurrency cap, a per-host rate limit, sane
timeouts + limited retries, a realistic User-Agent, and a JSON result cache
(.linkcheck_cache.json, same pattern as .jd_cache.json) so re-runs are cheap.

Usage:
    uv run link_check.py --dry-run            # report only, no tracker writes
    uv run link_check.py                       # DROP dead/expired sourced rows (default)
    uv run link_check.py --mark-only           # only mark link_status, keep rows
    uv run link_check.py --limit 50            # only the first 50 sourced rows

Never drops UNKNOWN (blocked/unverifiable) rows or advanced rows (applied, oa,
phone, onsite, offer, ...) — those are preserved even if the link looks dead.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import shutil
import time
from collections import Counter
from pathlib import Path

import httpx

from dedup_keys import norm_url

LIVE, DEAD, EXPIRED, UNKNOWN = "live", "dead", "expired", "unknown"

CACHE_NAME = ".linkcheck_cache.json"

# Realistic browser UA — many ATS reject the generic bot UA.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ATS / board wording that means "this posting is closed" (checked on a 200 page).
_EXPIRED_PATTERNS = (
    "no longer accepting applications",
    "no longer accepting application",
    "this job is no longer available",
    "this job posting is no longer available",
    "this position is no longer available",
    "this posting is no longer active",
    "this job posting is no longer active",
    "the job you are looking for is no longer available",
    "job is no longer available",
    "position has been filled",
    "this position has been filled",
    "the position has been closed",
    "this role is no longer open",
    "no longer open for applications",
    "applications are closed",
    "application deadline has passed",
    "this job has expired",
    "job posting has expired",
    "posting has expired",
    "this posting is not available",
    "posting is not available",
    "vacancy is closed",
    "this vacancy is now closed",
    "we are no longer accepting applications",
    "job opening is closed",
    "position is closed",
)

# Generic "page gone" wording — a soft 404 served with a 200 status.
_NOT_FOUND_PATTERNS = (
    "page not found",
    "job not found",
    "position not found",
    "the page you requested",
    "the page you are looking for",
    "this page doesn't exist",
    "this page does not exist",
    "404 error",
    "error 404",
)

# Generic home / listing paths a dead posting commonly redirects to.
_HOME_PATHS = frozenset({
    "", "/", "/jobs", "/job", "/careers", "/career", "/search", "/positions",
    "/openings", "/opportunities", "/en-us", "/vacancies", "/join-us", "/work-with-us",
})

# Hosts whose *individual* postings always have >= 2 path segments, so a
# single-segment path is a board root (i.e. a redirect-to-home = dead).
_ATS_BOARD_HOSTS = (
    "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
    "jobs.ashbyhq.com", "apply.workable.com", "jobs.smartrecruiters.com",
)


def _path(url: str) -> str:
    try:
        return (httpx.URL(url).path or "").rstrip("/") or "/"
    except Exception:
        return "/"


def _is_home_url(url: str) -> bool:
    """True when the URL is a board/careers landing page, not a single posting."""
    try:
        u = httpx.URL(url)
    except Exception:
        return False
    host = (u.host or "").lower().removeprefix("www.")
    path = (u.path or "").rstrip("/")
    if path.lower() in _HOME_PATHS:
        return True
    segments = [p for p in path.split("/") if p]
    for board in _ATS_BOARD_HOSTS:
        if host == board and len(segments) <= 1:
            return True
    if host.endswith(".recruitee.com") and len(segments) <= 1:
        return True
    return False


def classify(status_code: int, final_url: str, body: str,
             *, request_url: str = "") -> str:
    """Classify one HTTP result. Pure — no I/O, so it is exhaustively unit-tested.

    Precedence: hard-dead status > blocked/transient > redirect-to-home >
    expired wording > soft-404 wording > live.
    """
    if status_code in (404, 410):
        return DEAD
    if status_code in (401, 403, 429) or status_code >= 500:
        return UNKNOWN
    if 200 <= status_code < 300:
        if request_url and norm_url(final_url) != norm_url(request_url) \
                and _is_home_url(final_url):
            return DEAD
        low = (body or "").lower()
        if any(p in low for p in _EXPIRED_PATTERNS):
            return EXPIRED
        if any(p in low for p in _NOT_FOUND_PATTERNS):
            return DEAD
        return LIVE
    return UNKNOWN


# ---------------------------------------------------------------------------
# Async fetching (concurrency cap + per-host rate limit + limited retries)
# ---------------------------------------------------------------------------

class _HostThrottle:
    """Spaces requests to the same host by at least `min_interval` seconds."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            delta = time.monotonic() - self._last.get(host, 0.0)
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last[host] = time.monotonic()


async def _request(client, method, url, *, throttle, host, timeout, retries):
    for attempt in range(retries + 1):
        await throttle.wait(host)
        try:
            return await client.request(
                method, url, timeout=timeout, follow_redirects=True, headers=HEADERS,
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return None


async def check_url(client, url, *, throttle, sem, timeout=15.0, retries=2) -> dict:
    """HEAD (cheap) then GET (for body/final URL). Returns a result dict."""
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:
        return {"verdict": UNKNOWN, "status": 0, "final_url": url}

    async with sem:
        head = await _request(client, "HEAD", url, throttle=throttle, host=host,
                              timeout=timeout, retries=retries)
        if head is not None:
            st, fin = head.status_code, str(head.url)
            if st in (404, 410):
                return {"verdict": DEAD, "status": st, "final_url": fin}
            if st in (401, 403, 429) or st >= 500:
                return {"verdict": UNKNOWN, "status": st, "final_url": fin}
            if 200 <= st < 300 and norm_url(fin) != norm_url(url) and _is_home_url(fin):
                return {"verdict": DEAD, "status": st, "final_url": fin}

        got = await _request(client, "GET", url, throttle=throttle, host=host,
                             timeout=timeout, retries=retries)
        if got is None:
            return {"verdict": UNKNOWN, "status": 0, "final_url": url}
        verdict = classify(got.status_code, str(got.url), got.text, request_url=url)
        return {"verdict": verdict, "status": got.status_code, "final_url": str(got.url)}


async def _check_all(urls, *, client, concurrency, per_host_delay, timeout, retries):
    throttle = _HostThrottle(per_host_delay)
    sem = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *(check_url(client, u, throttle=throttle, sem=sem, timeout=timeout,
                    retries=retries) for u in urls)
    )
    return dict(zip(urls, results))


async def check_urls(urls, *, client=None, concurrency=8, per_host_delay=1.0,
                     timeout=15.0, retries=2) -> dict:
    """Check many URLs concurrently. Pass `client` (e.g. an httpx.AsyncClient with
    a MockTransport) to inject responses in tests; otherwise one is created."""
    if client is not None:
        return await _check_all(urls, client=client, concurrency=concurrency,
                                per_host_delay=per_host_delay, timeout=timeout,
                                retries=retries)
    async with httpx.AsyncClient() as c:
        return await _check_all(urls, client=c, concurrency=concurrency,
                                per_host_delay=per_host_delay, timeout=timeout,
                                retries=retries)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(base: Path | None = None) -> Path:
    return (base or Path(__file__).parent) / CACHE_NAME


def load_cache(base: Path | None = None) -> dict:
    path = _cache_path(base)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict, base: Path | None = None) -> None:
    try:
        _cache_path(base).write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


def _is_fresh(entry: dict, *, ttl_days: int, dead_ttl_days: int) -> bool:
    ts = entry.get("checked")
    if not ts:
        return False
    try:
        checked = dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    age_days = (dt.datetime.now() - checked).total_seconds() / 86400
    ttl = dead_ttl_days if entry.get("verdict") in (DEAD, EXPIRED) else ttl_days
    return age_days < ttl


# ---------------------------------------------------------------------------
# Tracker integration
# ---------------------------------------------------------------------------

_COLLAPSIBLE_STAGES = frozenset({"sourced", "new", ""})
LINK_STATUS_FIELD = "link_status"


def check_tracker_links(
    tracker_path: Path,
    *,
    drop_dead: bool = True,
    dry_run: bool = False,
    limit: int = 0,
    concurrency: int = 8,
    per_host_delay: float = 1.0,
    timeout: float = 15.0,
    retries: int = 2,
    ttl_days: int = 7,
    dead_ttl_days: int = 30,
    backup: bool = True,
) -> dict:
    """Check sourced/new tracker URLs and, by default, DROP the ones that are
    dead/expired; pass drop_dead=False to only mark link_status instead.

    Only sourced/new rows are ever candidates, so advanced rows (applied, oa,
    phone, onsite, offer, ...) are never dropped even if their link is dead — the
    user may have already applied. UNKNOWN (blocked/unverifiable) rows are never
    dropped either. Definitive verdicts are always written to link_status.

    Returns a summary dict: checked, verdicts (Counter), removed, examples.
    """
    if not tracker_path.exists():
        return {"checked": 0, "verdicts": Counter(), "removed": 0, "examples": {}}

    with tracker_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if LINK_STATUS_FIELD not in fieldnames:
        fieldnames = fieldnames + [LINK_STATUS_FIELD]

    candidates = [
        r for r in rows
        if (r.get("stage") or "").strip().lower() in _COLLAPSIBLE_STAGES
        and (r.get("url") or "").strip().lower().startswith("http")
    ]
    if limit and limit > 0:
        candidates = candidates[:limit]

    # One network hit per canonical URL, even if several rows share it.
    canon_of = {id(r): norm_url(r["url"]) for r in candidates}
    rep_url: dict[str, str] = {}
    for r in candidates:
        rep_url.setdefault(canon_of[id(r)], r["url"])

    cache = load_cache(tracker_path.parent)
    to_check = [
        raw for cu, raw in rep_url.items()
        if not _is_fresh(cache.get(cu, {}), ttl_days=ttl_days, dead_ttl_days=dead_ttl_days)
    ]

    if to_check:
        fresh = asyncio.run(check_urls(
            to_check, concurrency=concurrency, per_host_delay=per_host_delay,
            timeout=timeout, retries=retries,
        ))
        now = dt.datetime.now().isoformat(timespec="seconds")
        for raw, res in fresh.items():
            cache[norm_url(raw)] = {**res, "checked": now}
        save_cache(cache, tracker_path.parent)

    verdicts: Counter = Counter()
    examples: dict[str, list] = {DEAD: [], EXPIRED: []}
    drop_ids: set[int] = set()
    for r in candidates:
        entry = cache.get(canon_of[id(r)])
        if not entry:
            continue
        v = entry.get("verdict", UNKNOWN)
        verdicts[v] += 1
        if v in (DEAD, EXPIRED):
            if len(examples[v]) < 5:
                examples[v].append({
                    "company": r.get("company", ""), "role": r.get("role", ""),
                    "url": r.get("url", ""), "status": entry.get("status"),
                })
            if drop_dead:
                drop_ids.add(id(r))
        if v in (LIVE, DEAD, EXPIRED):  # definitive verdicts get written back
            r[LINK_STATUS_FIELD] = v

    removed = 0
    if drop_dead and drop_ids:
        kept = [r for r in rows if id(r) not in drop_ids]
        removed = len(rows) - len(kept)
        rows = kept

    if not dry_run:
        if backup:
            _backup(tracker_path)
        tmp = tracker_path.with_name(tracker_path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        tmp.replace(tracker_path)

    return {"checked": sum(verdicts.values()), "verdicts": verdicts,
            "removed": removed, "examples": examples}


def _backup(tracker_path: Path) -> None:
    if tracker_path.exists():
        shutil.copy2(tracker_path, tracker_path.with_name(tracker_path.name + ".bak"))


def _fmt_examples(examples: dict) -> str:
    out = []
    for verdict in (DEAD, EXPIRED):
        for ex in examples.get(verdict, [])[:3]:
            out.append(f"    [{verdict}] {ex['company']} — {ex['role']}  {ex['url']}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tracker", default="tracker.csv")
    ap.add_argument("--dry-run", action="store_true", help="Do not write tracker.csv")
    ap.add_argument("--mark-only", action="store_true",
                    help="Only mark link_status; do NOT remove dead/expired rows "
                         "(default is to remove them).")
    ap.add_argument("--limit", type=int, default=0, help="Only check the first N sourced rows")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--per-host-delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    dropping = not args.mark_only
    tracker_path = Path(__file__).parent / args.tracker
    summary = check_tracker_links(
        tracker_path, drop_dead=dropping, dry_run=args.dry_run,
        limit=args.limit, concurrency=args.concurrency,
        per_host_delay=args.per_host_delay, timeout=args.timeout,
    )
    v = summary["verdicts"]
    action = (("would remove" if dropping else "would flag") if args.dry_run
              else ("removed" if dropping else "flagged"))
    print(
        f"\n==> Link check: {summary['checked']} sourced URLs checked.\n"
        f"    live={v.get(LIVE, 0)}  dead={v.get(DEAD, 0)}  "
        f"expired={v.get(EXPIRED, 0)}  unknown={v.get(UNKNOWN, 0)}\n"
        f"    {action}: {v.get(DEAD, 0) + v.get(EXPIRED, 0)} dead/expired"
        + (f" ({summary['removed']} rows removed)" if dropping and not args.dry_run else "")
    )
    ex = _fmt_examples(summary["examples"])
    if ex:
        print("  Examples:\n" + ex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
