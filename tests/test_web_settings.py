"""Browser-managed settings: managed .env, admin token, profile overlay."""
import json

import profile_config
import web_settings


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_ENV_FILE", str(tmp_path / "managed.env"))
    monkeypatch.setenv("WEB_SETTINGS_FILE", str(tmp_path / "web_settings.yaml"))
    for k in web_settings.SECRET_FIELDS + web_settings.PLAIN_FIELDS + ["ADMIN_TOKEN"]:
        monkeypatch.delenv(k, raising=False)


def test_save_and_load_keys(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    web_settings.save_keys({"RAPIDAPI_KEY": "abc123", "LLM_PROVIDER": "gemini"})

    import os
    assert os.environ["RAPIDAPI_KEY"] == "abc123"
    assert "RAPIDAPI_KEY=abc123" in (tmp_path / "managed.env").read_text()

    # blank secret keeps the current value; a fresh process picks it up via load
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    web_settings.load_managed_env()
    assert os.environ["RAPIDAPI_KEY"] == "abc123"

    web_settings.save_keys({"RAPIDAPI_KEY": ""})  # blank = keep
    assert os.environ["RAPIDAPI_KEY"] == "abc123"


def test_ensure_admin_token_is_stable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tok = web_settings.ensure_admin_token()
    assert tok and len(tok) > 16

    import os
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    web_settings.load_managed_env()
    assert web_settings.ensure_admin_token() == tok  # persisted, not regenerated


def test_save_service_account_json_blob(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TRACKER_CSV", str(tmp_path / "tracker.csv"))
    blob = json.dumps({"type": "service_account", "client_email": "x@y.iam"})
    web_settings.save_service_account(blob)

    import os
    saved = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    assert saved.endswith("service_account.json")
    assert json.loads(open(saved).read())["type"] == "service_account"


def test_profile_overlay_wins(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    profile_config.load_profile.cache_clear()
    web_settings.save_profile({"seniority": "senior", "exp_good_max": 12.0,
                               "drop_senior_titles": False})
    prof = profile_config.load_profile()
    assert prof["seniority"] == "senior"
    assert prof["exp_good_max"] == 12.0
    assert prof["drop_senior_titles"] is False
    profile_config.load_profile.cache_clear()
