"""Lightweight STEP (AP203/AP214) geometry reader — Phase 1.

This intentionally avoids a heavy CAD kernel (OpenCASCADE / pythonocc-core).
It reads the explicit ``CARTESIAN_POINT`` vertices from the STEP text to derive
a reliable bounding box and a stock-thickness estimate (the smallest extent of
a thin sheet-metal part). Richer features — hole diameters, bend angles, wall
thickness, draft — require a real B-rep kernel and are reported as
``None`` (i.e. "needs manual check") until that extractor is plugged in.

The public surface (``extract_step`` -> features dict) is stable so an
OpenCASCADE-backed implementation can replace the internals without touching
the engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .thickness import analyze_thickness, parse_entities

# Matches: CARTESIAN_POINT('label',(x,y,z))  — label may be empty, numbers may
# use scientific notation. We only need the coordinate triple.
_POINT_RE = re.compile(
    r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", re.IGNORECASE
)
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


@dataclass
class GeometryFeatures:
    source_file: str
    point_count: int = 0
    bbox_min: tuple[float, float, float] | None = None
    bbox_max: tuple[float, float, float] | None = None
    dimensions_mm: tuple[float, float, float] | None = None
    min_extent_mm: float | None = None
    is_sheet_like: bool = False
    # Stock gauge is only inferred when the part is a flat blank; for formed/3D
    # parts it stays None (the smallest bbox extent is NOT the material gauge).
    stock_thickness_mm: float | None = None
    # Model-derived sheet-gauge analysis (bend radii + flat-wall pairs): the
    # expected material thickness plus any non-uniform wall regions. None when the
    # model has no usable B-rep faces.
    thickness_analysis: dict[str, Any] | None = None
    # Feature dict consumed by the rule engine (parameter -> measured value).
    features: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "point_count": self.point_count,
            "bbox_min": self.bbox_min,
            "bbox_max": self.bbox_max,
            "dimensions_mm": self.dimensions_mm,
            "min_extent_mm": self.min_extent_mm,
            "is_sheet_like": self.is_sheet_like,
            "stock_thickness_mm": self.stock_thickness_mm,
            "thickness_analysis": self.thickness_analysis,
            "features": self.features,
            "warnings": self.warnings,
        }


def _parse_points(text: str) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    for m in _POINT_RE.finditer(text):
        nums = _NUM_RE.findall(m.group(1))
        if len(nums) >= 3:
            points.append((float(nums[0]), float(nums[1]), float(nums[2])))
    return points


def extract_step(path: str | Path) -> GeometryFeatures:
    path = Path(path)
    geo = GeometryFeatures(source_file=path.name)

    text = path.read_text(encoding="utf-8", errors="ignore")
    points = _parse_points(text)
    geo.point_count = len(points)

    if not points:
        geo.warnings.append("No CARTESIAN_POINT vertices found — cannot derive geometry.")
        return geo

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    geo.bbox_min = (min(xs), min(ys), min(zs))
    geo.bbox_max = (max(xs), max(ys), max(zs))
    dims = (
        geo.bbox_max[0] - geo.bbox_min[0],
        geo.bbox_max[1] - geo.bbox_min[1],
        geo.bbox_max[2] - geo.bbox_min[2],
    )
    geo.dimensions_mm = dims

    nonzero = sorted(d for d in dims if d > 1e-6)
    if nonzero:
        geo.min_extent_mm = round(nonzero[0], 4)

    # Only a flat blank lets us read stock gauge off the bounding box: its
    # smallest extent must be far thinner than the next-smallest. Formed/3D parts
    # (these spring contacts) fail this test, so we don't guess a gauge for them.
    if len(nonzero) >= 2 and nonzero[0] <= 0.2 * nonzero[1]:
        geo.is_sheet_like = True
        geo.stock_thickness_mm = round(nonzero[0], 4)
    else:
        geo.warnings.append(
            "Formed/3D geometry — stock gauge cannot be derived from the bounding "
            "box. Material thickness is taken from the drawing/spec, not the model."
        )

    # Model-derived sheet gauge + wall-thickness uniformity (works for formed 3D
    # parts too, where the bounding box can't give a gauge). This is what lets the
    # DFM run on a 3D model alone, with no 2D drawing/spec.
    try:
        geo.thickness_analysis = analyze_thickness(parse_entities(text))
    except Exception as exc:  # never let a parse quirk break the whole extract
        geo.warnings.append(f"Thickness analysis skipped: {exc}")
        geo.thickness_analysis = None

    ta = geo.thickness_analysis
    geo.features = {
        "bbox_length_mm": round(max(dims), 4) if dims else None,
        "min_bbox_extent_mm": geo.min_extent_mm,
        # Present only when the part is a flat blank; None otherwise.
        "stock_thickness_measured_mm": geo.stock_thickness_mm,
        # Expected material thickness derived from the model (gauge), and the
        # worst wall-thickness deviation from it — drives the uniformity rule.
        "material_thickness_mm": ta["expected_thickness_mm"] if ta else None,
        "stock_thickness_uniformity_dev_mm": ta["max_deviation_mm"] if ta else None,
        # Min inside bend (form) radius from coaxial cylinder pairs — drives the
        # inside-corner / form-radius rules instead of a manual check.
        "min_inside_corner_radius_mm": ta.get("min_inside_radius_mm") if ta else None,
    }
    if ta and not ta["consistent"]:
        worst = max(r["thickness_mm"] for r in ta["inconsistencies"])
        geo.warnings.append(
            f"Material-thickness inconsistency: expected ~{ta['expected_thickness_mm']} mm "
            f"(from {ta['gauge_source']}), but a wall measures up to {worst} mm in "
            f"{len(ta['inconsistencies'])} region(s)."
        )
    return geo
