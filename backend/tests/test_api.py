"""FastAPI integration tests via TestClient (item 1 hardening + smoke)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

EXAMPLE_STEP = Path(__file__).resolve().parents[2] / "examples" / "synthetic-sig-strip.stp"


def test_index_ok():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_evaluate_with_bundled_step_returns_report():
    data = EXAMPLE_STEP.read_bytes()
    r = client.post(
        "/api/evaluate",
        files={"step_file": ("synthetic-sig-strip.stp", data, "application/octet-stream")},
        data={"family": "stamping"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["family"] == "stamping"
    assert "summary" in body and body["summary"]["results"]


@pytest.mark.parametrize("filename", ["notes.exe", "archive.zip", "noext"])
def test_upload_rejects_bad_extension(filename):
    r = client.post(
        "/api/evaluate",
        files={"step_file": (filename, b"junk", "application/octet-stream")},
        data={"family": "stamping"},
    )
    assert r.status_code == 400


def test_upload_rejects_path_traversal():
    r = client.post(
        "/api/evaluate",
        files={"step_file": ("../../evil.stp", b"junk", "application/octet-stream")},
        data={"family": "stamping"},
    )
    assert r.status_code == 400
    assert "path" in r.json()["detail"].lower()


def test_diff_bad_versions_returns_404():
    r = client.get("/api/criteria/diff", params={"a": 999, "b": 1000})
    assert r.status_code == 404


def test_ctf_rejects_malformed_payload():
    # No balloon_id and no rule_id/supplier/parameter identifier -> rejected.
    r = client.post("/api/ctf", json={"foo": "bar"})
    assert r.status_code == 400


def test_ctf_accepts_supplier_capability_entry():
    r = client.post(
        "/api/ctf",
        json={"rule_id": "STMP-BURR-GEN", "supplier": "Hoky", "achieved_min": 0.02,
              "confirmed": False, "context": "unit test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["kinds"] == ["supplier_capability"]
