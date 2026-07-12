# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Unified outreach prep — one command instead of four.

Runs (in order):
  1. find_contacts.py   — Hunter.io + email patterns → outreach/contacts.csv
  2. cold_mail_drafter.py — per-role cold emails → outreach/cold_mail_drafts.md
  3. referral_drafter.py  — per-company referral DMs → outreach/referral_drafts.md
  4. outreach_log.py --dashboard — follow-ups due + funnel stats

Usage:
    python3 outreach_run.py
    python3 outreach_run.py --top 15 --contact-limit 12
    python3 outreach_run.py --skip-contacts   # drafts only (no Hunter calls)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from morning_summary import outreach_stats
from outreach_log import prepare_outreach_sheet

ROOT = Path(__file__).resolve().parent


def _run_step(label: str, cmd: list[str]) -> int:
    print(f"\n{'─' * 60}\n  Outreach · {label}\n{'─' * 60}")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=10,
                    help="Top N scored roles for cold mail; top N companies for referrals.")
    ap.add_argument("--contact-limit", type=int, default=10,
                    help="Max companies to query via find_contacts (Hunter budget).")
    ap.add_argument("--skip-contacts", action="store_true",
                    help="Skip find_contacts (use existing outreach/contacts.csv).")
    ap.add_argument("--skip-cold", action="store_true", help="Skip cold mail drafts.")
    ap.add_argument("--skip-referral", action="store_true", help="Skip referral drafts.")
    ap.add_argument("--skip-dashboard", action="store_true", help="Skip outreach dashboard.")
    args = ap.parse_args()

    py = sys.executable
    rc = 0

    print("\n  Outreach · Google Sheet tab (log everything here)")
    if not prepare_outreach_sheet():
        print("    Tip: pip install -e .  and set GOOGLE_SHEETS_ID + GOOGLE_SERVICE_ACCOUNT_JSON in .env\n")

    if not args.skip_contacts:
        code = _run_step(
            "find contacts",
            [py, str(ROOT / "find_contacts.py"), "--limit", str(args.contact_limit)],
        )
        if code != 0:
            rc = code

    if not args.skip_cold:
        code = _run_step(
            "cold mail drafts (one section per role)",
            [py, str(ROOT / "cold_mail_drafter.py"), "--top", str(args.top)],
        )
        if code != 0:
            rc = code

    if not args.skip_referral:
        code = _run_step(
            "referral drafts (startups / remote, grouped by company)",
            [py, str(ROOT / "referral_drafter.py"),
             "--startups", "--top", str(args.top)],
        )
        if code != 0:
            rc = code

    if not args.skip_dashboard:
        code = _run_step(
            "outreach dashboard",
            [py, str(ROOT / "outreach_log.py"), "--dashboard"],
        )
        if code != 0:
            rc = code

    prepare_outreach_sheet(quiet=True)

    o = outreach_stats()
    print(
        "\n  Outreach files updated:\n"
        f"    contacts.csv          — {o['contacts']} people\n"
        f"    cold_mail_drafts.md   — {o['cold_drafts']} email drafts\n"
        f"    referral_drafts.md    — {o['referral_drafts']} LinkedIn drafts\n"
        "  (Full checklist prints at the end of morning.py.)\n"
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
