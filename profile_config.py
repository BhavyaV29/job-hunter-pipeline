"""Per-person tuning read from the ``profile:`` block in sources.yaml.

Seniority, experience bands, stack keywords, geography and score weights differ
person to person; keeping them here means a new user retunes the search without
touching code. Every getter falls back to the original fresher / India-backend
defaults, so an absent block changes nothing. (Named ``profile_config`` to avoid
shadowing the stdlib ``profile`` module.)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_SOURCES_PATH = Path(__file__).parent / "sources.yaml"

# Defaults per seniority for the knobs that flip most between new-grad and senior:
# experience-match bands, the senior-title hard-drop, and the fetch YOE ceiling.
_SENIORITY_PRESETS: dict[str, dict] = {
    "fresher": {"exp_good_max": 1.0,  "exp_warn_max": 2.0,  "drop_senior_titles": True,  "max_exp_years": 2.0},
    "junior":  {"exp_good_max": 2.0,  "exp_warn_max": 4.0,  "drop_senior_titles": True,  "max_exp_years": 4.0},
    "mid":     {"exp_good_max": 5.0,  "exp_warn_max": 8.0,  "drop_senior_titles": False, "max_exp_years": None},
    "senior":  {"exp_good_max": 12.0, "exp_warn_max": 20.0, "drop_senior_titles": False, "max_exp_years": None},
}
_DEFAULT_SENIORITY = "fresher"


@lru_cache(maxsize=1)
def load_profile(path: str | None = None) -> dict:
    p = Path(path) if path else _SOURCES_PATH
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    prof = data.get("profile") or {}
    prof = dict(prof) if isinstance(prof, dict) else {}
    try:  # browser-managed overlay (web_settings) wins per key
        import web_settings
        overlay = web_settings.load_overlay().get("profile") or {}
        if isinstance(overlay, dict):
            prof.update(overlay)
    except Exception:
        pass
    return prof


def _preset(prof: dict) -> dict:
    sen = str(prof.get("seniority", _DEFAULT_SENIORITY)).strip().lower()
    return dict(_SENIORITY_PRESETS.get(sen, _SENIORITY_PRESETS[_DEFAULT_SENIORITY]))


def _num(prof: dict, key: str, fallback: float) -> float:
    try:
        return float(prof.get(key, fallback))
    except (TypeError, ValueError):
        return fallback


def experience_bands(prof: dict | None = None) -> tuple[float, float]:
    """(good_max, warn_max) YOE thresholds; explicit keys > preset > fresher."""
    prof = load_profile() if prof is None else prof
    base = _preset(prof)
    return _num(prof, "exp_good_max", base["exp_good_max"]), _num(prof, "exp_warn_max", base["exp_warn_max"])


def drop_senior_titles(prof: dict | None = None) -> bool:
    prof = load_profile() if prof is None else prof
    return bool(prof.get("drop_senior_titles", _preset(prof)["drop_senior_titles"]))


def max_exp_years(prof: dict | None = None, fallback: float = 2.0) -> float | None:
    """Fetch-time YOE ceiling; None means keep everything."""
    prof = load_profile() if prof is None else prof
    v = prof.get("max_exp_years", _preset(prof)["max_exp_years"])
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def weight_overrides(key: str) -> dict | None:
    """A dict override for a score-weight table (boost_keywords, etc.), or None."""
    v = load_profile().get(key)
    return v if isinstance(v, dict) else None


def scalar(key: str, default):
    v = load_profile().get(key)
    if v is None:
        return default
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default
