"""Flat-state "enough material" measurements on the developed blank.

These produce a small feature dict consumed by the existing deterministic rule
engine (so the verdicts, margins, provenance and readiness scoring are all the
normal machinery — nothing special-cased). The matching rules live in the
stamping family YAML:

* ``flat_min_web_mm``               -> STMP-FLAT-MIN-WEB
* ``flat_min_feature_to_edge_mm``   -> STMP-FLAT-FEATURE-TO-EDGE
* ``flat_min_carrier_connection_mm``-> STMP-FLAT-CARRIER-CONNECTION
* ``flat_patch_overlap_mm``         -> STMP-FLAT-OVERLAP

When the flat pattern is not fully developed (``status != "ok"``) every feature
is ``None`` so the engine returns ``manual`` — the checks are never silently
passed. The unfold reasons are surfaced separately for the manual detail.
"""
from __future__ import annotations

import math
from typing import Any

from . import geom2d
from .unfold import FlatPattern, PatchMeta


def _closest_points(a: list, b: list) -> tuple[float, tuple[float, float], tuple[float, float]]:
    """Closest distance + witness points between two closed polygons (disjoint)."""
    best = math.inf
    wa: tuple[float, float] = a[0]
    wb: tuple[float, float] = b[0]
    for ring, other in ((a, b), (b, a)):
        for v in ring:
            for c, d in geom2d.edges(other):
                foot = _foot(v, c, d)
                dist = math.dist(v, foot)
                if dist < best:
                    best = dist
                    if ring is a:
                        wa, wb = v, foot
                    else:
                        wa, wb = foot, v
    return best, wa, wb


def _foot(p, a, b) -> tuple[float, float]:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-15:
        return a
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    return (ax + t * dx, ay + t * dy)


def compute_flat_features(fp: FlatPattern) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(features, details)``.

    ``features`` is merged into the engine's measured-feature dict. ``details``
    carries flat-space witness locations (and model-space where recoverable) so
    the 2D render can highlight the narrow zones and the 3D viewer can pin them.
    """
    features: dict[str, Any] = {
        "flat_min_web_mm": None,
        "flat_min_feature_to_edge_mm": None,
        "flat_min_carrier_connection_mm": None,  # no carrier context yet -> manual
        "flat_patch_overlap_mm": None,
    }
    details: dict[str, Any] = {}

    if fp.status != "ok":
        details["reasons"] = fp.reasons
        return features, details

    # Gather cutouts tagged with their owning patch (for model-space mapping).
    cutouts: list[tuple[int, list]] = []
    for meta in fp.patches:
        for hole in meta.holes2d:
            if len(hole) >= 3:
                cutouts.append((meta.idx, hole))

    # 1) Overlap (blocker): non-adjacent developed patches must not intersect.
    overlap = 0.0
    metas = fp.patches
    for i in range(len(metas)):
        for j in range(i + 1, len(metas)):
            ov = geom2d.polygons_overlap_area(metas[i].poly2d, metas[j].poly2d)
            overlap = max(overlap, ov)
    features["flat_patch_overlap_mm"] = round(overlap, 4)

    # 2) Min web between adjacent cutouts.
    if len(cutouts) >= 2:
        best = math.inf
        witness = None
        for i in range(len(cutouts)):
            for j in range(i + 1, len(cutouts)):
                (ia, pa), (_ib, pb) = cutouts[i], cutouts[j]
                dist, wa, wb = _closest_points(pa, pb)
                if dist < best:
                    best = dist
                    witness = (ia, wa, wb)
        features["flat_min_web_mm"] = round(best, 4)
        if witness:
            ia, wa, wb = witness
            mid = ((wa[0] + wb[0]) / 2, (wa[1] + wb[1]) / 2)
            details["min_web"] = {
                "value_mm": round(best, 4),
                "a": [round(wa[0], 3), round(wa[1], 3)],
                "b": [round(wb[0], 3), round(wb[1], 3)],
                "mid": [round(mid[0], 3), round(mid[1], 3)],
                "model": fp.flat_to_model(ia, mid),
            }
    else:
        details["min_web_note"] = "Fewer than two cutouts in the developed blank — no web to measure."

    # 3) Min cutout-to-edge (measured to the owning patch's outer ring; the shared
    #    bend edge is treated as an edge, i.e. conservative).
    best_edge = math.inf
    edge_witness = None
    for idx, hole in cutouts:
        meta = next((m for m in metas if m.idx == idx), None)
        if meta is None:
            continue
        dist, wa, wb = _closest_points(hole, meta.poly2d)
        if dist < best_edge:
            best_edge = dist
            edge_witness = (idx, wa, wb)
    if edge_witness is not None:
        features["flat_min_feature_to_edge_mm"] = round(best_edge, 4)
        idx, wa, wb = edge_witness
        mid = ((wa[0] + wb[0]) / 2, (wa[1] + wb[1]) / 2)
        details["feature_to_edge"] = {
            "value_mm": round(best_edge, 4),
            "a": [round(wa[0], 3), round(wa[1], 3)],
            "b": [round(wb[0], 3), round(wb[1], 3)],
            "mid": [round(mid[0], 3), round(mid[1], 3)],
            "model": fp.flat_to_model(idx, mid),
        }

    return features, details
