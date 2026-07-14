"""Recover the topology the unfolder needs from STEP text — kernel-free.

This builds on the primitives already proven in ``extractors/thickness.py``
(``parse_entities`` tokenizes the DATA section, ``_cylinders`` finds cylindrical
surfaces with axis + radius, plus the small vector helpers). Rather than
duplicating those regexes we import them here and add the two richer things a
flat-pattern needs and the thickness pass does not:

1. **Ordered planar-face loops** — the outer wire and any interior holes of each
   planar ``ADVANCED_FACE``, as ordered 3D point rings (EDGE_LOOP ->
   ORIENTED_EDGE -> EDGE_CURVE -> VERTEX_POINT).
2. **Bend adjacency** — which two planar patches a cylindrical bend joins, and
   the straight tangent edge on each patch where it meets the bend.

Nothing here decides *whether* a part is developable — that honesty gate lives in
``unfold.py``. This module only reports what it can and cannot see.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..extractors.thickness import (
    Vec,
    _coords,
    _cylinders,
    _dot,
    _refs,
    _sub,
    _unit,
    parse_entities,
)

# Curved surface types that are NOT simple developable bends. Their presence is a
# signal the part may have drawn/compound/rolled features the unfolder must not
# silently flatten.
_NON_DEVELOPABLE = (
    "B_SPLINE_SURFACE",
    "B_SPLINE_SURFACE_WITH_KNOTS",
    "BOUNDED_SURFACE",
    "SURFACE_OF_REVOLUTION",
    "SPHERICAL_SURFACE",
    "RATIONAL_B_SPLINE_SURFACE",
)
# Filleted corners/chamfers show up as tori/cones on otherwise-simple bends; we
# note them but do not treat them as blockers.
_SOFT_CURVED = ("TOROIDAL_SURFACE", "CONICAL_SURFACE")

Ents = dict[int, tuple[str, str]]


@dataclass
class Patch:
    """A planar face: its plane and its ordered boundary rings (3D)."""

    idx: int
    face_id: int
    origin: Vec
    normal: Vec
    outer: list[Vec]
    holes: list[list[Vec]] = field(default_factory=list)

    def area(self) -> float:
        return _polygon_area_3d(self.outer, self.normal)


@dataclass
class Bend:
    """A cylindrical bend joining (ideally) two patches."""

    gauge: float
    inside_radius: float
    axis: Vec
    axis_pt: Vec
    # Populated by adjacency: patch idx -> straight tangent edge (two 3D points).
    tangents: dict[int, tuple[Vec, Vec]] = field(default_factory=dict)
    # Recorded during unfolding (the true bend angle + allowance used).
    developed_angle_rad: float | None = None
    developed_ba: float | None = None
    developed_k: float | None = None

    @property
    def patch_ids(self) -> list[int]:
        return sorted(self.tangents.keys())


@dataclass
class StepGraph:
    patches: list[Patch]
    bends: list[Bend]
    non_developable: list[str]
    soft_curved: list[str]
    notes: list[str] = field(default_factory=list)


# --- vector helpers on top of thickness's -------------------------------------
def _cross(a: Vec, b: Vec) -> Vec:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _perp_distance_to_axis(p: Vec, axis_pt: Vec, axis: Vec) -> float:
    v = _sub(p, axis_pt)
    proj = _dot(v, axis)
    perp = _sub(v, _scale(axis, proj))
    return _norm(perp)


def _polygon_area_3d(pts: list[Vec], normal: Vec) -> float:
    if len(pts) < 3:
        return 0.0
    total = (0.0, 0.0, 0.0)
    o = pts[0]
    for i in range(1, len(pts) - 1):
        total = _add(total, _cross(_sub(pts[i], o), _sub(pts[i + 1], o)))
    return abs(_dot(total, normal)) / 2.0


# --- loop / face parsing ------------------------------------------------------
def _vertex_coord(ents: Ents, vid: int) -> Vec | None:
    body = ents[vid][1]
    refs = _refs(body)
    if not refs or refs[0] not in ents:
        return None
    return _coords(ents, refs[0])


def _loop_points(ents: Ents, loop_id: int) -> list[Vec]:
    pts: list[Vec] = []
    for oe in _refs(ents[loop_id][1]):
        if oe not in ents or ents[oe][0] != "ORIENTED_EDGE":
            continue
        obody = ents[oe][1]
        forward = not obody.rstrip().endswith(".F.)")
        edge_curve = None
        for r in _refs(obody):
            if r in ents and ents[r][0] == "EDGE_CURVE":
                edge_curve = r
                break
        if edge_curve is None:
            continue
        verts = [
            r for r in _refs(ents[edge_curve][1])
            if r in ents and ents[r][0] == "VERTEX_POINT"
        ]
        if not verts:
            continue
        start = verts[0] if (forward or len(verts) == 1) else verts[1]
        c = _vertex_coord(ents, start)
        if c is not None:
            pts.append(c)
    return _dedupe(pts)


def _dedupe(pts: list[Vec]) -> list[Vec]:
    out: list[Vec] = []
    for p in pts:
        if not out or math.dist(p, out[-1]) > 1e-6:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-6:
        out.pop()
    return out


def _plane_frame(ents: Ents, surf_id: int) -> tuple[Vec, Vec] | None:
    refs = _refs(ents[surf_id][1])
    if not refs or refs[0] not in ents:
        return None
    ax = refs[0]
    axrefs = _refs(ents[ax][1])
    if len(axrefs) < 2:
        return None
    origin = _coords(ents, axrefs[0])
    normal = _unit(_coords(ents, axrefs[1]))
    return origin, normal


def _planar_patches(ents: Ents) -> list[Patch]:
    patches: list[Patch] = []
    idx = 0
    for fid, (typ, body) in ents.items():
        if typ != "ADVANCED_FACE":
            continue
        surf = None
        bounds: list[tuple[str, int]] = []
        for r in _refs(body):
            if r not in ents:
                continue
            rt = ents[r][0]
            if rt == "PLANE":
                surf = r
            elif rt in ("FACE_OUTER_BOUND", "FACE_BOUND"):
                bounds.append((rt, r))
        if surf is None:
            continue
        frame = _plane_frame(ents, surf)
        if frame is None:
            continue
        origin, normal = frame
        outer: list[Vec] = []
        holes: list[list[Vec]] = []
        for rt, bid in bounds:
            loop = None
            for r in _refs(ents[bid][1]):
                if r in ents and ents[r][0] == "EDGE_LOOP":
                    loop = r
                    break
            if loop is None:
                continue
            ring = _loop_points(ents, loop)
            if len(ring) < 3:
                continue
            if rt == "FACE_OUTER_BOUND" and not outer:
                outer = ring
            else:
                holes.append(ring)
        if len(outer) < 3:
            # No outer wire recovered; still record if a single ring exists.
            if holes:
                outer = holes.pop(0)
            else:
                continue
        patches.append(Patch(idx, fid, origin, normal, outer, holes))
        idx += 1
    return patches


# --- bend parsing + adjacency -------------------------------------------------
def _bends(ents: Ents) -> list[Bend]:
    cyls = _cylinders(ents)
    out: list[Bend] = []
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
            if perp > 0.05:
                continue
            gauge = round(abs(ri - rj), 4)
            if 0.02 <= gauge <= 1.0:
                inside_r = min(ri, rj)
                axis_pt = li if ri <= rj else lj
                out.append(Bend(gauge, inside_r, ai, axis_pt))
    return out


def _attach_tangents(patches: list[Patch], bend: Bend) -> None:
    """Find, per patch, the straight outer-loop edge tangent to this bend."""
    r_lo = bend.inside_radius - max(0.15, 0.6 * bend.gauge)
    r_hi = bend.inside_radius + bend.gauge + max(0.15, 0.6 * bend.gauge)
    for patch in patches:
        best: tuple[float, tuple[Vec, Vec]] | None = None
        ring = patch.outer
        m = len(ring)
        for k in range(m):
            a = ring[k]
            b = ring[(k + 1) % m]
            e = _sub(b, a)
            length = _norm(e)
            if length < 1e-6:
                continue
            if abs(_dot(_unit(e), bend.axis)) < 0.985:
                continue  # edge not parallel to the bend axis
            mid = _scale(_add(a, b), 0.5)
            d = _perp_distance_to_axis(mid, bend.axis_pt, bend.axis)
            if not (r_lo <= d <= r_hi):
                continue
            score = abs(d - bend.inside_radius)
            if best is None or score < best[0]:
                best = (score, (a, b))
        if best is not None:
            bend.tangents[patch.idx] = best[1]


def build_graph(step_text: str) -> StepGraph:
    ents = parse_entities(step_text)
    patches = _planar_patches(ents)
    bends = _bends(ents)
    for b in bends:
        _attach_tangents(patches, b)

    types = {t for t, _ in ents.values()}
    non_dev = sorted(t for t in _NON_DEVELOPABLE if t in types)
    soft = sorted(t for t in _SOFT_CURVED if t in types)
    return StepGraph(patches=patches, bends=bends, non_developable=non_dev, soft_curved=soft)
