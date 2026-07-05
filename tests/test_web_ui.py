"""Live-mode UI behaviour: empty state, HTML escaping, deadline cues, token gating.

Uses monkeypatch to flip into live mode per-test (test_web.py runs in demo mode),
and a fresh TestClient per test so the lifespan startup + dynamic env are honoured.
"""
import csv
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
import tracker_store as store  # noqa: E402

FIELDS = ["date_found", "company", "score", "stage", "url", "role",
          "location", "salary", "deadline", "exp_match"]


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("ADMIN_TOKEN", "tkn-abc-123")
    monkeypatch.setenv("WEB_ENV_FILE", str(tmp_path / "managed.env"))
    monkeypatch.setenv("WEB_SETTINGS_FILE", str(tmp_path / "web_settings.yaml"))
    monkeypatch.setenv("TRACKER_CSV", str(tmp_path / "tracker.csv"))
    return tmp_path


def test_live_empty_state(live):
    with TestClient(server.app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "No roles yet" in r.text and "/settings" in r.text
        assert c.get("/healthz").json()["demo"] is False


def test_live_hides_recruiter_banner(live):
    # The "Live demo / sample data" banner is for the public showcase only;
    # a real user's live instance must never see it.
    with TestClient(server.app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "github.com/BhavyaV29/job-hunter-pipeline" not in r.text
        assert "Live demo" not in r.text


def test_escaping_and_deadline_cues(live):
    today = date.today()
    _write(live / "tracker.csv", [
        {"date_found": today.isoformat(), "company": "<script>x</script>", "score": "900",
         "stage": "sourced", "url": "https://e/1", "role": "Backend Engineer",
         "location": "Remote (India eligible)", "salary": "20 LPA",
         "deadline": (today + timedelta(days=3)).isoformat(), "exp_match": "good"},
        {"date_found": "2020-01-01", "company": "OldCorp", "score": "500", "stage": "sourced",
         "url": "https://e/2", "role": "SDE 1", "location": "Bengaluru", "salary": "15 LPA",
         "deadline": (today - timedelta(days=2)).isoformat()},
    ])
    with TestClient(server.app) as c:
        r = c.get("/?view=all")
        assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text
        assert "(3d)" in r.text and "line-through" in r.text
        assert "found today" in r.text


def test_token_gating_and_persist(live):
    _write(live / "tracker.csv", [
        {"date_found": "2024-01-01", "company": "StageCo", "score": "300",
         "stage": "sourced", "url": "https://e/3", "role": "Engineer", "location": "Bengaluru"}])
    with TestClient(server.app) as c:  # no token
        assert c.post("/roles/stage", data={"url": "https://e/3", "stage": "applied"}).status_code == 401
    with TestClient(server.app) as c:  # unlock via ?token= -> cookie
        c.get("/?token=tkn-abc-123")
        r = c.post("/roles/stage", data={"url": "https://e/3", "stage": "applied", "view": "all"})
        assert r.status_code == 200
        rows = store.read_rows(live / "tracker.csv")[1]
        assert any(x["url"] == "https://e/3" and x["stage"] == "applied" for x in rows)
