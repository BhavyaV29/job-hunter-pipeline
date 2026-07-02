"""Optional preferences-based filter. Enable with PREFERENCES_FILTER=1 in fetch."""
from __future__ import annotations

from preferences import company_blocked, load_preferences, matches_must_have


def filter_job(company: str, role: str, description: str = "") -> tuple[bool, str]:
    """Return (keep, reason). Empty reason when keep=True."""
    prefs = load_preferences()
    if company_blocked(company, prefs):
        return False, "preferences_blocklist"
    if not matches_must_have(role, description, prefs):
        return False, "preferences_keywords"
    return True, ""
