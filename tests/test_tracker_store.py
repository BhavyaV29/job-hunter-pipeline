"""Store behaviour: read/sort/filter, stage updates, funnel stats, demo guard."""
import csv

import pytest

import tracker_store as store


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=store.FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in store.FIELDS})


def _seed(tmp_path):
    p = tmp_path / "tracker.csv"
    _write(p, [
        {"company": "Acme", "role": "Backend Engineer", "stage": "sourced",
         "url": "u1", "score": "900", "salary": "₹20 LPA", "location": "Bengaluru",
         "exp_match": "good"},
        {"company": "Beta", "role": "ML Engineer", "stage": "sourced",
         "url": "u2", "score": "500", "location": "Remote (India eligible)"},
        {"company": "Gamma", "role": "SDE 1", "stage": "applied",
         "url": "u3", "score": "700", "location": "Hyderabad"},
    ])
    return p


def test_list_sort_and_filter(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    monkeypatch.setenv("TRACKER_CSV", str(p))
    monkeypatch.delenv("DEMO_MODE", raising=False)

    assert store.is_demo() is False
    ranked = store.list_roles(sort="score")
    assert [r["company"] for r in ranked] == ["Acme", "Gamma", "Beta"]

    triage = store.list_roles(triage_only=True)
    assert {r["company"] for r in triage} == {"Acme", "Beta"}

    assert [r["company"] for r in store.list_roles(query="gamma")] == ["Gamma"]


def test_stats_funnel(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    monkeypatch.setenv("TRACKER_CSV", str(p))
    monkeypatch.delenv("DEMO_MODE", raising=False)

    s = store.stats()
    assert s["total"] == 3
    assert s["triage"] == 2
    assert dict(s["funnel"]).get("applied") == 1


def test_update_stage_persists(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    monkeypatch.setenv("TRACKER_CSV", str(p))
    monkeypatch.delenv("DEMO_MODE", raising=False)

    assert store.update_stage("u1", "applied") is True
    assert store.update_stage("does-not-exist", "applied") is False

    _, rows = store.read_rows(p)
    stages = {r["url"]: r["stage"] for r in rows}
    assert stages["u1"] == "applied"
    # header/schema preserved on write-back
    fieldnames, _ = store.read_rows(p)
    assert fieldnames == store.FIELDS


def test_shortlisted_is_triage_and_invalid_stage_is_rejected(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    monkeypatch.setenv("TRACKER_CSV", str(p))
    monkeypatch.delenv("DEMO_MODE", raising=False)

    assert store.update_stage("u1", "shortlisted") is True
    assert any(r["url"] == "u1" for r in store.list_roles(triage_only=True))
    with pytest.raises(store.InvalidStageError):
        store.update_stage("u1", "invented-stage")


def test_demo_is_read_only(tmp_path, monkeypatch):
    p = _seed(tmp_path)
    monkeypatch.setenv("TRACKER_CSV", str(p))
    monkeypatch.setenv("DEMO_MODE", "1")

    assert store.is_demo() is True
    try:
        store.update_stage("u1", "applied")
        assert False, "expected ReadOnlyError"
    except store.ReadOnlyError:
        pass
