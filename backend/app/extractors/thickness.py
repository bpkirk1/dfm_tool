"""Sheet-metal material-thickness analysis from a STEP B-rep — Phase 1.

When no 2D drawing/spec is supplied, the *expected material thickness* (stock
gauge) is derived directly from the 3D model and any non-uniform wall regions are
flagged as inconsistencies. The method is dependency-free (no CAD kernel):

1. **Bend gauge** — sheet-metal bends export as concentric inner/outer
   ``CYLINDRICAL_SURFACE`` pairs sharing an axis; the radius difference equals the
   stock gauge. This is the most reliable signal and is averaged over every bend.
2. **Flat-wall gauge** — pairs of parallel ``PLANE`` faces that are *stacked*
   (large, strongly overlapping, separated by a small gap) are the two skins of a
   flat wall; the gap is the local thickness.

The expected gauge is the dominant value across both signals. Any flat wall whose
local thickness is well above the gauge (and still wall-scale, not a cavity) is a
**thickness inconsistency** — reported with its location so the report/3D viewer
can highlight it. Precise per-feature thickness (interference, true min-wall)
remains a B-rep-kernel item; this surfaces gross gauge non-uniformity.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_REC = re.compile(r"^\s*#(\d+)\s*=\s*(.*)$", re.DOTALL)
_TYP = re.compile(r"^\s*\(?\s*([A-Za-z_0-9]+)\s*\(")
_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_BOOL_END = re.compile(r"\.(T|F)\.\s*$")

# --- tuning (geometry heuristics; gauge limits live in the criteria YAML) -------
_MIN_FACE_EXT_MM = 0.25      # ignore sliver/chamfer faces when pairing walls
_MAX_OVERLAP_REL = 1.5       # lateral offset / face size: a true wall is stacked
_WALL_MAX_MM = 0.75          # gaps wider than this are cavities, not one wall
_THIN_SAMPLE_MM = 0.20       # samples at/below this define the stock-gauge cluster
_OFF_GAUGE_FLOOR_MM = 0.20   # absolute floor for "off-gauge" (with 2x gauge)
_CLUSTER_RADIUS_MM = 1.5     # merge nearby inconsistency hits into one region

# A genuine single-gauge sheet part is uniform apart from a *few* localized
# anomalies. If off-gauge walls are pervasive (a high fraction of all walls) or
# there are many distinct off-gauge regions, the geometry is feature-rich or not
# single-gauge stock at all (e.g. a molded part), and the flat-wall heuristic is
# reading legitimate features as "walls". In that case we do NOT claim stock-gauge
# anomalies — we report the gauge as inconclusive-for-uniformity instead of
# spraying false positives across the part.
_MAX_ANOMALY_REGIONS = 8     # more off-gauge regions than this => not stock anomalies
_MAX_OFF_GAUGE_RATIO = 0.40  # off-gauge walls must be a clear minority of all walls

Vec = tuple[float, float, float]


def parse_entities(text: str) -> dict[int, tuple[str, str]]:
    """Parse a STEP DATA section into ``{id: (TYPE, body)}`` (one record per ``;``)."""
    ents: dict[int, tuple[str, str]] = {}
    for chunk in text.split(";"):
        m = _REC.match(chunk)
        if not m:
            continue
        body = m.group(2)
        t = _TYP.match(body)
        ents[int(m.group(1))] = ((t.group(1).upper() if t else ""), body)
    return ents


def _refs(s: str) -> list[int]:
    return [int(x) for x in re.findall(r"#(\d+)", s)]


def _coords(ents: dict[int, tuple[str, str]], eid: int) -> Vec:
    nums = _NUM.findall(ents[eid][1])
    return (float(nums[0]), float(nums[1]), float(nums[2]))


def _unit(v: Vec) -> Vec:
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mode(values: list[float]) -> float | None:
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


# --- bends: coaxial cylinder radius difference = stock gauge ---------------------
def _cylinders(ents: dict[int, tuple[str, str]]) -> list[tuple[Vec, Vec, float]]:
    """Return ``(location, axis, radius)`` for every cylindrical surface."""
    cyls: list[tuple[Vec, Vec, float]] = []
    for eid, (typ, body) in ents.items():
        if typ != "CYLINDRICAL_SURFACE":
            continue
        r = _refs(body)
        radius = float(_NUM.findall(body)[-1])
        axr = _refs(ents[r[0]][1])
        loc = _coords(ents, axr[0])
        axis = _unit(_coords(ents, axr[1]))
        cyls.append((loc, axis, radius))
    return cyls


def _coaxial_bends(
    ents: dict[int, tuple[str, str]]
) -> list[tuple[float, float, Vec]]:
    """Coaxial inner/outer cylinder pairs (sheet-metal bends).

    Returns ``(gauge, inside_radius, inside_location)`` per pair: the radius
    *difference* is the stock gauge, the *smaller* radius is the inside bend
    (form) radius, and the location is that inner cylinder's axis point so the
    exact corner can be pinned in the 3D viewer. The geometric coaxial +
    gauge-band filter is what makes both robust — a raw per-cylinder radius is
    not trustworthy across exporters.
    """
    cyls = _cylinders(ents)
    out: list[tuple[float, float, Vec]] = []
    n = len(cyls)
    for i in range(n):
        li, ai, ri = cyls[i]
        for j in range(i + 1, n):
            lj, aj, rj = cyls[j]
            if abs(_dot(ai, aj)) < 0.999:
                continue
            v = _sub(lj, li)
            proj = _dot(v, ai)
            perp = math.sqrt(max(_dot(v, v) - proj * proj, 0.0))
            if perp > 0.05:  # not truly coaxial
                continue
            d = round(abs(ri - rj), 4)
            if 0.02 <= d <= 1.0:
                inside_loc = li if ri <= rj else lj
                out.append((d, min(ri, rj), inside_loc))
    return out


def _bend_gauges(ents: dict[int, tuple[str, str]]) -> list[float]:
    return [g for g, _, _ in _coaxial_bends(ents)]


# --- flat walls: stacked parallel planar faces ----------------------------------
def _vertex_points(
    ents: dict[int, tuple[str, str]], fid: int, cache: dict[int, list[Vec]]
) -> list[Vec]:
    if fid in cache:
        return cache[fid]
    pts: list[Vec] = []
    seen: set[int] = set()
    stack = [fid]
    while stack:
        x = stack.pop()
        if x in seen or x not in ents:
            continue
        seen.add(x)
        typ, body = ents[x]
        if typ == "VERTEX_POINT":
            pts.append(_coords(ents, _refs(body)[0]))
            continue
        if typ in (
            "CARTESIAN_POINT", "DIRECTION", "PLANE", "CYLINDRICAL_SURFACE",
            "LINE", "CIRCLE", "VECTOR", "AXIS2_PLACEMENT_3D",
        ):
            continue
        stack.extend(_refs(body))
    cache[fid] = pts
    return pts


def _planar_faces(
    ents: dict[int, tuple[str, str]]
) -> list[tuple[Vec, Vec, float]]:
    """Return ``(centroid, normal, extent)`` for each planar ADVANCED_FACE."""
    cache: dict[int, list[Vec]] = {}
    faces: list[tuple[Vec, Vec, float]] = []
    for eid, (typ, body) in ents.items():
        if typ != "ADVANCED_FACE":
            continue
        surf = _refs(body)[-1]
        if surf not in ents or ents[surf][0] != "PLANE":
            continue
        ax = _refs(ents[surf][1])[0]
        normal = _unit(_coords(ents, _refs(ents[ax][1])[1]))
        pts = _vertex_points(ents, eid, cache)
        if len(pts) < 3:
            continue
        c = (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
            sum(p[2] for p in pts) / len(pts),
        )
        ext = max(math.dist(p, c) for p in pts)
        faces.append((c, normal, ext))
    return faces


def _wall_samples(
    faces: list[tuple[Vec, Vec, float]]
) -> list[tuple[float, Vec]]:
    """Local flat-wall thicknesses: ``(thickness_mm, location)`` per wall face."""
    samples: list[tuple[float, Vec]] = []
    n = len(faces)
    for i in range(n):
        ci, ni, ei = faces[i]
        if ei < _MIN_FACE_EXT_MM:
            continue
        best: tuple[float, Vec] | None = None
        for j in range(n):
            if i == j:
                continue
            cj, nj, ej = faces[j]
            if ej < _MIN_FACE_EXT_MM or abs(_dot(ni, nj)) < 0.999:
                continue
            v = _sub(cj, ci)
            gap = abs(_dot(v, ni))
            if gap < 0.02 or gap > _WALL_MAX_MM:
                continue
            lateral = math.sqrt(max(_dot(v, v) - gap * gap, 0.0))
            if lateral / min(ei, ej) > _MAX_OVERLAP_REL:
                continue
            if best is None or gap < best[0]:
                best = (round(gap, 3), ci)
        if best is not None:
            samples.append(best)
    return samples


def _cluster_regions(
    hits: list[tuple[float, Vec]]
) -> list[dict[str, Any]]:
    """Merge nearby off-gauge hits into regions, keeping the worst thickness."""
    regions: list[dict[str, Any]] = []
    for thickness, loc in sorted(hits, key=lambda h: -h[0]):
        placed = False
        for reg in regions:
            if math.dist(loc, reg["_center"]) <= _CLUSTER_RADIUS_MM:
                reg["hits"] += 1
                placed = True
                break
        if not placed:
            regions.append(
                {
                    "thickness_mm": thickness,
                    "location": [round(loc[0], 2), round(loc[1], 2), round(loc[2], 2)],
                    "_center": loc,
                    "hits": 1,
                }
            )
    for reg in regions:
        reg.pop("_center", None)
    return regions


def analyze_thickness(ents: dict[int, tuple[str, str]]) -> dict[str, Any] | None:
    """Derive the expected stock gauge from the model and flag non-uniform walls."""
    bends = _coaxial_bends(ents)
    bend_gauges = [g for g, _, _ in bends]
    min_inside_radius = None
    min_inside_location = None
    if bends:
        _g, _r, _loc = min(bends, key=lambda b: b[1])
        min_inside_radius = round(_r, 3)
        min_inside_location = [round(_loc[0], 2), round(_loc[1], 2), round(_loc[2], 2)]
    faces = _planar_faces(ents)
    walls = _wall_samples(faces)

    if not bend_gauges and not walls:
        return None

    wall_vals = [t for t, _ in walls]
    thin = [g for g in bend_gauges if g <= _THIN_SAMPLE_MM] + [
        t for t in wall_vals if t <= _THIN_SAMPLE_MM
    ]
    gauge = _mode(thin) or _mode(bend_gauges) or _mode(wall_vals)
    if gauge is None:
        return None

    bend_gauge = _mode(bend_gauges)
    if bend_gauges and walls:
        source = "bend radii + flat-wall pairs"
    elif bend_gauges:
        source = "bend radii (concentric cylinders)"
    else:
        source = "flat-wall pairs"

    off_gauge_threshold = max(_OFF_GAUGE_FLOOR_MM, round(2.0 * gauge, 3))
    hits = [(t, loc) for t, loc in walls if t > off_gauge_threshold]
    regions = _cluster_regions(hits)

    # Robustness gate: only call these "true gauge anomalies" when the part is an
    # otherwise-uniform single-gauge sheet with a few localized off-gauge spots.
    off_ratio = round(len(hits) / len(wall_vals), 3) if wall_vals else 0.0
    candidate_regions = len(regions)
    feature_rich = (
        candidate_regions > _MAX_ANOMALY_REGIONS or off_ratio > _MAX_OFF_GAUGE_RATIO
    )
    if feature_rich:
        uniformity_status = "inconclusive"  # geometry too feature-rich to attribute
        regions = []
    elif regions:
        uniformity_status = "anomalies"
    else:
        uniformity_status = "uniform"

    max_thickness = max(wall_vals) if wall_vals else gauge
    max_deviation = round(max(0.0, max_thickness - gauge), 3) if regions else 0.0

    return {
        "expected_thickness_mm": gauge,
        "gauge_source": source,
        "bend_gauge_mm": bend_gauge,
        "bend_sample_count": len(bend_gauges),
        # Smallest inside bend (form) radius measured from coaxial cylinder pairs,
        # plus its model-space location so the viewer can pin that exact corner.
        "min_inside_radius_mm": min_inside_radius,
        "min_inside_radius_location": min_inside_location,
        "wall_sample_count": len(walls),
        "thickness_min_mm": round(min(wall_vals), 3) if wall_vals else None,
        "thickness_max_mm": round(max(wall_vals), 3) if wall_vals else None,
        "off_gauge_threshold_mm": off_gauge_threshold,
        # Diagnostics behind the verdict (shown in the report for transparency).
        "candidate_region_count": candidate_regions,
        "off_gauge_ratio": off_ratio,
        "uniformity_status": uniformity_status,
        "inconsistencies": regions,
        "max_deviation_mm": max_deviation,
        # Uniform and inconclusive both pass the DFM rule; only real localized
        # anomalies fail it.
        "consistent": uniformity_status != "anomalies",
    }
