"""FastAPI smoke tests against the read-only demo (sample data)."""
import os

os.environ["DEMO_MODE"] = "1"  # serve tracker.sample.csv, block writes

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["demo"] is True


def test_dashboard_renders():
    r = client.get("/")
    assert r.status_code == 200
    assert "job" in r.text.lower()
    assert "DEMO" in r.text  # demo badge shown


def test_roles_partial_has_sample():
    r = client.get("/roles?view=all")
    assert r.status_code == 200
    assert "Razorpay" in r.text or "Vercel" in r.text


def test_api_roles_json():
    r = client.get("/api/roles?view=all")
    assert r.status_code == 200
    data = r.json()
    assert data and "tier" in data[0] and "score" in data[0]


def test_demo_write_blocked():
    r = client.post("/roles/stage",
                    data={"url": "https://demo.local/jobs/vercel-be", "stage": "applied"})
    assert r.status_code == 403


def test_demo_run_blocked():
    r = client.post("/run", data={"force": "true"})
    assert r.status_code == 403


def test_settings_page_renders_view_only():
    r = client.get("/settings")
    assert r.status_code == 200
    assert "API keys" in r.text and "Your profile" in r.text


def test_demo_settings_write_blocked():
    r = client.post("/settings/keys", data={"RAPIDAPI_KEY": "x"})
    assert r.status_code == 403
