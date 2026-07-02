"""Layman end-of-run summary for morning.py / outreach_run.py."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

JOBSEARCH = Path(__file__).resolve().parent
OUTREACH = JOBSEARCH.parent / "outreach"
TRACKER = JOBSEARCH / "tracker.csv"


def tracker_stats() -> tuple[int, int]:
    """Return (total_tracked, new_today)."""
    if not TRACKER.is_file():
        return 0, 0
    today = date.today().isoformat()
    total = new_today = 0
    with TRACKER.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            if (row.get("date_found") or "").startswith(today):
                new_today += 1
    return total, new_today


def _count_csv_data_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def _count_md_h2(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ") and not line.startswith("### ")
    )


def outreach_stats() -> dict[str, int]:
    return {
        "contacts": _count_csv_data_rows(OUTREACH / "contacts.csv"),
        "cold_drafts": _count_md_h2(OUTREACH / "cold_mail_drafts.md"),
        "referral_drafts": _count_md_h2(OUTREACH / "referral_drafts.md"),
    }


def print_morning_wrapup(
    *,
    sheets_ok: bool,
    sheet_id: str = "",
    outreach_ran: bool = True,
) -> None:
    total, new_today = tracker_stats()
    bar = "═" * 54

    print(f"\n{bar}")
    print("  ☀  MORNING DONE — here's your list")
    print(bar)

    print("\n  WHAT HAPPENED")
    if sheets_ok and sheet_id:
        new_bit = f", {new_today} new today" if new_today else ""
        print(f"  • Google Sheet updated — {total} jobs tracked{new_bit}")
    else:
        new_bit = f" ({new_today} new today)" if new_today else ""
        print(f"  • Job list updated — {total} roles in tracker.csv{new_bit}")

    if outreach_ran:
        o = outreach_stats()
        print(f"  • Contact list ready — {o['contacts']} people saved")
        print(f"  • {o['cold_drafts']} cold-email drafts written")
        print(f"  • {o['referral_drafts']} LinkedIn referral drafts written")

    print("\n  WHAT TO DO NOW")
    step = 1
    if sheets_ok and sheet_id:
        print(f"  {step}. JOBS — Open your Sheet (link below). Apply to the top 5.")
        print('     Mark each row: stage = "applied", fill applied_date = today.')
        step += 1
    elif total:
        print(f"  {step}. JOBS — Open tracker.csv or run with Google Sheets configured.")
        step += 1

    if outreach_ran:
        o = outreach_stats()
        if o["referral_drafts"]:
            print(
                f"  {step}. REFERRALS — Open outreach/referral_drafts.md "
                f"({o['referral_drafts']} messages)."
            )
            print("     Personalise each one, find the person on LinkedIn, send manually.")
            step += 1
        if o["cold_drafts"]:
            print(
                f"  {step}. COLD EMAIL — Open outreach/cold_mail_drafts.md "
                f"({o['cold_drafts']} emails)."
            )
            print("     Personalise, attach resume PDF from resume/out/, send manually.")
            step += 1
        if sheets_ok:
            print(
                f"  {step}. LOG IT — In your Google Sheet, open the **outreach** tab (recommended)."
            )
            print("     Add a row: company, person_name, channel (linkedin/email),")
            print('     message_type (cold/referral), status = sent, sent_date = today.')
            print("     Or from terminal: uv run outreach_log.py --add company=\"...\" person=\"...\"")
            print("     (follow_up_due fills automatically on the next morning run.)")
        else:
            print(
                f"  {step}. LOG IT — uv run outreach_log.py --add company=\"...\" person=\"...\""
            )
            print("     (Or configure Google Sheets in .env to use the outreach tab.)")

    print()
    if sheets_ok and sheet_id:
        print(f"  Sheet → https://docs.google.com/spreadsheets/d/{sheet_id}")
    if outreach_ran:
        print(f"  Drafts → {OUTREACH}/")
    print("\n  Tomorrow: cd job-hunter-pipeline && python3 morning.py --force")
    print(f"{bar}\n")
