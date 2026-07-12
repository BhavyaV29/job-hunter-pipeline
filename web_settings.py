"""Browser-managed config for a self-hosted instance.

API keys go to a managed env file loaded into ``os.environ``; the search profile
goes to an overlay merged over ``sources.yaml``. Both live under the tracker data
directory and are never committed. They survive restarts only when the host mounts
durable storage there (Docker's VOLUME declaration alone does not provide it).
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent

# Secret keys: blank in the form means "keep current" (we never echo secrets).
SECRET_FIELDS = [
    "RAPIDAPI_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "SERPAPI_KEY",
    "HUNTER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
]
# Plain (non-secret) fields: shown and set as-is.
PLAIN_FIELDS = ["LLM_PROVIDER", "GOOGLE_SHEETS_ID"]
_MANAGED = SECRET_FIELDS + PLAIN_FIELDS + ["GOOGLE_SERVICE_ACCOUNT_JSON", "ADMIN_TOKEN"]


def _data_dir() -> Path:
    tc = os.environ.get("TRACKER_CSV")
    return Path(tc).parent if tc else ROOT


def env_file() -> Path:
    return Path(os.environ.get("WEB_ENV_FILE", str(_data_dir() / "managed.env")))


def settings_file() -> Path:
    return Path(os.environ.get("WEB_SETTINGS_FILE", str(_data_dir() / "web_settings.yaml")))


# --- managed .env ------------------------------------------------------------
def _read_env(p: Path) -> dict:
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env(p: Path, data: dict) -> None:
    lines = []
    for k, v in data.items():
        v = "" if v is None else str(v)
        lines.append(f'{k}="{v}"' if (" " in v or "#" in v) else f"{k}={v}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def load_managed_env() -> None:
    """Apply the managed env file onto os.environ (call at startup)."""
    for k, v in _read_env(env_file()).items():
        if v != "":
            os.environ[k] = v


def save_keys(form: dict) -> None:
    """Persist submitted keys. Blank secret = keep current; plain fields set as-is."""
    data = _read_env(env_file())
    for k in SECRET_FIELDS:
        v = (form.get(k) or "").strip()
        if v:
            data[k] = v
            os.environ[k] = v
    for k in PLAIN_FIELDS:
        if k in form:
            v = (form.get(k) or "").strip()
            data[k] = v
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    _write_env(env_file(), data)


def save_service_account(value: str) -> None:
    """Accept a pasted JSON blob (written to a file) or a filesystem path."""
    value = (value or "").strip()
    if not value:
        return
    if value.startswith("{"):
        try:
            json.loads(value)
        except ValueError:
            return
        path = _data_dir() / "service_account.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        value = str(path)
    data = _read_env(env_file())
    data["GOOGLE_SERVICE_ACCOUNT_JSON"] = value
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = value
    _write_env(env_file(), data)


def ensure_admin_token() -> str:
    """Return the admin token, generating + persisting one when none is set."""
    tok = os.environ.get("ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    data = _read_env(env_file())
    tok = data.get("ADMIN_TOKEN", "").strip() or secrets.token_urlsafe(24)
    data["ADMIN_TOKEN"] = tok
    _write_env(env_file(), data)
    os.environ["ADMIN_TOKEN"] = tok
    return tok


# --- profile overlay ---------------------------------------------------------
def load_overlay() -> dict:
    p = settings_file()
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def save_profile(profile: dict) -> None:
    data = load_overlay()
    data["profile"] = {k: v for k, v in profile.items() if v not in (None, "")}
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    import profile_config
    profile_config.load_profile.cache_clear()
