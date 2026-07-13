"""Orchestrates one DFM run: extract -> detect family -> evaluate -> assemble.

Returns a single context dict the report template (or API) consumes. Each
verdict in the result carries its rule id and cited source, so the report never
shows an unexplained pass/flag/fail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..engine import evaluate_family
from ..extractors import detect_family, extract_pdf, extract_step
from ..store import CriteriaStore


@dataclass
class RunInputs:
    step_path: str | Path | None = None
    pdf_path: str | Path | None = None
    family: str | None = None  # explicit override; otherwise auto-detected
    part_name: str | None = None


# Rule whose failing regions we can localize from the model-derived thickness map.
_THICKNESS_RULE_ID = "STMP-STOCK-THICKNESS-UNIFORM"
# Rules driven by the model-derived minimum inside corner/bend radius; we can pin
# the exact corner that drives them.
_RADIUS_PARAM = "min_inside_corner_radius_mm"
_VERDICT_RANK = {"fail": 3, "flag": 2, "manual": 1, "pass": 0}


def _radius_marker(
    thickness: dict[str, Any] | None, summary: Any
) -> dict[str, Any] | None:
    """Pin the exact minimum inside-radius corner when a radius rule flags/fails."""
    if not thickness:
        return None
    loc = thickness.get("min_inside_radius_location")
    radius = thickness.get("min_inside_radius_mm")
    if not loc or radius is None:
        return None

    # The worst-verdict radius rule drives the marker color and click-target.
    worst = None
    for r in summary.results:
        if r.parameter != _RADIUS_PARAM or r.verdict not in ("fail", "flag"):
            continue
        if worst is None or _VERDICT_RANK[r.verdict] > _VERDICT_RANK[worst.verdict]:
            worst = r
    if worst is None:
        return None

    return {
        "rule_id": worst.rule_id,
        "parameter": _RADIUS_PARAM,  # lets the viewer link both radius rules here
        "verdict": worst.verdict,
        "location": loc,
        "value_mm": radius,
        "label": f"Min inside radius R{radius} mm (limit {worst.limit_detail})",
    }


def _build_markers(thickness: dict[str, Any] | None, summary: Any) -> list[dict[str, Any]]:
    """Turn localized geometry findings into 3D-viewer markers.

    Today the only per-feature localization we can derive without a B-rep kernel
    is the off-gauge wall regions from the thickness analysis. Each marker carries
    a model-space ``location`` so the viewer can pin the exact area that failed
    instead of tinting the whole part.
    """
    markers: list[dict[str, Any]] = []
    if not thickness:
        return markers

    verdict = "fail"
    for r in summary.results:
        if r.rule_id == _THICKNESS_RULE_ID:
            verdict = r.verdict
            break

    gauge = thickness.get("expected_thickness_mm")
    for i, reg in enumerate(thickness.get("inconsistencies") or [], 1):
        t = reg.get("thickness_mm")
        markers.append(
            {
                "rule_id": _THICKNESS_RULE_ID,
                "verdict": verdict,
                "location": reg.get("location"),
                "value_mm": t,
                "label": (
                    f"Off-gauge wall #{i}: {t} mm"
                    + (f" vs {gauge} mm gauge" if gauge is not None else "")
                ),
            }
        )

    radius_marker = _radius_marker(thickness, summary)
    if radius_marker:
        markers.append(radius_marker)
    return markers


def build_report(store: CriteriaStore, inputs: RunInputs) -> dict[str, Any]:
    criteria = store.get_criteria()

    geometry = extract_step(inputs.step_path) if inputs.step_path else None
    drawing = extract_pdf(inputs.pdf_path) if inputs.pdf_path else None

    family_name = inputs.family or detect_family(
        pdf_name=Path(inputs.pdf_path).name if inputs.pdf_path else "",
        step_name=Path(inputs.step_path).name if inputs.step_path else "",
        drawing=drawing,
        default=next(iter(criteria.process_families), "stamping"),
    )
    family = criteria.family(family_name)

    # Assemble the measured feature set. Anything not present here evaluates to
    # "needs manual check" rather than a silent pass.
    features: dict[str, Any] = {}
    if geometry:
        features.update(geometry.features)

    summary = evaluate_family(
        family_name, family, features, ruleset_version=criteria.meta.ruleset_version
    )

    # Expected material thickness: prefer the drawing/spec when a 2D drawing was
    # supplied; otherwise derive it from the 3D model (the gauge from bends/walls).
    thickness = geometry.thickness_analysis if geometry else None
    spec_thickness = (
        family.material.thickness_mm if family.material else None
    )
    if inputs.pdf_path and spec_thickness is not None:
        material_thickness_used = spec_thickness
        material_thickness_source = "2D drawing / material spec"
    elif thickness:
        material_thickness_used = thickness["expected_thickness_mm"]
        material_thickness_source = f"3D model — {thickness['gauge_source']}"
    else:
        material_thickness_used = spec_thickness
        material_thickness_source = "material spec" if spec_thickness else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "part_name": inputs.part_name
        or (Path(inputs.step_path).stem if inputs.step_path else "unnamed part"),
        "family": family_name,
        "family_auto_detected": inputs.family is None,
        "ruleset_version": criteria.meta.ruleset_version,
        "schema_version": criteria.meta.schema_version,
        "summary": summary.to_dict(),
        "geometry": geometry.to_dict() if geometry else None,
        "drawing": drawing.to_dict() if drawing else None,
        "material": family.material.model_dump() if family.material else None,
        # Model-derived sheet-gauge analysis + which thickness drove the run.
        "thickness": thickness,
        "material_thickness_used_mm": material_thickness_used,
        "material_thickness_source": material_thickness_source,
        # Per-feature markers (model-space) so the viewer pins the exact failed
        # areas rather than tinting the whole part.
        "markers": _build_markers(thickness, summary),
        # Basename of the STEP model so the report can load it in the 3D viewer.
        "model_file": Path(inputs.step_path).name if inputs.step_path else None,
    }
