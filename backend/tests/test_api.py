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


# --- Phase 2: per-run display toggles ----------------------------------------
def _evaluate_html(**data):
    files = {"step_file": ("synthetic-sig-strip.stp", EXAMPLE_STEP.read_bytes(),
                           "application/octet-stream")}
    return client.post("/evaluate", files=files, data={"family": "stamping", **data})


def _evaluate_json(**data):
    files = {"step_file": ("synthetic-sig-strip.stp", EXAMPLE_STEP.read_bytes(),
                           "application/octet-stream")}
    return client.post("/api/evaluate", files=files, data={"family": "stamping", **data})


def test_defaults_render_both_sections():
    html = _evaluate_html().text
    assert "Requires manual check" in html
    assert "Generate strip layout" in html


def test_show_manual_false_hides_manual_section():
    html = _evaluate_html(show_manual="false").text
    assert "Requires manual check" not in html
    # strip is untouched by the manual toggle
    assert "Generate strip layout" in html


def test_show_strip_false_hides_strip_button():
    html = _evaluate_html(show_strip="false").text
    assert "Generate strip layout" not in html
    # manual section still present when only strip is toggled off
    assert "Requires manual check" in html


def test_json_keeps_manual_data_but_records_display_options():
    body = _evaluate_json(show_manual="false").json()
    # underlying data is intact — hiding is presentation-only
    assert body["summary"]["manual_check_parameters"]
    assert body["display_options"]["show_manual"] is False
    assert body["display_options"]["show_strip"] is True


def test_toggles_do_not_change_verdicts_or_score():
    base = _evaluate_json().json()["summary"]
    toggled = _evaluate_json(show_manual="false", show_strip="false").json()["summary"]
    assert base["counts"] == toggled["counts"]
    assert base["readiness_score"] == toggled["readiness_score"]
    assert len(base["results"]) == len(toggled["results"])


# --- Phase 3: correction advisor ---------------------------------------------
DEFECT_STEP = Path(__file__).resolve().parents[2] / "examples" / "bma_shield_defect.stp"


def test_defect_example_produces_corrections():
    data = DEFECT_STEP.read_bytes()
    r = client.post(
        "/api/evaluate",
        files={"step_file": ("bma_shield_defect.stp", data, "application/octet-stream")},
    )
    assert r.status_code == 200
    body = r.json()
    assert "corrections" in body
    assert len(body["corrections"]) >= 1
    # every correction cites a rule and carries a recommendation (provenance)
    for c in body["corrections"]:
        assert c["rule_id"] and c["recommendation"] and c["rationale"]
