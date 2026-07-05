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


def test_demo_sample_urls_are_real_and_clickable():
    r = client.get("/roles?view=all")
    assert r.status_code == 200
    assert "demo.local" not in r.text            # no placeholder links
    assert "https://vercel.com/careers" in r.text  # a real, clickable posting
    assert ">open" in r.text                      # rendered as a link, not the "demo" fallback


def test_demo_recruiter_banner():
    r = client.get("/")
    assert r.status_code == 200
    assert "github.com/BhavyaV29/job-hunter-pipeline" in r.text
    assert "Live demo" in r.text and "Deploy your own" in r.text


def test_api_roles_json():
    r = client.get("/api/roles?view=all")
    assert r.status_code == 200
    data = r.json()
    assert data and "tier" in data[0] and "score" in data[0]


def test_demo_write_blocked():
    r = client.post("/roles/stage",
                    data={"url": "https://vercel.com/careers", "stage": "applied"})
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
