# /// script
# requires-python = ">=3.9"
# dependencies = ["gspread>=5.12", "google-auth>=2.29"]
# ///
"""
outreach_log.py — manage and review outreach activity.

Primary store: Google Sheets (same spreadsheet as job tracker, "outreach" tab).
Fallback: outreach/outreach_log.csv when Sheet credentials are unavailable.
The CSV is always written as a local mirror/backup.

Schema:
  date_logged, company, person_name, channel, role, job_id,
  message_type, sent_date, status, follow_up_due, last_follow_up, notes

channel:       linkedin | email | twitter
message_type:  cold | referral
status:        drafted | sent | replied | referred | no_response | closed

Usage:
    uv run outreach_log.py                           # dashboard (default)
    uv run outreach_log.py --dashboard               # follow-ups, funnel, pace
    uv run outreach_log.py --add company="Stripe" person="Sam B" channel=linkedin role="SWE" job_id="7618977" type=referral
    uv run outreach_log.py --mark company="Stripe" status=replied
    uv run outreach_log.py --sent company="Stripe"
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT   = SCRIPT_DIR.parent
LOG_CSV     = REPO_ROOT / "outreach" / "outreach_log.csv"

LOG_COLUMNS = [
    "date_logged", "company", "person_name", "channel", "role", "job_id",
    "message_type", "sent_date", "status", "follow_up_due",
    "last_follow_up", "notes",
]

VALID_CHANNELS  = {"linkedin", "email", "twitter"}
VALID_TYPES     = {"cold", "referral"}
VALID_STATUSES  = {"drafted", "sent", "replied", "referred", "no_response", "closed"}
FUNNEL_ORDER    = ["drafted", "sent", "replied", "referred", "no_response", "closed"]
FOLLOW_UP_DAYS  = 3


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _open_outreach_sheet():
    """Return (spreadsheet, worksheet) for the outreach tab, or None if unavailable.

    Falls back silently to CSV-only mode when:
    - GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_JSON env vars are missing
    - The service account JSON file doesn't exist
    - Any network / auth error occurs
    """
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
    sa_json  = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if not sheet_id or not sa_json:
        return None  # silently use CSV

    if not Path(sa_json).exists():
        print(f"Warning: service account JSON not found at {sa_json!r} — CSV-only mode")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            sa_json,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(sheet_id)
    except Exception as exc:
        print(f"Warning: could not connect to Google Sheet: {exc} — CSV-only mode")
        return None

    # Import init helper from sheets_sync in the same directory
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from sheets_sync import _init_outreach_tab  # type: ignore[import]

    worksheet = _init_outreach_tab(spreadsheet)
    return spreadsheet, worksheet


def _load_from_sheet(worksheet) -> list[dict]:
    """Read all data rows from the outreach worksheet, skipping blank rows."""
    all_values = worksheet.get_all_values()
    if not all_values or not all_values[0]:
        return []
    header = all_values[0]
    return [
        {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
        for row in all_values[1:]
        if any(cell.strip() for cell in row)
    ]


def _write_all_to_sheet(worksheet, rows: list[dict]) -> None:
    """Overwrite the outreach worksheet with header + all rows."""
    data: list[list[str]] = [LOG_COLUMNS]
    for row in rows:
        data.append([str(row.get(f, "") or "") for f in LOG_COLUMNS])
    worksheet.clear()
    worksheet.batch_update(
        [{"range": "A1", "values": data}],
        value_input_option="USER_ENTERED",
    )


def _append_to_sheet(worksheet, row: dict) -> None:
    """Append a single row to the outreach worksheet."""
    worksheet.append_row(
        [str(row.get(f, "") or "") for f in LOG_COLUMNS],
        value_input_option="USER_ENTERED",
    )


# ---------------------------------------------------------------------------
# CSV I/O  (local mirror / fallback)
# ---------------------------------------------------------------------------

def _ensure_log() -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_CSV.exists():
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_COLUMNS).writeheader()
        print(f"Created {LOG_CSV}")


def _load() -> list[dict]:
    if not LOG_CSV.exists():
        return []
    with open(LOG_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save(rows: list[dict]) -> None:
    with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _append_to_csv(row: dict) -> None:
    """Append a single row to the CSV (ensures file + header exist first)."""
    _ensure_log()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction="ignore").writerow(row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_kv(args: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for arg in args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _today() -> str:
    return date.today().isoformat()


def _add_days(base: str | None, n: int = FOLLOW_UP_DAYS) -> str:
    d = date.fromisoformat(base) if base else date.today()
    return (d + timedelta(days=n)).isoformat()


def _parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def _match_indices(rows: list[dict], company: str) -> list[int]:
    needle = company.lower()
    return [
        i for i, r in enumerate(rows)
        if (r.get("company") or "").lower() == needle
    ]


def _fill_follow_up_dates(rows: list[dict]) -> bool:
    """Set follow_up_due on sent rows when missing. Returns True if any row changed."""
    changed = False
    for r in rows:
        status = (r.get("status") or "").lower()
        sent = (r.get("sent_date") or "").strip()
        if status == "sent" and sent and not (r.get("follow_up_due") or "").strip():
            r["follow_up_due"] = _add_days(sent)
            changed = True
    return changed


def prepare_outreach_sheet(*, quiet: bool = False) -> bool:
    """Ensure outreach tab exists and pull Sheet → local CSV mirror.

    Returns True when Sheet is available and synced.
    """
    sheet_result = _open_outreach_sheet()
    if sheet_result is None:
        if not quiet:
            print(
                "  ⚠  Outreach log: Sheet not available (install gspread + check .env). "
                "Using CSV fallback.\n"
            )
        return False

    _, worksheet = sheet_result
    spreadsheet = sheet_result[0]
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from sheets_sync import _apply_outreach_formatting  # type: ignore[import]

    try:
        _apply_outreach_formatting(spreadsheet, worksheet)
    except Exception as exc:
        if not quiet:
            print(f"  ⚠  Outreach tab formatting skipped: {exc}")

    rows = _load_from_sheet(worksheet)
    if _fill_follow_up_dates(rows):
        _write_all_to_sheet(worksheet, rows)
        if not quiet:
            print("  ✓  Outreach tab: auto-filled follow_up_due for sent rows.")
    _save(rows)
    if not quiet:
        print(f"  ✓  Outreach tab ready — log new rows in the Sheet ({len(rows)} logged so far).")
    return True


def sync_from_sheet() -> list[dict]:
    """Pull outreach tab → CSV mirror; return rows (empty if Sheet unavailable)."""
    sheet_result = _open_outreach_sheet()
    if sheet_result is None:
        return _load()
    _, worksheet = sheet_result
    rows = _load_from_sheet(worksheet)
    if _fill_follow_up_dates(rows):
        _write_all_to_sheet(worksheet, rows)
    _save(rows)
    return rows


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _dashboard(rows: list[dict]) -> None:
    today = date.today()
    hr = "─" * 54

    print(f"\n{hr}")
    print("  OUTREACH DASHBOARD")
    print(f"{hr}")

    # Follow-ups due today
    due: list[dict] = []
    for r in rows:
        if (r.get("status") or "").lower() == "sent":
            d = _parse_date(r.get("follow_up_due"))
            if d is not None and d <= today:
                due.append(r)

    if due:
        print(f"\n⏰  Follow-ups due today ({len(due)}):\n")
        for r in due:
            co      = (r.get("company")     or "?")[:22]
            person  = (r.get("person_name") or "?")[:18]
            ch      = (r.get("channel")     or "?")[:9]
            due_str = r.get("follow_up_due", "")
            role    = (r.get("role")        or "")[:28]
            print(f"  • {co:<22}  {person:<18}  [{ch}]  due {due_str}  {role}")
    else:
        print("\n✓  No follow-ups due today.")

    # Funnel counts
    counts: dict[str, int] = {}
    for r in rows:
        s = (r.get("status") or "drafted").lower()
        counts[s] = counts.get(s, 0) + 1

    total = sum(counts.values())
    print(f"\n📊  Funnel  ({total} total)\n")
    for s in FUNNEL_ORDER:
        n   = counts.get(s, 0)
        bar = "█" * min(n, 20) + "░" * max(0, 20 - n)
        print(f"  {s:<12}  {bar}  {n:3d}")
    for s, n in sorted(counts.items()):
        if s not in FUNNEL_ORDER:
            print(f"  {s:<12}  {n:3d}")

    # Weekly pace
    week_ago = today - timedelta(days=7)
    sent_this_week = [
        r for r in rows
        if (r.get("status") or "").lower() in ("sent", "replied", "referred")
        and (_parse_date(r.get("sent_date")) or date.min) >= week_ago
    ]
    print(f"\n📬  Sent in last 7 days: {len(sent_this_week)}")
    print(f"    Target: 15–20 / week for a healthy pipeline")
    print(f"\n{hr}\n")


# ---------------------------------------------------------------------------
# --add
# ---------------------------------------------------------------------------

def _cmd_add(kv: dict[str, str]) -> None:
    company = kv.get("company", "").strip()
    if not company:
        print("Error: --add requires company=<name>")
        sys.exit(1)

    channel      = kv.get("channel", "").lower()
    message_type = kv.get("type", kv.get("message_type", "cold")).lower()
    status       = kv.get("status", "drafted").lower()
    today        = _today()

    sent_date     = kv.get("sent_date", today if status == "sent" else "")
    follow_up_due = _add_days(sent_date) if status == "sent" and sent_date else ""

    if channel and channel not in VALID_CHANNELS:
        print(f"Warning: channel='{channel}' not one of {sorted(VALID_CHANNELS)}")
    if message_type and message_type not in VALID_TYPES:
        print(f"Warning: type='{message_type}' not one of {sorted(VALID_TYPES)}")

    row: dict[str, str] = {
        "date_logged":    today,
        "company":        company,
        "person_name":    kv.get("person", kv.get("person_name", "")),
        "channel":        channel,
        "role":           kv.get("role", ""),
        "job_id":         kv.get("job_id", ""),
        "message_type":   message_type,
        "sent_date":      sent_date,
        "status":         status,
        "follow_up_due":  follow_up_due,
        "last_follow_up": "",
        "notes":          kv.get("notes", ""),
    }

    # CSV backup (always)
    _append_to_csv(row)

    # Sheet (if available)
    sheet_result = _open_outreach_sheet()
    if sheet_result is not None:
        _append_to_sheet(sheet_result[1], row)

    print(
        f"Added: {company}  person={row['person_name']}  "
        f"channel={channel}  status={status}"
    )
    if follow_up_due:
        print(f"Follow-up due: {follow_up_due}")


# ---------------------------------------------------------------------------
# --mark
# ---------------------------------------------------------------------------

def _cmd_mark(kv: dict[str, str]) -> None:
    company    = kv.get("company", "").strip()
    new_status = kv.get("status", "").strip().lower()
    if not company or not new_status:
        print("Error: --mark requires company=<name> status=<status>")
        sys.exit(1)

    sheet_result = _open_outreach_sheet()
    worksheet    = sheet_result[1] if sheet_result else None
    rows         = _load_from_sheet(worksheet) if worksheet else _load()

    indices = _match_indices(rows, company)
    if not indices:
        print(f"No rows found for company='{company}'.")
        sys.exit(1)

    today = _today()
    for i in indices:
        rows[i]["status"] = new_status
        if new_status in ("replied", "referred", "no_response", "closed"):
            rows[i]["last_follow_up"] = today

    if worksheet:
        _write_all_to_sheet(worksheet, rows)
    _save(rows)
    print(f"Updated {len(indices)} row(s) for '{company}' → status={new_status}")


# ---------------------------------------------------------------------------
# --sent
# ---------------------------------------------------------------------------

def _cmd_sent(kv: dict[str, str]) -> None:
    company = kv.get("company", "").strip()
    if not company:
        print("Error: --sent requires company=<name>")
        sys.exit(1)

    sheet_result = _open_outreach_sheet()
    worksheet    = sheet_result[1] if sheet_result else None
    rows         = _load_from_sheet(worksheet) if worksheet else _load()

    indices = _match_indices(rows, company)
    if not indices:
        print(f"No rows found for company='{company}'.")
        sys.exit(1)

    today = _today()
    fud   = _add_days(today)
    for i in indices:
        rows[i]["status"]        = "sent"
        rows[i]["sent_date"]     = today
        rows[i]["follow_up_due"] = fud

    if worksheet:
        _write_all_to_sheet(worksheet, rows)
    _save(rows)
    print(f"Marked {len(indices)} row(s) for '{company}' as sent.")
    print(f"Follow-up due: {fud}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--dashboard", action="store_true",
        help="Show follow-ups, funnel, and weekly pace (default mode)",
    )
    g.add_argument(
        "--add", nargs="+", metavar="KEY=VALUE",
        help="Append a new outreach row: company=X person=Y channel=linkedin ...",
    )
    g.add_argument(
        "--mark", nargs="+", metavar="KEY=VALUE",
        help="Update status for matching rows: company=X status=replied",
    )
    g.add_argument(
        "--sent", nargs="+", metavar="KEY=VALUE",
        help="Mark rows as sent + auto-set follow_up_due: company=X",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    _ensure_log()

    if args.add:
        _cmd_add(_parse_kv(args.add))
    elif args.mark:
        _cmd_mark(_parse_kv(args.mark))
    elif args.sent:
        _cmd_sent(_parse_kv(args.sent))
    else:
        sync_from_sheet()
        sheet_result = _open_outreach_sheet()
        if sheet_result is not None:
            rows = _load_from_sheet(sheet_result[1])
        else:
            rows = _load()
        _dashboard(rows)


if __name__ == "__main__":
    main()
