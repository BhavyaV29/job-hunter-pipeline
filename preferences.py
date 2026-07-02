"""Load preferences.yaml for optional job filtering."""
from __future__ import annotations

from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).parent / "preferences.yaml"


def load_preferences(path: Path | None = None) -> dict:
    p = path or _DEFAULT_PATH
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def company_blocked(company: str, prefs: dict | None = None) -> bool:
    prefs = prefs if prefs is not None else load_preferences()
    co = (company or "").lower()
    for blocked in prefs.get("blocklist_companies") or []:
        if blocked.lower() in co:
            return True
    return False


def matches_must_have(role: str, description: str, prefs: dict | None = None) -> bool:
    prefs = prefs if prefs is not None else load_preferences()
    keywords = prefs.get("must_have_any") or []
    if not keywords:
        return True
    text = f"{role} {description}".lower()
    return any(k.lower() in text for k in keywords)
