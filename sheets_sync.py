# /// script
# requires-python = ">=3.10"
# dependencies = ["gspread>=5.12", "google-auth>=2.29"]
# ///
"""
Google Sheets sync engine for the job-search tracker.

Modes:
    uv run sheets_sync.py --push    CSV -> Sheets (overwrite Sheet with local tracker.csv)
    uv run sheets_sync.py --pull    Sheets -> CSV (overwrite local tracker.csv with Sheet data)
    uv run sheets_sync.py --sync    Smart two-way merge:
                                      Sheet wins for user-edited fields (stage, notes, dates, ...);
                                      CSV wins for pipeline fields (company, role, deadline, ...);
                                      New CSV rows appended to Sheet;
                                      Sheet-only rows (manually added) kept in both.
    uv run sheets_sync.py --status  Show row counts, schema match, last backup (no writes).

All modes skip gracefully with a clear message if GOOGLE_SHEETS_ID or
GOOGLE_SERVICE_ACCOUNT_JSON are not set in the environment, so the rest of
the pipeline keeps working unmodified.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

TRACKER = Path(__file__).parent / "tracker.csv"
TRACKER_BAK = Path(__file__).parent / "tracker.csv.bak"
SHEET_TAB = "tracker"

# Canonical 28-column schema (mirrors fetch_jobs.py FIELDS exactly).
# stage and url sit right after score so the most actionable columns are
# visible without horizontal scrolling.
FIELDS: list[str] = [
    "date_found", "posted_date", "company", "score", "stage", "url", "role", "location",
    "salary", "deadline", "source", "applied_date", "contact_name",
    "contact_email", "job_id", "resume_variant", "referral_contact", "oa_date",
    "phone_date", "tech_date", "onsite_date", "offer_details", "next_action",
    "next_action_date", "notes", "exp_years", "exp_match", "link_status",
]

# Sheet version wins for these fields during --sync (user edits these in the Sheet)
USER_FIELDS: frozenset[str] = frozenset({
    "stage", "applied_date", "contact_name", "contact_email", "job_id",
    "resume_variant", "referral_contact", "oa_date", "phone_date", "tech_date",
    "onsite_date", "offer_details", "next_action", "next_action_date", "notes",
})

# CSV version wins for these fields during --sync (pipeline populates them)
PIPELINE_FIELDS: frozenset[str] = frozenset({
    "date_found", "posted_date", "company", "role", "location", "salary",
    "source", "deadline", "score", "exp_years", "exp_match", "link_status",
})

STAGE_VALUES: list[str] = [
    "sourced", "new", "shortlisted", "applied", "not_applicable",
    "oa", "phone_screen", "tech_screen", "onsite", "offer",
    "rejected", "withdrawn",
    # legacy aliases kept for existing Sheet rows
    "phone", "tech", "closed",
]

OUTREACH_COLUMNS: list[str] = [
    "date_logged", "company", "person_name", "channel", "role", "job_id",
    "message_type", "sent_date", "status", "follow_up_due", "last_follow_up", "notes",
]

OUTREACH_CHANNEL_VALUES = ["linkedin", "email", "twitter"]
OUTREACH_TYPE_VALUES = ["cold", "referral"]
OUTREACH_STATUS_VALUES = [
    "drafted", "sent", "replied", "referred", "no_response", "closed",
]


def _safe_int(value, default: int = 0) -> int:
    """Parse a possibly-blank/non-numeric 'score' cell into an int without ever
    raising (a manually-edited or empty Sheet cell must not crash the push)."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Environment / auth
# ---------------------------------------------------------------------------

def _env_check() -> tuple[str, str] | None:
    """Return (sheet_id, sa_json_path) if both env vars are set, else print and return None."""
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    missing = [name for name, val in [
        ("GOOGLE_SHEETS_ID", sheet_id),
        ("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json),
    ] if not val]
    if missing:
        print(f"{' / '.join(missing)} not set — skipping.")
        return None
    return sheet_id, sa_json


def _open_sheet(sheet_id: str, sa_json_path: str) -> tuple | None:
    """Return (spreadsheet, worksheet) or None on failure (prints reason)."""
    import gspread
    from google.oauth2.service_account import Credentials

    if not Path(sa_json_path).exists():
        print(f"Service account JSON key not found: {sa_json_path}")
        return None

    creds = Credentials.from_service_account_file(
        sa_json_path,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)

    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except gspread.SpreadsheetNotFound:
        print(
            f"Spreadsheet not found (GOOGLE_SHEETS_ID={sheet_id!r}).\n"
            "Check that the ID is correct and that the service account has Editor access.\n"
            "Tip: the Sheet ID is the long string between /d/ and /edit in the URL."
        )
        return None
    except Exception as exc:
        print(f"Could not open spreadsheet: {exc}")
        return None

    try:
        worksheet = spreadsheet.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=SHEET_TAB, rows=5000, cols=30)
        print(f"Created new worksheet tab '{SHEET_TAB}'.")

    return spreadsheet, worksheet


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _load_csv() -> tuple[list[dict], list[str]]:
    """Return (rows, fieldnames) from tracker.csv."""
    if not TRACKER.exists():
        return [], list(FIELDS)
    with TRACKER.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or FIELDS)
        rows = list(reader)
    return rows, fieldnames


def _backup_csv() -> None:
    """Copy tracker.csv to tracker.csv.bak (silently skips if file absent)."""
    if TRACKER.exists():
        shutil.copy2(TRACKER, TRACKER_BAK)


def _save_csv(rows: list[dict], fieldnames: list[str]) -> None:
    """Atomically write rows to tracker.csv, backing up first."""
    _backup_csv()
    tmp = TRACKER.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(TRACKER)


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _read_sheet(worksheet) -> tuple[list[dict], list[str]]:
    """Return (rows, fieldnames) from the worksheet. Skips fully-blank rows."""
    all_values = worksheet.get_all_values()
    if not all_values or not all_values[0]:
        return [], []
    header = all_values[0]
    rows = [
        {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
        for row in all_values[1:]
        if any(cell.strip() for cell in row)
    ]
    return rows, header


def _write_sheet(spreadsheet, worksheet, rows: list[dict], fieldnames: list[str]) -> None:
    """Clear the worksheet and write header + all rows.

    USER_ENTERED input option lets Google Sheets auto-format dates and numbers
    (e.g. 'YYYY-MM-DD' strings become proper date values, sortable in the Sheet).

    After writing, trims the sheet to data rows + 100 spare rows. This prevents
    the Google Sheets UI from showing ~5,000 "rows" when the sheet was originally
    created with rows=5000 capacity but only has ~300-400 real data rows.
    """
    worksheet.clear()
    data: list[list[str]] = [fieldnames]
    for row in rows:
        data.append([str(row.get(f, "") or "") for f in fieldnames])
    worksheet.batch_update(
        [{"range": "A1", "values": data}],
        value_input_option="USER_ENTERED",
    )
    # Resize sheet to data rows + 100 buffer so the Sheet UI shows the real row
    # count instead of the pre-allocated 5000-row capacity.
    target_rows = max(len(data) + 100, 200)
    try:
        spreadsheet.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {"rowCount": target_rows},
                },
                "fields": "gridProperties.rowCount",
            }
        }]})
    except Exception:
        pass  # Non-fatal; resize is cosmetic


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


def _col_letter(idx: int) -> str:
    """Convert 0-based column index to a Sheets column letter (A, B, ..., Z, AA, ...)."""
    letters = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _apply_formatting(spreadsheet, worksheet, fieldnames: list[str]) -> None:
    """Apply header style, column widths, freeze, and conditional formatting.

    Called after every --push and --sync to keep the Sheet looking clean.
    Deletes existing conditional format rules before adding new ones to prevent
    accumulation on repeated calls.
    """
    sheet_id = worksheet.id
    n_cols = len(fieldnames)
    col_idx = {name: i for i, name in enumerate(fieldnames)}
    requests: list[dict] = []

    # Remove existing conditional format rules to avoid accumulation
    try:
        raw = spreadsheet.client.request(
            "get",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet.id}",
            params={"fields": "sheets(properties.sheetId,conditionalFormats)"},
        ).json()
        for s in raw.get("sheets", []):
            if s.get("properties", {}).get("sheetId") == sheet_id:
                n_existing = len(s.get("conditionalFormats", []))
                for i in range(n_existing - 1, -1, -1):
                    requests.append({
                        "deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}
                    })
                break
    except Exception:
        pass  # Non-fatal; may accumulate duplicate rules on repeated calls

    # Header: bold, dark navy (#1a1a2e background), white text
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": n_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": _hex_to_rgb("#1a1a2e"),
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    },
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })

    # Freeze row 1 (header) and column 1 (date_found)
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1},
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    })

    # Column widths (pixels); unlisted columns get the default auto width
    for col_name, px in [
        ("company", 180), ("role", 250), ("location", 150), ("salary", 120),
        ("stage", 100), ("deadline", 100), ("applied_date", 110), ("next_action", 200),
    ]:
        if col_name in col_idx:
            i = col_idx[col_name]
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id, "dimension": "COLUMNS",
                        "startIndex": i, "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            })

    # Conditional formatting: stage column colour-codes funnel position
    stage_col = col_idx.get("stage")
    if stage_col is not None:
        stage_range = {
            "sheetId": sheet_id, "startRowIndex": 1,
            "startColumnIndex": stage_col, "endColumnIndex": stage_col + 1,
        }
        for stage_values, bg_hex in [
            (["sourced", "new"],                          "#cfe2ff"),  # light blue
            (["shortlisted"],                             "#e2d9f3"),  # light purple
            (["applied"],                                 "#fff3cd"),  # light yellow
            (["oa", "phone_screen", "tech_screen", "onsite",
              "phone", "tech"],                           "#ffd5a8"),  # light orange
            (["offer"],                                   "#d1e7dd"),  # light green
            (["rejected", "withdrawn", "not_applicable", "closed"], "#e9ecef"),
        ]:
            for sv in stage_values:
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [stage_range],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_EQ",
                                    "values": [{"userEnteredValue": sv}],
                                },
                                "format": {"backgroundColor": _hex_to_rgb(bg_hex)},
                            },
                        },
                        "index": 0,
                    }
                })

    # Conditional formatting: deadline within 7 days of today → red
    deadline_col = col_idx.get("deadline")
    if deadline_col is not None:
        dl_letter = _col_letter(deadline_col)
        dl_full_range = {
            "sheetId": sheet_id, "startRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": n_cols,
        }
        # Closing soon (1-3 days): orange row tint
        formula_closing = (
            f"=AND(NOT(ISBLANK({dl_letter}2)),"
            f"{dl_letter}2>=TODAY(),"
            f"{dl_letter}2<=TODAY()+3)"
        )
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [dl_full_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula_closing}],
                        },
                        "format": {"backgroundColor": _hex_to_rgb("#ffe5b4")},
                    },
                },
                "index": 0,
            }
        })
        # Past deadline: red row tint
        formula_past = (
            f"=AND(NOT(ISBLANK({dl_letter}2)),{dl_letter}2<TODAY())"
        )
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [dl_full_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula_past}],
                        },
                        "format": {"backgroundColor": _hex_to_rgb("#f8d7da")},
                    },
                },
                "index": 0,
            }
        })
        # Deadline within 7 days (column only): light red
        formula = (
            f"=AND(NOT(ISBLANK({dl_letter}2)),"
            f"{dl_letter}2>=TODAY(),"
            f"{dl_letter}2<=TODAY()+7)"
        )
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id, "startRowIndex": 1,
                        "startColumnIndex": deadline_col,
                        "endColumnIndex": deadline_col + 1,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula}],
                        },
                        "format": {"backgroundColor": _hex_to_rgb("#f8d7da")},
                    },
                },
                "index": 0,
            }
        })

    # NEW jobs: date_found = today → green row tint
    date_col = col_idx.get("date_found")
    if date_col is not None:
        df_letter = _col_letter(date_col)
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id, "startRowIndex": 1,
                        "startColumnIndex": 0, "endColumnIndex": n_cols,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f"=${df_letter}2=TODAY()"}],
                        },
                        "format": {"backgroundColor": _hex_to_rgb("#d4edda")},
                    },
                },
                "index": 0,
            }
        })

    # Experience mismatch: exp_match = bad → light red text on row
    exp_col = col_idx.get("exp_match")
    if exp_col is not None:
        em_letter = _col_letter(exp_col)
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id, "startRowIndex": 1,
                        "startColumnIndex": 0, "endColumnIndex": n_cols,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f'=${em_letter}2="bad"'}],
                        },
                        "format": {
                            "textFormat": {
                                "foregroundColor": _hex_to_rgb("#842029"),
                            },
                        },
                    },
                },
                "index": 0,
            }
        })

    if requests:
        spreadsheet.batch_update({"requests": requests})

    stage_col = col_idx.get("stage")
    if stage_col is not None:
        _add_stage_dropdown(spreadsheet, worksheet, stage_col)


def _add_stage_dropdown(spreadsheet, worksheet, stage_col: int) -> None:
    """Apply a ONE_OF_LIST dropdown validation to the stage column (rows 2–2000)."""
    _add_column_dropdown(spreadsheet, worksheet, stage_col, STAGE_VALUES)


def _add_column_dropdown(
    spreadsheet, worksheet, col: int, values: list[str]
) -> None:
    """ONE_OF_LIST dropdown for rows 2–2000 on one column."""
    sheet_id = worksheet.id
    spreadsheet.batch_update({"requests": [{
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 2000,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }]})


# ---------------------------------------------------------------------------
# Outreach tab
# ---------------------------------------------------------------------------

def _apply_outreach_formatting(spreadsheet, worksheet) -> None:
    """Apply header style, column widths, freeze, and conditional formatting to outreach tab."""
    sheet_id = worksheet.id
    n_cols = len(OUTREACH_COLUMNS)
    col_idx = {name: i for i, name in enumerate(OUTREACH_COLUMNS)}
    requests: list[dict] = []

    # Remove existing conditional format rules to avoid accumulation
    try:
        raw = spreadsheet.client.request(
            "get",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet.id}",
            params={"fields": "sheets(properties.sheetId,conditionalFormats)"},
        ).json()
        for s in raw.get("sheets", []):
            if s.get("properties", {}).get("sheetId") == sheet_id:
                n_existing = len(s.get("conditionalFormats", []))
                for i in range(n_existing - 1, -1, -1):
                    requests.append({
                        "deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}
                    })
                break
    except Exception:
        pass

    # Header: bold, dark navy (#1a1a2e), white text
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": n_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": _hex_to_rgb("#1a1a2e"),
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    },
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    })

    # Freeze row 1 (header); no column freeze for outreach
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 0},
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    })

    # Column widths (pixels)
    for col_name, px in [
        ("company", 180), ("person_name", 150), ("channel", 100),
        ("role", 200), ("job_id", 100), ("message_type", 110),
        ("sent_date", 110), ("status", 120), ("follow_up_due", 120),
        ("last_follow_up", 120), ("notes", 300),
    ]:
        if col_name in col_idx:
            i = col_idx[col_name]
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id, "dimension": "COLUMNS",
                        "startIndex": i, "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            })

    # Conditional formatting: follow_up_due (col J, index 9)
    fud_col = col_idx.get("follow_up_due")
    status_col = col_idx.get("status")
    if fud_col is not None and status_col is not None:
        fud_letter = _col_letter(fud_col)
        status_letter = _col_letter(status_col)
        fud_range = {
            "sheetId": sheet_id, "startRowIndex": 1,
            "startColumnIndex": fud_col, "endColumnIndex": fud_col + 1,
        }
        # Overdue: follow_up_due < today AND status="sent" → red
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [fud_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": (
                                f"=AND(NOT(ISBLANK({fud_letter}2)),"
                                f"{fud_letter}2<TODAY(),"
                                f"{status_letter}2=\"sent\")"
                            )}],
                        },
                        "format": {"backgroundColor": _hex_to_rgb("#f8d7da")},
                    },
                },
                "index": 0,
            }
        })
        # Due today: follow_up_due = today AND status="sent" → yellow
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [fud_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": (
                                f"=AND(NOT(ISBLANK({fud_letter}2)),"
                                f"{fud_letter}2=TODAY(),"
                                f"{status_letter}2=\"sent\")"
                            )}],
                        },
                        "format": {"backgroundColor": _hex_to_rgb("#fff3cd")},
                    },
                },
                "index": 0,
            }
        })

    # Conditional formatting: status (col I, index 8)
    if status_col is not None:
        status_range = {
            "sheetId": sheet_id, "startRowIndex": 1,
            "startColumnIndex": status_col, "endColumnIndex": status_col + 1,
        }
        for sv_list, bg_hex in [
            (["replied", "referred"], "#d1e7dd"),   # green
            (["closed", "no_response"], "#e9ecef"),  # grey
            (["sent"], "#cfe2ff"),                   # blue
        ]:
            for sv in sv_list:
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [status_range],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_EQ",
                                    "values": [{"userEnteredValue": sv}],
                                },
                                "format": {"backgroundColor": _hex_to_rgb(bg_hex)},
                            },
                        },
                        "index": 0,
                    }
                })

    if requests:
        spreadsheet.batch_update({"requests": requests})

    for col_name, values in [
        ("channel", OUTREACH_CHANNEL_VALUES),
        ("message_type", OUTREACH_TYPE_VALUES),
        ("status", OUTREACH_STATUS_VALUES),
    ]:
        if col_name in col_idx:
            _add_column_dropdown(spreadsheet, worksheet, col_idx[col_name], values)


def _init_outreach_tab(spreadsheet) -> object:
    """Get or create the 'outreach' worksheet with headers and formatting.

    Returns the worksheet. Writes headers + formatting only on first use
    (when the tab doesn't exist or is empty).
    """
    import gspread

    try:
        ws = spreadsheet.worksheet("outreach")
        existing = ws.get_all_values()
        if not existing or not any(c.strip() for c in existing[0]):
            ws.batch_update(
                [{"range": "A1", "values": [OUTREACH_COLUMNS]}],
                value_input_option="USER_ENTERED",
            )
            _apply_outreach_formatting(spreadsheet, ws)
            print("Initialized empty 'outreach' worksheet with headers and formatting.")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title="outreach", rows=2000, cols=len(OUTREACH_COLUMNS) + 2
        )
        ws.batch_update(
            [{"range": "A1", "values": [OUTREACH_COLUMNS]}],
            value_input_option="USER_ENTERED",
        )
        _apply_outreach_formatting(spreadsheet, ws)
        print("Created 'outreach' worksheet tab with headers and formatting.")

    return ws


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_push() -> None:
    """CSV -> Sheets: clear the Sheet and replace with local tracker.csv."""
    result = _env_check()
    if result is None:
        return
    sheet_id, sa_json = result

    rows, _ = _load_csv()
    if not rows:
        print("tracker.csv is empty or not found — nothing to push.")
        return

    # Pre-sort by score descending so Sheet arrives in ranked order. Score is
    # parsed defensively so a blank/non-numeric cell can never crash the push.
    rows.sort(key=lambda r: _safe_int(r.get("score")), reverse=True)

    # not_applicable rows are kept in CSV as a permanent dedup blacklist; they
    # are excluded from the Sheet push so they don't clutter the working view.
    na_count = sum(1 for r in rows if r.get("stage", "") == "not_applicable")
    rows = [r for r in rows if r.get("stage", "") != "not_applicable"]
    if na_count:
        print(f"  (excluded {na_count} not_applicable rows from Sheet)")

    opened = _open_sheet(sheet_id, sa_json)
    if opened is None:
        sys.exit(1)
    spreadsheet, worksheet = opened

    print(f"Pushing {len(rows)} rows to Google Sheets...")
    _write_sheet(spreadsheet, worksheet, rows, list(FIELDS))
    _apply_formatting(spreadsheet, worksheet, list(FIELDS))
    print(f"Done. Sheet updated with {len(rows)} data rows + formatted header.")


def _merge_not_applicable_blacklist(
    sheet_rows: list[dict],
    prior_rows: list[dict],
) -> tuple[list[dict], int]:
    """Re-attach CSV-only ``not_applicable`` rows after a Sheet pull.

    ``cmd_push`` hides ``not_applicable`` from the Sheet (permanent dedup
    blacklist stays in CSV only). A naive pull would wipe that blacklist and
    let dismissed jobs reappear on the next fetch. Preserve prior CSV rows
    whose stage is ``not_applicable`` and whose URL is not already on the Sheet.
    """
    sheet_urls = {
        (r.get("url") or "").strip()
        for r in sheet_rows
        if (r.get("url") or "").strip()
    }
    kept: list[dict] = []
    for row in prior_rows:
        if (row.get("stage") or "").strip() != "not_applicable":
            continue
        url = (row.get("url") or "").strip()
        if not url or url in sheet_urls:
            continue
        kept.append(row)
        sheet_urls.add(url)
    return sheet_rows + kept, len(kept)


def cmd_pull() -> None:
    """Sheets -> CSV: overwrite local tracker.csv with Sheet data.

    Preserves CSV-only ``not_applicable`` blacklist rows that were intentionally
    excluded from the Sheet on the previous push.
    """
    result = _env_check()
    if result is None:
        return
    sheet_id, sa_json = result

    opened = _open_sheet(sheet_id, sa_json)
    if opened is None:
        sys.exit(1)
    _, worksheet = opened

    sheet_rows, sheet_fields = _read_sheet(worksheet)
    if not sheet_fields:
        print("Sheet is empty or has no header row — nothing to pull.")
        return

    expected = set(FIELDS)
    actual = set(sheet_fields)
    if expected - actual:
        print(f"WARNING: Sheet missing expected columns: {sorted(expected - actual)}")
    if actual - expected:
        print(f"WARNING: Sheet has extra columns not in local schema: {sorted(actual - expected)}")

    prior_rows, _ = _load_csv()
    merged_rows, na_kept = _merge_not_applicable_blacklist(sheet_rows, prior_rows)

    # Write the CSV in the canonical FIELDS order (by NAME), NOT the raw Sheet
    # column order — a manual column reorder in the Sheet must never reshuffle the
    # CSV (that would misalign fetch_jobs.py's append). Any extra Sheet-only
    # columns are preserved at the end so no user data is lost.
    out_fields = list(FIELDS) + [f for f in sheet_fields if f not in FIELDS]
    _save_csv(merged_rows, out_fields)
    na_note = (
        f"\n  (preserved {na_kept} not_applicable blacklist row(s) from prior CSV)"
        if na_kept else ""
    )
    print(
        f"Pulled {len(sheet_rows)} rows from Sheet -> tracker.csv"
        f" ({len(merged_rows)} after blacklist merge)\n"
        f"  (previous tracker.csv backed up to tracker.csv.bak)"
        f"{na_note}"
    )


def cmd_sync() -> None:
    """Smart two-way merge.

    Merge rules (per row, keyed by URL):
      Both sides: PIPELINE_FIELDS come from CSV; USER_FIELDS come from Sheet.
      CSV-only (new fetched roles): appended to Sheet as-is.
      Sheet-only (manually added rows): kept in both CSV and Sheet.
    """
    result = _env_check()
    if result is None:
        return
    sheet_id, sa_json = result

    opened = _open_sheet(sheet_id, sa_json)
    if opened is None:
        sys.exit(1)
    spreadsheet, worksheet = opened

    csv_rows, csv_fields = _load_csv()
    sheet_rows, _ = _read_sheet(worksheet)

    sheet_by_url: dict[str, dict] = {}
    for row in sheet_rows:
        url = (row.get("url") or "").strip()
        if url:
            sheet_by_url[url] = row

    merged: list[dict] = []
    merged_urls: set[str] = set()

    for row in csv_rows:
        url = (row.get("url") or "").strip()
        if url and url in sheet_by_url:
            sheet_row = sheet_by_url[url]
            merged_row: dict[str, str] = {}
            for f in csv_fields:
                if f in PIPELINE_FIELDS:
                    merged_row[f] = row.get(f, "")
                else:
                    # User field: prefer Sheet value; fall back to CSV if Sheet is empty
                    merged_row[f] = sheet_row.get(f, "") or row.get(f, "")
        else:
            merged_row = {f: row.get(f, "") for f in csv_fields}
        merged.append(merged_row)
        if url:
            merged_urls.add(url)

    sheet_only_count = 0
    for row in sheet_rows:
        url = (row.get("url") or "").strip()
        if url and url not in merged_urls:
            merged.append({f: row.get(f, "") for f in csv_fields})
            merged_urls.add(url)
            sheet_only_count += 1

    _save_csv(merged, csv_fields)
    _write_sheet(spreadsheet, worksheet, merged, csv_fields)
    _apply_formatting(spreadsheet, worksheet, csv_fields)

    print(
        f"Sync complete. {len(merged)} total rows.\n"
        f"  CSV: {len(csv_rows)} rows  |  Sheet: {len(sheet_rows)} rows  "
        f"|  Sheet-only rows kept: {sheet_only_count}\n"
        f"  Both tracker.csv and Sheet updated. Backup: tracker.csv.bak"
    )


def cmd_status() -> None:
    """Print row counts, schema match, and backup age. Makes no writes."""
    result = _env_check()
    if result is None:
        return
    sheet_id, sa_json = result

    csv_rows, csv_fields = _load_csv()
    csv_mtime = (
        dt.datetime.fromtimestamp(TRACKER.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if TRACKER.exists() else "not found"
    )
    bak_mtime = (
        dt.datetime.fromtimestamp(TRACKER_BAK.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if TRACKER_BAK.exists() else "none"
    )

    print(f"\nJob Tracker — Google Sheets Sync Status")
    print("=" * 45)
    print(f"  tracker.csv:      {len(csv_rows):>5} rows  (last modified: {csv_mtime})")
    print(f"  tracker.csv.bak:  last backup: {bak_mtime}")

    opened = _open_sheet(sheet_id, sa_json)
    if opened is None:
        print("  Google Sheet:     could not connect (check env vars / service account)")
    else:
        _, worksheet = opened
        sheet_rows, sheet_fields = _read_sheet(worksheet)
        schema_ok = set(sheet_fields) == set(FIELDS)
        schema_tag = "OK" if schema_ok else f"MISMATCH ({len(sheet_fields)} cols vs {len(FIELDS)} expected)"
        print(f"  Google Sheet:     {len(sheet_rows):>5} rows  schema: {schema_tag}")
        if not schema_ok:
            missing = sorted(set(FIELDS) - set(sheet_fields))
            extra = sorted(set(sheet_fields) - set(FIELDS))
            if missing:
                print(f"    Missing in Sheet: {missing}")
            if extra:
                print(f"    Extra in Sheet:   {extra}")

    print()


def cmd_init_outreach() -> None:
    """Create and format the outreach worksheet tab (idempotent)."""
    result = _env_check()
    if result is None:
        return
    sheet_id, sa_json = result

    opened = _open_sheet(sheet_id, sa_json)
    if opened is None:
        sys.exit(1)
    spreadsheet, _ = opened

    ws = _init_outreach_tab(spreadsheet)
    print(f"Outreach tab '{ws.title}' is ready (spreadsheet ID={sheet_id}).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--push", action="store_true",
                     help="CSV -> Sheets: overwrite Sheet with local tracker.csv")
    grp.add_argument("--pull", action="store_true",
                     help="Sheets -> CSV: overwrite local tracker.csv with Sheet data")
    grp.add_argument("--sync", action="store_true",
                     help="Smart two-way merge (Sheet wins for user fields, CSV for pipeline fields)")
    grp.add_argument("--status", action="store_true",
                     help="Show row counts, schema match, backup age (no writes)")
    grp.add_argument("--init-outreach", action="store_true",
                     help="Create/format the outreach worksheet tab (idempotent)")
    args = ap.parse_args()

    if args.push:
        cmd_push()
    elif args.pull:
        cmd_pull()
    elif args.sync:
        cmd_sync()
    elif args.status:
        cmd_status()
    elif args.init_outreach:
        cmd_init_outreach()


if __name__ == "__main__":
    main()
