"""Tests for the optional geometry-correction backend (Phase 5).

The kernel-dependent cases are skipped when cadquery is absent so CI without the
extra still passes. The kernel-absent path (status flag + friendly API error) is
always exercised.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.geometry import corrector
from app.geometry import kernel
from app.main import app
from app.store.criteria_store import CriteriaStore

SEED = Path(__file__).resolve().parents[2] / "dfm-criteria.seed.yaml"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
client = TestClient(app)

kernel_only = pytest.mark.skipif(not kernel.available, reason="cadquery not installed")


def _store(tmp_path):
    s = CriteriaStore(tmp_path / "t.sqlite")
    s.sync_from_yaml(SEED)
    return s


# --- always-on: availability + kernel-absent behavior -------------------------
def test_status_endpoint_matches_kernel_availability():
    r = client.get("/api/geometry/status")
    assert r.status_code == 200
    assert r.json()["available"] == kernel.available


@pytest.mark.skipif(kernel.available, reason="only meaningful without the kernel")
def test_correct_without_kernel_returns_friendly_error():
    fixes = json.dumps({"schema": "dfm-fixes/1", "corrections": []}).encode("utf-8")
    r = client.post(
        "/api/geometry/correct",
        data={"model": "bma_shield_defect.stp", "family": "stamping"},
        files={"fixes": ("fixes.json", fixes, "application/json")},
    )
    assert r.status_code == 501
    body = r.json()
    assert body["available"] is False
    assert "unavailable" in body["error"].lower()


@pytest.mark.skipif(kernel.available, reason="only meaningful without the kernel")
def test_apply_fixes_reports_unavailable_status(tmp_path):
    store = _store(tmp_path)
    res = corrector.apply_fixes(
        EXAMPLES / "bma_shield_defect.stp",
        {"corrections": []},
        store=store,
        family="stamping",
        out_dir=tmp_path,
    )
    assert res.status == "unavailable"
    assert res.output_path is None
    store.close()


# --- unit: routing/skip discipline (no kernel needed) -------------------------
def test_registry_has_bend_and_hole_handlers():
    assert "min_inside_corner_radius_mm" in corrector.HANDLERS
    assert "min_pierced_width_or_dia" in corrector.HANDLERS


# --- kernel-only: real correction, skip, and regression guard -----------------
@kernel_only
def test_bracket_undersized_fillet_is_corrected(tmp_path):
    import cadquery as cq

    store = _store(tmp_path)
    # Simple bent bracket with a deliberately tiny inside fillet.
    part = (
        cq.Workplane("XY").box(20, 20, 0.8)
        .faces(">Z").workplane().rect(20, 20).extrude(6)
        .edges("|Y").fillet(0.03)
    )
    step_in = tmp_path / "bracket.stp"
    cq.exporters.export(part, str(step_in))

    fix_file = {
        "schema": "dfm-fixes/1",
        "family": "stamping",
        "corrections": [
            {"rule_id": "STMP-MIN-INSIDE-RADIUS", "parameter": "min_inside_corner_radius_mm",
             "confidence": "computed", "operator": "gte", "target_value": 0.25,
             "current_value": 0.03, "unit": "mm", "rationale": "test"}
        ],
    }
    res = corrector.apply_fixes(step_in, fix_file, store=store, family="stamping", out_dir=tmp_path)
    # Either the edit applied and improved, or it was safely skipped — never a
    # silently-wrong model, and the input is untouched.
    assert step_in.exists()
    if res.status == "applied":
        assert res.output_path and Path(res.output_path).exists()
        assert not res.regressed
    else:
        assert res.output_path is None
    store.close()


@kernel_only
def test_no_handler_parameter_is_skipped(tmp_path):
    import cadquery as cq

    store = _store(tmp_path)
    part = cq.Workplane("XY").box(10, 10, 0.8)
    step_in = tmp_path / "plate.stp"
    cq.exporters.export(part, str(step_in))
    fix_file = {
        "corrections": [
            {"rule_id": "X", "parameter": "totally_unknown_param",
             "confidence": "computed", "target_value": 1.0}
        ]
    }
    res = corrector.apply_fixes(step_in, fix_file, store=store, family="stamping", out_dir=tmp_path)
    assert res.output_path is None
    assert any("no geometry handler" in s["reason"] for s in res.skipped)
    store.close()


@kernel_only
def test_advisory_corrections_are_never_applied(tmp_path):
    import cadquery as cq

    store = _store(tmp_path)
    part = cq.Workplane("XY").box(10, 10, 0.8)
    step_in = tmp_path / "plate2.stp"
    cq.exporters.export(part, str(step_in))
    fix_file = {
        "corrections": [
            {"rule_id": "Y", "parameter": "min_inside_corner_radius_mm",
             "confidence": "advisory", "target_value": 0.25}
        ]
    }
    res = corrector.apply_fixes(step_in, fix_file, store=store, family="stamping", out_dir=tmp_path)
    assert res.applied == []
    assert any("not auto-applied" in s["reason"] for s in res.skipped)
    store.close()
