"""3D-viewer marker selection is driven by the `marker:` tag, not the rule id.

Renaming a rule in YAML must not silently kill marker pinning as long as the
rule still carries its `marker: thickness|radius` tag (item 3).
"""
from __future__ import annotations

from app.engine.evaluator import evaluate_family
from app.models.criteria import ProcessFamily
from app.report.builder import _build_markers


def _thickness_analysis():
    return {
        "expected_thickness_mm": 0.08,
        "inconsistencies": [{"thickness_mm": 0.20, "location": [1.0, 2.0, 3.0]}],
        "min_inside_radius_mm": 0.10,
        "min_inside_radius_location": [4.0, 5.0, 6.0],
    }


def test_tagged_thickness_rule_pins_marker_after_rename():
    fam = ProcessFamily(
        rules=[
            {"id": "RENAMED-THICKNESS-2026", "parameter": "stock_thickness_uniformity_dev_mm",
             "operator": "lte", "limit": 0.05, "severity": "blocker", "source": "model",
             "marker": "thickness"},
        ]
    )
    summary = evaluate_family("stamping", fam, {"stock_thickness_uniformity_dev_mm": 0.20}, "v")
    markers = _build_markers(_thickness_analysis(), summary)
    assert markers, "expected an off-gauge marker even though the rule id changed"
    assert markers[0]["rule_id"] == "RENAMED-THICKNESS-2026"


def test_tagged_radius_rule_pins_marker_after_rename():
    fam = ProcessFamily(
        rules=[
            {"id": "RENAMED-RADIUS-2026", "parameter": "min_inside_corner_radius_mm",
             "operator": "gte", "limit": 0.20, "severity": "major", "source": "dfm",
             "marker": "radius"},
        ]
    )
    summary = evaluate_family("stamping", fam, {"min_inside_corner_radius_mm": 0.10}, "v")
    markers = _build_markers(_thickness_analysis(), summary)
    radius_markers = [m for m in markers if m["rule_id"] == "RENAMED-RADIUS-2026"]
    assert radius_markers, "radius marker should pin via the marker tag"


def test_untagged_rule_falls_back_to_id_constant():
    # No marker tag, but the historical id is kept -> still pins (back-compat).
    fam = ProcessFamily(
        rules=[
            {"id": "STMP-STOCK-THICKNESS-UNIFORM", "parameter": "stock_thickness_uniformity_dev_mm",
             "operator": "lte", "limit": 0.05, "severity": "blocker", "source": "model"},
        ]
    )
    summary = evaluate_family("stamping", fam, {"stock_thickness_uniformity_dev_mm": 0.20}, "v")
    markers = _build_markers(_thickness_analysis(), summary)
    assert markers and markers[0]["rule_id"] == "STMP-STOCK-THICKNESS-UNIFORM"
