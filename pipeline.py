# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Daily application-pipeline dashboard + "mark applied" workflow for tracker.csv.

Usage:
    uv run pipeline.py              # full dashboard (funnel + due + active + pace)
    uv run pipeline.py --due        # only what's due/overdue today
    uv run pipeline.py --funnel     # funnel counts only

Mark roles applied (so score.py drops them from tomorrow's triage queue):
    uv run pipeline.py --applied <url1> <url2> ...   # mark these URLs applied
    uv run pipeline.py --applied-file applied.txt    # one URL per line (# = comment)
    uv run pipeline.py --mark-top 25                 # mark current top-N sourced (by score)

Each sets stage=applied + applied_date=today for matching rows, leaves everything
else intact, warns about URLs it can't find, and never regresses a role that has
already advanced past 'applied'. URL-based marking is the primary, reliable path.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sys
from pathlib import Path

TRACKER = Path(__file__).parent / "tracker.csv"

STAGES = [
    "sourced", "applied", "oa", "phone_screen", "tech_screen",
    "onsite", "offer", "rejected", "withdrawn", "not_applicable",
]
TERMINAL = {"rejected", "withdrawn", "offer", "not_applicable"}
DAILY_TARGET = 20  # applications/day

# Tabulate is optional - gracefully fall back to manual formatting.
try:
    from tabulate import tabulate as _tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False


def _tabulate_rows(headers: list[str], rows: list[list], fmt: str = "simple") -> str:
    if _HAS_TABULATE:
        return _tabulate(rows, headers=headers, tablefmt=fmt)
    col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                  for i, h in enumerate(headers)]
    sep = "  ".join("-" * w for w in col_widths)
    header_line = "  ".join(str(h).ljust(w) for h, w in zip(headers, col_widths))
    body = "\n".join(
        "  ".join(str(r[i]).ljust(col_widths[i]) for i in range(len(headers)))
        for r in rows
    )
    return f"{header_line}\n{sep}\n{body}"


def load_rows() -> list[dict]:
    if not TRACKER.exists():
        return []
    with TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_rows_with_fields() -> tuple[list[dict], list[str]]:
    """Load rows plus the tracker's column order (so we can rewrite it intact)."""
    if not TRACKER.exists():
        return [], []
    with TRACKER.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        return list(reader), fields


def _save_rows(rows: list[dict], fieldnames: list[str]) -> None:
    """Atomically rewrite tracker.csv preserving its exact schema/column order."""
    tmp = TRACKER.with_name(TRACKER.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(TRACKER)


# Only roles still in the triage queue get flipped to 'applied'. A role that has
# already advanced (applied/oa/.../offer/rejected/withdrawn) is NOT regressed.
_FLIPPABLE_STAGES = {"sourced", "new", ""}


def _read_url_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        print(f"--applied-file: '{path}' not found.")
        return []
    urls = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            urls.append(s)
    return urls


def _top_sourced_urls(rows: list[dict], n: int):
    """Return the URLs of the current top-N stage=sourced roles, ranked exactly
    like `score.py`. Returns None if score.py can't be imported."""
    try:
        import score
    except Exception as e:  # pragma: no cover - defensive
        print(f"--mark-top needs score.py importable ({type(e).__name__}); "
              f"use `--applied <url>` instead.")
        return None
    min_inr, remote_floor_inr = score.load_thresholds(
        Path(__file__).parent / "sources.yaml"
    )
    sourced = [r for r in rows
               if (r.get("stage") or "").strip().lower() in ("sourced", "new")]
    ranked = sorted(sourced,
                    key=lambda r: score.total_score(r, min_inr, remote_floor_inr),
                    reverse=True)
    return [r["url"] for r in ranked[:n] if r.get("url")]


def mark_applied(urls: list[str]) -> None:
    """Set stage=applied + applied_date=today for every tracker row whose URL is
    in `urls`. Preserves all other fields, skips already-advanced roles (no
    regression), and reports updated / already-applied / not-found counts."""
    rows, fields = load_rows_with_fields()
    if not rows:
        print("tracker.csv is empty or not found. Run `uv run fetch_jobs.py` first.")
        return
    today = dt.date.today().isoformat()
    want = {u.strip() for u in urls if u and u.strip()}
    if not want:
        print("No URLs provided to mark applied.")
        return

    matched, newly, already = set(), 0, 0
    for r in rows:
        if r.get("url") in want:
            matched.add(r["url"])
            stage = (r.get("stage") or "").strip().lower()
            if stage in _FLIPPABLE_STAGES:
                r["stage"] = "applied"
                if not (r.get("applied_date") or "").strip():
                    r["applied_date"] = today
                newly += 1
            else:
                already += 1
                if not (r.get("applied_date") or "").strip():
                    r["applied_date"] = today

    if newly or already:
        _save_rows(rows, fields)

    not_found = sorted(want - matched)
    print(f"\nMarked {newly} role(s) as applied (date={today}).")
    if already:
        print(f"  - {already} URL(s) had already advanced past 'sourced' - "
              f"left at their current stage (no regression).")
    if not_found:
        print(f"  ! {len(not_found)} URL(s) not found in tracker:")
        for u in not_found:
            print(f"      {u}")
    print("  Applied roles are now excluded from `uv run score.py` (triage queue).")


def print_funnel(rows: list[dict]) -> None:
    from collections import Counter
    counts = Counter(r.get("stage", "") for r in rows)
    total = len(rows)
    print(f"\n{'='*60}")
    print(f"  FUNNEL  ({total} total tracked)")
    print(f"{'='*60}")
    table_rows = []
    for s in STAGES:
        n = counts.get(s, 0)
        bar = "█" * min(n, 40) + ("…" if n > 40 else "")
        table_rows.append([s, n, bar])
    others = {k: v for k, v in counts.items() if k not in STAGES and k}
    for k in sorted(others):
        n = others[k]
        bar = "█" * min(n, 40) + ("…" if n > 40 else "")
        table_rows.append([f"({k})", n, bar])
    print(_tabulate_rows(["Stage", "Count", ""], table_rows))


def print_due(rows: list[dict]) -> None:
    today = dt.date.today().isoformat()
    due = [r for r in rows if r.get("next_action_date") and r["next_action_date"] <= today]
    due.sort(key=lambda r: r.get("next_action_date", ""))
    print(f"\n{'='*60}")
    print(f"  DUE / OVERDUE  ({len(due)} items)  — today: {today}")
    print(f"{'='*60}")
    if not due:
        print("  Nothing due. Inbox zero!")
        return
    table_rows = []
    for r in due:
        d = r.get("next_action_date", "")
        overdue = "!!" if d < today else ""
        table_rows.append([
            r.get("company", "")[:20],
            r.get("role", "")[:32],
            r.get("stage", "")[:14],
            d,
            overdue,
            r.get("next_action", "")[:40],
        ])
    print(_tabulate_rows(["Company", "Role", "Stage", "Date", "", "Next Action"], table_rows))


def print_active(rows: list[dict]) -> None:
    # TERMINAL includes not_applicable, so dismissed rows are excluded here.
    # They're also filtered by applied_date (not_applicable rows have none).
    active = [r for r in rows
              if r.get("stage") not in TERMINAL and r.get("applied_date")]
    active.sort(key=lambda r: r.get("applied_date", ""), reverse=True)
    print(f"\n{'='*60}")
    print(f"  ACTIVE PIPELINE  ({len(active)} roles in-flight)")
    print(f"{'='*60}")
    if not active:
        print("  No applied roles yet. Run `uv run fetch_jobs.py && uv run score.py` to source.")
        return
    table_rows = [
        [
            r.get("company", "")[:20],
            r.get("role", "")[:32],
            r.get("stage", "")[:14],
            r.get("applied_date", ""),
            r.get("next_action_date", ""),
        ]
        for r in active[:30]
    ]
    print(_tabulate_rows(
        ["Company", "Role", "Stage", "Applied", "Next Date"], table_rows
    ))
    if len(active) > 30:
        print(f"  ... and {len(active) - 30} more. Use tracker.csv for the full list.")


def _load_expiry_warn_days() -> int:
    """Read expiry_warn_days from sources.yaml -> filters, default 7."""
    sources = Path(__file__).parent / "sources.yaml"
    try:
        text = sources.read_text(encoding="utf-8")
        m = re.search(r"^\s*expiry_warn_days\s*:\s*(\d+)", text, re.MULTILINE)
        return int(m.group(1)) if m else 7
    except OSError:
        return 7


def print_closing_soon(rows: list[dict], warn_days: int = 7) -> None:
    """Show stage=sourced roles whose deadline is within warn_days days, soonest first.

    Displayed FIRST in the default dashboard so closing roles are impossible to miss.
    Columns: company, role, tier, salary, deadline, days_left.
    """
    today = dt.date.today()
    soon = []
    for r in rows:
        if (r.get("stage") or "").strip() not in ("sourced", "new"):
            continue
        deadline = (r.get("deadline") or "").strip()
        if not deadline:
            continue
        try:
            dldate = dt.date.fromisoformat(deadline)
            days_left = (dldate - today).days
            if 0 <= days_left <= warn_days:
                soon.append((r, dldate, days_left))
        except ValueError:
            pass
    soon.sort(key=lambda x: x[1])  # soonest first

    print(f"\n{'='*60}")
    print(f"  CLOSING SOON  ({len(soon)} sourced role(s) within {warn_days} days)")
    print(f"{'='*60}")
    if not soon:
        print(f"  No sourced roles closing within the next {warn_days} days.")
        return

    # Get tier labels via score.py (soft import — pipeline stays runnable alone)
    try:
        import score as _score
        _min_inr, _rfloori = _score.load_thresholds(Path(__file__).parent / "sources.yaml")

        def _tier_lbl(r: dict) -> str:
            t = _score.tier_of(r.get("role", ""), r.get("location", ""),
                               r.get("salary", ""), _min_inr, _rfloori)
            return f"T{t}" if t else "-"
    except Exception:
        def _tier_lbl(r: dict) -> str:
            return ""

    table_rows = [
        [
            r.get("company", "")[:20],
            r.get("role", "")[:32],
            _tier_lbl(r),
            r.get("salary", "")[:18] or "—",
            dldate.isoformat(),
            f"{days_left}d",
        ]
        for r, dldate, days_left in soon
    ]
    print(_tabulate_rows(["Company", "Role", "Tier", "Salary", "Deadline", "Left"], table_rows))


def print_weekly_pace(rows: list[dict]) -> None:
    today = dt.date.today()
    week_ago = (today - dt.timedelta(days=7)).isoformat()
    weekly = [r for r in rows if r.get("applied_date", "") >= week_ago]
    target = DAILY_TARGET * 7
    pct = len(weekly) / target * 100 if target else 0
    print(f"\n{'='*60}")
    print(f"  WEEKLY PACE  (target: {DAILY_TARGET}/day = {target}/week)")
    print(f"{'='*60}")
    print(f"  Applied last 7 days: {len(weekly)} / {target}  ({pct:.0f}%)")

    by_day: dict[str, int] = {}
    for r in weekly:
        d = r.get("applied_date", "")
        if d:
            by_day[d] = by_day.get(d, 0) + 1

    table_rows = []
    for i in range(6, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        n = by_day.get(d, 0)
        bar = "█" * min(n, 30) + ("…" if n > 30 else "")
        tag = " <- today" if i == 0 else ""
        table_rows.append([d + tag, n, bar])
    print(_tabulate_rows(["Date", "Apps", ""], table_rows))


def _pull_from_sheets() -> None:
    """Pull latest tracker data from Google Sheets before the dashboard.

    Skips gracefully (with a brief note) if GOOGLE_SHEETS_ID or
    GOOGLE_SERVICE_ACCOUNT_JSON are not set in the environment.
    """
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sheet_id or not sa_json:
        print("  (--pull-sheets: GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping)")
        return
    import subprocess
    sync_script = Path(__file__).parent / "sheets_sync.py"
    print("Pulling latest data from Google Sheets...")
    try:
        subprocess.run(["uv", "run", str(sync_script), "--pull"], check=False)
    except FileNotFoundError:
        print("  (--pull-sheets: uv not found — skipping Sheets pull)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--due", action="store_true", help="Only show due/overdue items")
    ap.add_argument("--funnel", action="store_true", help="Only show funnel counts")
    ap.add_argument("--closing", action="store_true",
                    help="Only show sourced roles closing within expiry_warn_days days")
    ap.add_argument("--applied", nargs="+", metavar="URL",
                    help="Mark these URL(s) as applied (stage=applied, date=today)")
    ap.add_argument("--applied-file", metavar="PATH",
                    help="Mark URLs listed in a file (one per line; # = comment) as applied")
    ap.add_argument("--mark-top", type=int, metavar="N",
                    help="Mark the current top-N sourced roles (by score.py rank) as applied")
    ap.add_argument("--pull-sheets", action="store_true",
                    help="Pull latest data from Google Sheets before showing dashboard "
                         "(runs sheets_sync --pull; skips gracefully if Sheets not configured)")
    args = ap.parse_args()

    # ---- mark-applied modes (mutate the tracker, then exit) ----
    if args.applied or args.applied_file or args.mark_top:
        urls: list[str] = []
        if args.applied:
            urls += args.applied
        if args.applied_file:
            urls += _read_url_file(args.applied_file)
        if args.mark_top:
            rows = load_rows()
            top = _top_sourced_urls(rows, args.mark_top)
            if top is None:
                sys.exit(1)
            urls += top
        mark_applied(urls)
        print()
        return

    if args.pull_sheets:
        _pull_from_sheets()

    rows = load_rows()
    if not rows:
        print("tracker.csv is empty or not found. Run `uv run fetch_jobs.py` first.")
        sys.exit(0)

    warn_days = _load_expiry_warn_days()
    if args.closing:
        print_closing_soon(rows, warn_days)
    elif args.funnel:
        print_funnel(rows)
    elif args.due:
        print_due(rows)
    else:
        # CLOSING SOON comes first — impossible to miss roles with imminent deadlines.
        print_closing_soon(rows, warn_days)
        print_funnel(rows)
        print_due(rows)
        print_active(rows)
        print_weekly_pace(rows)
    print()


if __name__ == "__main__":
    main()
