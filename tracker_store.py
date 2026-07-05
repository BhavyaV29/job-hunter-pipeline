"""CSV-backed store for the web dashboard.

Reads and writes the same ``tracker.csv`` as the CLI, by column name, keeping the
file's own header order — so it can never reorder the canonical schema and stays
in sync with a ``morning.py`` run. In demo mode it serves ``tracker.sample.csv``
read-only, so a public deploy is safe to share.
"""
from __future__ import annotations

import csv
import os
import threading
from datetime import date, datetime
from pathlib import Path

import score as _score

ROOT = Path(__file__).resolve().parent

# Only used to seed an empty tracker; reads always honour the file's real header.
FIELDS = [
    "date_found", "company", "score", "stage", "url", "role", "location",
    "salary", "deadline", "source", "applied_date", "contact_name",
    "contact_email", "job_id", "resume_variant", "referral_contact", "oa_date",
    "phone_date", "tech_date", "onsite_date", "offer_details", "next_action",
    "next_action_date", "notes", "exp_years", "exp_match",
]
ACTIVE_STAGES = ("sourced", "new")
FUNNEL_STAGES = (
    "sourced", "applied", "oa", "phone", "tech", "onsite", "offer",
    "rejected", "withdrawn", "not_applicable",
)

_write_lock = threading.Lock()


class ReadOnlyError(RuntimeError):
    """Raised when a write is attempted against the demo (read-only) tracker."""


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def tracker_path() -> Path:
    return Path(os.environ.get("TRACKER_CSV", str(ROOT / "tracker.csv")))


def sample_path() -> Path:
    return Path(os.environ.get("SAMPLE_CSV", str(ROOT / "tracker.sample.csv")))


def is_demo() -> bool:
    # Explicit only: a fresh live instance (no tracker yet) stays configurable.
    return _flag("DEMO_MODE")


def active_path() -> Path:
    return sample_path() if is_demo() else tracker_path()


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _days_until(value) -> int | None:
    s = (value or "").strip()[:10]
    if not s:
        return None
    try:
        return (datetime.strptime(s, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def read_rows(path: Path | None = None) -> tuple[list[str], list[dict]]:
    p = path or active_path()
    if not p.exists():
        return list(FIELDS), []
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or FIELDS), [dict(r) for r in reader]


def _thresholds():
    src = ROOT / "sources.yaml"
    min_inr, remote_floor_inr = _score.load_thresholds(src)
    return min_inr, remote_floor_inr, _score.load_expiry_warn_days(src), _score.load_dream_companies(src)


def _decorate(rows: list[dict]) -> list[dict]:
    """Attach _score / _tier using score.py so the UI matches the CLI ranking."""
    min_inr, remote_floor_inr, warn_days, dreams = _thresholds()
    today = date.today().isoformat()
    out = []
    for r in rows:
        r = dict(r)
        stored = str(r.get("score", "")).strip()
        r["_score"] = _to_int(stored) if stored else _score.total_score(
            r, min_inr, remote_floor_inr, warn_days, dreams)
        r["_tier"] = _score.tier_of(r.get("role", ""), r.get("location", ""),
                                    r.get("salary", ""), min_inr, remote_floor_inr)
        r["_new"] = (r.get("date_found") or "").strip()[:10] == today
        r["_deadline_days"] = _days_until(r.get("deadline"))
        out.append(r)
    return out


def list_roles(*, stage: str = "", query: str = "", sort: str = "score",
               limit: int = 0, triage_only: bool = False) -> list[dict]:
    _, rows = read_rows()
    rows = _decorate(rows)

    if triage_only:
        rows = [r for r in rows if (r.get("stage") or "").strip().lower() in ACTIVE_STAGES]
    elif stage:
        rows = [r for r in rows if (r.get("stage") or "").strip().lower() == stage.strip().lower()]

    if query:
        q = query.strip().lower()
        rows = [r for r in rows if any(
            q in (r.get(k, "") or "").lower() for k in ("company", "role", "location"))]

    keys = {
        "deadline": lambda r: r.get("deadline") or "9999-12-31",
        "company": lambda r: (r.get("company", "") or "").lower(),
        "date": lambda r: r.get("date_found") or "",
    }
    if sort in keys:
        rows.sort(key=keys[sort], reverse=(sort == "date"))
    else:
        rows.sort(key=lambda r: r.get("_score", 0), reverse=True)

    return rows[:limit] if limit and limit > 0 else rows


def stats() -> dict:
    _, rows = read_rows()
    by_stage: dict[str, int] = {}
    for r in rows:
        s = (r.get("stage") or "").strip().lower() or "unknown"
        by_stage[s] = by_stage.get(s, 0) + 1
    return {
        "total": len(rows),
        "triage": sum(by_stage.get(s, 0) for s in ACTIVE_STAGES),
        "by_stage": by_stage,
        "funnel": [(s, by_stage[s]) for s in FUNNEL_STAGES if by_stage.get(s)],
        "demo": is_demo(),
    }


def update_stage(url: str, new_stage: str, *, extra: dict | None = None) -> bool:
    """Set the stage (+ optional fields) of the row matching ``url``.

    Atomic write (tmp + replace) after a .bak backup, preserving the header.
    Returns True when a row matched; raises ReadOnlyError in demo mode.
    """
    if is_demo():
        raise ReadOnlyError("tracker is read-only in demo mode")
    url = (url or "").strip()
    if not url:
        return False

    with _write_lock:
        path = tracker_path()
        fieldnames, rows = read_rows(path)
        row = next((r for r in rows if (r.get("url") or "").strip() == url), None)
        if row is None:
            return False
        row["stage"] = new_stage
        for k, v in (extra or {}).items():
            if k in fieldnames:
                row[k] = v

        if path.exists():
            try:
                path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
            except OSError:
                pass
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        tmp.replace(path)
        return True
