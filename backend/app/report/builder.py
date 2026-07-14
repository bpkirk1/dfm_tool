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

from ..corrections import build_corrections
from ..engine import evaluate_family
from ..extractors import detect_family, extract_pdf, extract_step
from ..flatpattern import analyze_flat
from ..store import CriteriaStore


@dataclass
class RunInputs:
    step_path: str | Path | None = None
    pdf_path: str | Path | None = None
    family: str | None = None  # explicit override; otherwise auto-detected
    part_name: str | None = None


# Rule whose failing regions we can localize from the model-derived thickness map.
_THICKNESS_RULE_ID = "STMP-STOCK-THICKNESS-UNIFORM"
# Flat-pattern check parameters (populated only for the stamping family) and the
# detail key that carries each one's flat-space + model-space witness location.
_FLAT_PARAMS = (
    "flat_min_web_mm",
    "flat_min_feature_to_edge_mm",
    "flat_min_carrier_connection_mm",
    "flat_patch_overlap_mm",
)
_FLAT_MARKER_MAP = {
    "STMP-FLAT-MIN-WEB": ("min_web", "flat_min_web_mm"),
    "STMP-FLAT-FEATURE-TO-EDGE": ("feature_to_edge", "flat_min_feature_to_edge_mm"),
}
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
    # Prefer rules tagged `marker: radius`; fall back to the parameter name so a
    # renamed rule still pins as long as it either carries the tag or the param.
    worst = None
    for r in summary.results:
        is_radius = getattr(r, "marker", None) == "radius" or r.parameter == _RADIUS_PARAM
        if not is_radius or r.verdict not in ("fail", "flag"):
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

    # Prefer the rule tagged `marker: thickness`; fall back to the id constant so
    # renaming the rule in YAML doesn't break marker pinning (as long as the tag
    # is present, or the historical id is kept).
    verdict = "fail"
    thickness_rule_id = _THICKNESS_RULE_ID
    tagged = next(
        (r for r in summary.results if getattr(r, "marker", None) == "thickness"), None
    )
    if tagged is None:
        tagged = next(
            (r for r in summary.results if r.rule_id == _THICKNESS_RULE_ID), None
        )
    if tagged is not None:
        verdict = tagged.verdict
        thickness_rule_id = tagged.rule_id

    gauge = thickness.get("expected_thickness_mm")
    for i, reg in enumerate(thickness.get("inconsistencies") or [], 1):
        t = reg.get("thickness_mm")
        markers.append(
            {
                "rule_id": thickness_rule_id,
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


def _flat_markers(details: dict[str, Any], summary: Any) -> list[dict[str, Any]]:
    """Pin failing/at-risk flat-state checks in the 3D viewer, where we can map
    the developed-blank location back to model space."""
    markers: list[dict[str, Any]] = []
    for r in summary.results:
        info = _FLAT_MARKER_MAP.get(r.rule_id)
        if not info or r.verdict not in ("fail", "flag"):
            continue
        zone = details.get(info[0])
        if not zone or not zone.get("model"):
            continue
        markers.append(
            {
                "rule_id": r.rule_id,
                "parameter": info[1],
                "verdict": r.verdict,
                "location": zone["model"],
                "value_mm": zone.get("value_mm"),
                "label": f"Flat-state {info[0].replace('_', ' ')}: {zone.get('value_mm')} mm "
                f"(limit {r.limit_detail})",
            }
        )
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

    # Phase 7: develop the flat blank and measure the flat-state material checks.
    # Only for the stamping family and only from a STEP model. Failures/manuals
    # flow through the normal engine because the rules live in the YAML.
    flat_result = None
    if inputs.step_path and family_name == "stamping":
        try:
            flat_cfg = getattr(family, "flat_pattern", None)
            flat_result = analyze_flat(str(inputs.step_path), flat_cfg)
            features.update(flat_result.features)
        except Exception as exc:  # a parse quirk must never break the report
            flat_result = None
            features.setdefault("_flat_error", str(exc))

    summary = evaluate_family(
        family_name,
        family,
        features,
        ruleset_version=criteria.meta.ruleset_version,
        scoring=criteria.meta.scoring,
    )

    # Surface the unfold reasons in the manual detail of the flat rules, so the
    # "Requires manual check" section explains *why* they weren't developed.
    if flat_result is not None and flat_result.flat_pattern.status != "ok":
        reasons = flat_result.flat_pattern.reasons or [
            "Flat pattern could not be fully developed from the model."
        ]
        detail = " ".join(reasons)
        for r in summary.results:
            if r.parameter in _FLAT_PARAMS and r.verdict == "manual":
                r.note = (r.note + " " if r.note else "") + detail

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

    markers = _build_markers(thickness, summary)
    if flat_result is not None:
        markers.extend(_flat_markers(flat_result.details, summary))

    # Deterministic correction advisor: what each fail/flag would need to comply.
    safety_margin = criteria.meta.corrections.safety_margin
    corrections = build_corrections(
        summary.to_dict()["results"], family_name, safety_margin
    )
    latest = store.latest()
    criteria_version = int(latest["id"]) if latest is not None else None

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
        "markers": markers,
        # Phase 7: developed-blank summary (status, bends, developed bbox, reasons).
        "flat_pattern": flat_result.flat_pattern.to_dict() if flat_result else None,
        # Basename of the STEP model so the report can load it in the 3D viewer.
        "model_file": Path(inputs.step_path).name if inputs.step_path else None,
        # Phase 3: deterministic correction advisor + provenance for the fix file.
        "corrections": [c.to_dict() for c in corrections],
        "criteria_version": criteria_version,
    }
