"""Kernel-free unfolding engine — honest by construction.

Develops a formed stamped part into its flat blank by rotating each planar patch
about its bend line into a common base plane, replacing every bend arc with its
**bend allowance** ``BA = angle * (R_inside + K * t)`` where ``K`` comes from
config (never code).

The honesty gate is the whole point: we only claim ``status = "ok"`` when the
topology is unambiguous — planar patches joined by simple coaxial cylindrical
bends forming a connected, acyclic graph, each bend joining exactly two patches,
and no non-developable (drawn/rolled/compound) surfaces. Anything else yields
``"partial"`` (we still show what we could develop) or ``"unavailable"`` with
explicit reasons, and the dependent flat-checks become manual items. We never
emit a silently-wrong flat pattern.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import stepgraph
from .stepgraph import Bend, Patch, StepGraph, Vec

Mat = tuple[tuple[float, float, float], ...]
_I: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


# --- small linear algebra -----------------------------------------------------
def _mv(m: Mat, v: Vec) -> Vec:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _mm(a: Mat, b: Mat) -> Mat:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _transpose(m: Mat) -> Mat:
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vec) -> Vec:
    n = _norm(a) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def _rodrigues(axis: Vec, angle: float) -> Mat:
    a = _unit(axis)
    c = math.cos(angle)
    s = math.sin(angle)
    t = 1.0 - c
    x, y, z = a
    return (
        (c + x * x * t, x * y * t - z * s, x * z * t + y * s),
        (y * x * t + z * s, c + y * y * t, y * z * t - x * s),
        (z * x * t - y * s, z * y * t + x * s, c + z * z * t),
    )


def _centroid(pts: list[Vec]) -> Vec:
    n = max(len(pts), 1)
    return (
        sum(p[0] for p in pts) / n,
        sum(p[1] for p in pts) / n,
        sum(p[2] for p in pts) / n,
    )


# --- placed-patch bookkeeping -------------------------------------------------
@dataclass
class _Placed:
    idx: int
    outer: list[Vec]
    holes: list[list[Vec]]
    normal: Vec
    # current = R * model + t  (rigid transform applied while unfolding)
    R: Mat = _I
    t: Vec = (0.0, 0.0, 0.0)


def _apply(node: _Placed, R: Mat, t: Vec) -> None:
    node.outer = [_add(_mv(R, p), t) for p in node.outer]
    node.holes = [[_add(_mv(R, p), t) for p in h] for h in node.holes]
    node.normal = _mv(R, node.normal)
    node.R = _mm(R, node.R)
    node.t = _add(_mv(R, node.t), t)


def _apply_bend(bend: Bend, R: Mat, t: Vec) -> None:
    bend.axis = _mv(R, bend.axis)
    bend.axis_pt = _add(_mv(R, bend.axis_pt), t)
    for k, (a, b) in list(bend.tangents.items()):
        bend.tangents[k] = (_add(_mv(R, a), t), _add(_mv(R, b), t))


# --- flat-pattern result ------------------------------------------------------
@dataclass
class PatchMeta:
    """A developed patch in the base 2D frame + the inverse map to model space."""

    idx: int
    poly2d: list[tuple[float, float]]
    holes2d: list[list[tuple[float, float]]]
    R: Mat
    t: Vec


@dataclass
class FlatPattern:
    status: str  # ok | partial | unavailable
    source_file: str = ""
    outline: list[list[list[float]]] = field(default_factory=list)
    cutouts: list[list[list[float]]] = field(default_factory=list)
    bend_lines: list[dict[str, Any]] = field(default_factory=list)
    developed_bbox_mm: list[float] | None = None
    k_factor_default: float | None = None
    developed_bend_count: int = 0
    assumptions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # Internal (base frame + placed patches) for checks/markers; not serialized.
    base_origin: Vec = (0.0, 0.0, 0.0)
    base_u: Vec = (1.0, 0.0, 0.0)
    base_v: Vec = (0.0, 1.0, 0.0)
    patches: list[PatchMeta] = field(default_factory=list)

    def flat_to_model(self, patch_idx: int, xy: tuple[float, float]) -> list[float] | None:
        """Map a developed 2D point on a given patch back to model 3D space."""
        meta = next((p for p in self.patches if p.idx == patch_idx), None)
        if meta is None:
            return None
        cur = _add(
            _add(self.base_origin, _scale(self.base_u, xy[0])),
            _scale(self.base_v, xy[1]),
        )
        model = _mv(_transpose(meta.R), _sub(cur, meta.t))
        return [round(model[0], 3), round(model[1], 3), round(model[2], 3)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_file": self.source_file,
            "outline": self.outline,
            "cutouts": self.cutouts,
            "bend_lines": self.bend_lines,
            "developed_bbox_mm": self.developed_bbox_mm,
            "k_factor_default": self.k_factor_default,
            "developed_bend_count": self.developed_bend_count,
            "assumptions": self.assumptions,
            "reasons": self.reasons,
        }


# --- config -------------------------------------------------------------------
def _resolve_config(flat_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(flat_cfg or {})
    cfg.setdefault("k_factor_default", 0.40)
    cfg.setdefault("k_factor_by_r_over_t", [])
    return cfg


def _k_factor(cfg: dict[str, Any], inside_radius: float, gauge: float) -> float:
    table = cfg.get("k_factor_by_r_over_t") or []
    if gauge > 1e-9 and table:
        r_over_t = inside_radius / gauge
        for row in sorted(table, key=lambda r: r.get("r_over_t_max", math.inf)):
            if r_over_t <= row.get("r_over_t_max", math.inf):
                return float(row.get("k", cfg["k_factor_default"]))
    return float(cfg["k_factor_default"])


# --- projection ---------------------------------------------------------------
def _project(pt: Vec, o: Vec, u: Vec, v: Vec) -> tuple[float, float]:
    d = _sub(pt, o)
    return (round(_dot(d, u), 4), round(_dot(d, v), 4))


# --- main entry ---------------------------------------------------------------
def develop_flat_pattern(
    step_text: str, flat_cfg: dict[str, Any] | None = None, source_file: str = ""
) -> FlatPattern:
    cfg = _resolve_config(flat_cfg)
    graph = stepgraph.build_graph(step_text)
    reasons: list[str] = []
    assumptions: list[str] = []

    if not graph.patches:
        return FlatPattern(
            status="unavailable",
            source_file=source_file,
            k_factor_default=cfg["k_factor_default"],
            reasons=[
                "No planar faces could be recovered from the STEP model — a flat "
                "pattern cannot be developed kernel-free."
            ],
        )

    if graph.non_developable:
        reasons.append(
            "Non-developable surfaces present ("
            + ", ".join(graph.non_developable)
            + ") — drawn/compound/rolled geometry cannot be unfolded without a CAD kernel."
        )
    if graph.soft_curved:
        assumptions.append(
            "Filleted corners / chamfers ("
            + ", ".join(graph.soft_curved)
            + ") are treated as sharp in the development."
        )

    # Classify bends by how many planar patches they cleanly touch. Count the
    # ambiguous cases and report them once (not one line per bend).
    usable: list[Bend] = []
    n_single = 0
    n_multi = 0
    for b in graph.bends:
        ids = b.patch_ids
        if len(ids) == 2:
            usable.append(b)
        elif len(ids) == 1:
            n_single += 1
        elif len(ids) > 2:
            n_multi += 1
    if n_single:
        reasons.append(
            f"{n_single} bend(s) matched only one planar patch — adjacency is "
            f"ambiguous, so {'that fold was' if n_single == 1 else 'those folds were'} "
            "not developed."
        )
    if n_multi:
        reasons.append(
            f"{n_multi} bend(s) matched more than two planar patches (likely a solid "
            "model carrying both sheet skins) — mid-surface reduction is needed; "
            f"{'that fold was' if n_multi == 1 else 'those folds were'} not developed."
        )

    patches = {p.idx: p for p in graph.patches}
    base_patch = max(graph.patches, key=lambda p: p.area())

    # Adjacency from usable bends only.
    adj: dict[int, list[tuple[Bend, int]]] = {p.idx: [] for p in graph.patches}
    for b in usable:
        i, j = b.patch_ids
        adj[i].append((b, j))
        adj[j].append((b, i))

    # Spanning tree (BFS) from the base patch; note cycles / disconnected folds.
    placed: dict[int, _Placed] = {}
    base_node = _Placed(
        idx=base_patch.idx,
        outer=list(base_patch.outer),
        holes=[list(h) for h in base_patch.holes],
        normal=base_patch.normal,
    )
    placed[base_patch.idx] = base_node

    tree_edges: list[tuple[int, int, Bend]] = []
    children: dict[int, list[tuple[int, Bend]]] = {p.idx: [] for p in graph.patches}
    queue = [base_patch.idx]
    seen = {base_patch.idx}
    cycle = False
    while queue:
        p = queue.pop(0)
        for bend, c in adj[p]:
            if c in seen:
                if (p, c) not in [(a, b) for a, b, _ in tree_edges] and (
                    c,
                    p,
                ) not in [(a, b) for a, b, _ in tree_edges]:
                    cycle = True
                continue
            seen.add(c)
            tree_edges.append((p, c, bend))
            children[p].append((c, bend))
            queue.append(c)

    if cycle:
        reasons.append(
            "The bend graph contains a closed loop — the part is not a simple "
            "acyclic fold sequence; it was not fully developed."
        )
    unreached = [p.idx for p in graph.patches if adj[p.idx] and p.idx not in seen]
    if unreached:
        reasons.append(
            "Some folded patches are not connected to the main body through simple "
            "bends and were left out of the development."
        )

    # Unfold: place each child by rotating its subtree flat + inserting BA.
    k_values: list[float] = []
    for parent_idx, child_idx, bend in tree_edges:
        child_patch = patches[child_idx]
        child_node = _Placed(
            idx=child_idx,
            outer=list(child_patch.outer),
            holes=[list(h) for h in child_patch.holes],
            normal=child_patch.normal,
        )
        placed[child_idx] = child_node
        k = _place_child(placed, parent_idx, child_idx, bend, children, patches, cfg)
        if k is not None:
            k_values.append(k)

    # Base 2D frame.
    o0 = base_node.outer[0]
    n0 = _unit(base_node.normal)
    u0 = _unit(_sub(base_node.outer[1], base_node.outer[0]))
    if abs(_dot(u0, n0)) > 0.9:  # degenerate first edge; pick another
        u0 = _unit(_cross(n0, (1.0, 0.0, 0.0)))
        if _norm(u0) < 1e-6:
            u0 = _unit(_cross(n0, (0.0, 1.0, 0.0)))
    u0 = _unit(_sub(u0, _scale(n0, _dot(u0, n0))))  # re-orthogonalize to plane
    v0 = _unit(_cross(n0, u0))

    metas: list[PatchMeta] = []
    outline: list[list[list[float]]] = []
    cutouts: list[list[list[float]]] = []
    for node in placed.values():
        poly2d = [_project(p, o0, u0, v0) for p in node.outer]
        holes2d = [[_project(p, o0, u0, v0) for p in h] for h in node.holes]
        metas.append(PatchMeta(node.idx, poly2d, holes2d, node.R, node.t))
        outline.append([[x, y] for x, y in poly2d])
        for h in holes2d:
            cutouts.append([[x, y] for x, y in h])

    bbox = _bbox2d([m.poly2d for m in metas])
    developed_bbox = None
    if bbox:
        developed_bbox = [round(bbox[2] - bbox[0], 3), round(bbox[3] - bbox[1], 3)]

    bend_lines = _bend_lines(tree_edges, placed, patches, cfg, o0, u0, v0)

    single_flat = not usable and not graph.bends
    if single_flat:
        assumptions.append(
            "No bends detected — the part is treated as an already-flat blank "
            "(largest planar face)."
        )

    # "Nothing meaningful developed": there was something to fold (bends and/or
    # non-developable surfaces) but no fold could be resolved, so all that remains
    # is the base face on its own. That is not a trustworthy blank — report it as
    # unavailable rather than showing a misleading sliver.
    nothing_developed = len(tree_edges) == 0 and not single_flat

    if not reasons and (single_flat or len(tree_edges) == len(usable)):
        status = "ok"
    elif nothing_developed or not metas:
        status = "unavailable"
    else:
        status = "partial"

    if status == "ok" and usable:
        assumptions.append(
            f"Developed {len(tree_edges)} bend(s) about their bend lines; each arc "
            f"replaced by its bend allowance (neutral-axis K from config)."
        )

    if nothing_developed:
        reasons.append(
            "No developable blank could be produced — no fold resolved cleanly, so "
            "only an isolated planar face remained. A full CAD kernel is required to "
            "flatten this part."
        )
        # Drop the misleading single-face geometry; the reasons list stands in.
        outline, cutouts, bend_lines, metas = [], [], [], []
        developed_bbox = None

    reasons = _dedup(reasons)

    return FlatPattern(
        status=status,
        source_file=source_file,
        outline=outline,
        cutouts=cutouts,
        bend_lines=bend_lines,
        developed_bbox_mm=developed_bbox,
        k_factor_default=cfg["k_factor_default"],
        developed_bend_count=len(tree_edges),
        assumptions=assumptions,
        reasons=reasons,
        base_origin=o0,
        base_u=u0,
        base_v=v0,
        patches=metas,
    )


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving de-duplication (defensive; the counted reasons above
    already avoid the common repeats)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _subtree(root: int, children: dict[int, list[tuple[int, Bend]]]) -> set[int]:
    out = {root}
    stack = [root]
    while stack:
        n = stack.pop()
        for c, _b in children.get(n, []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _place_child(
    placed: dict[int, _Placed],
    parent_idx: int,
    child_idx: int,
    bend: Bend,
    children: dict[int, list[tuple[int, Bend]]],
    patches: dict[int, Patch],
    cfg: dict[str, Any],
) -> float | None:
    parent = placed[parent_idx]
    child = placed[child_idx]
    if parent_idx not in bend.tangents or child_idx not in bend.tangents:
        return None

    nP = _unit(parent.normal)
    nC = _unit(child.normal)
    theta = math.acos(max(-1.0, min(1.0, _dot(nP, nC))))

    # The parent tangent is the fold line and must NOT move (parent stays put).
    pa, pb = bend.tangents[parent_idx]
    ca0, cb0 = bend.tangents[child_idx]

    # Choose the rotation sense that makes the child coplanar with the parent.
    best_R = _I
    if theta > 1e-4:
        best_dot = -2.0
        for sign in (1.0, -1.0):
            R = _rodrigues(bend.axis, sign * theta)
            d = _dot(_mv(R, nC), nP)
            if d > best_dot:
                best_dot = d
                best_R = R
    R1 = best_R
    q0 = bend.axis_pt
    t1 = _sub(q0, _mv(R1, q0))

    # Rotate the child subtree (and its internal bends) flat.
    subtree = _subtree(child_idx, children)
    subtree.discard(parent_idx)
    for idx in subtree:
        _apply(placed[idx], R1, t1)
    for _p, _c, b in _edges_within(subtree, children):
        _apply_bend(b, R1, t1)

    # Rotated child tangent (computed locally so we never move the parent side).
    ca = _add(_mv(R1, ca0), t1)
    cb = _add(_mv(R1, cb0), t1)

    # Insert the bend allowance: shift the child subtree outward so the developed
    # gap between the parent and child tangent edges equals BA.
    T = _unit(_sub(pb, pa))
    N = _unit(_cross(nP, T))
    p_cent = _centroid(parent.outer)
    c_cent = _centroid(child.outer)  # already rotated
    if _dot(N, _sub(c_cent, p_cent)) < 0:
        N = _scale(N, -1.0)

    mid_p = _scale(_add(pa, pb), 0.5)
    mid_c = _scale(_add(ca, cb), 0.5)
    gap = _dot(_sub(mid_c, mid_p), N)

    k = _k_factor(cfg, bend.inside_radius, bend.gauge)
    ba = theta * (bend.inside_radius + k * bend.gauge)
    shift = _scale(N, ba - gap)
    for idx in subtree:
        _apply(placed[idx], _I, shift)
    for _p, _c, b in _edges_within(subtree, children):
        _apply_bend(b, _I, shift)

    bend.developed_angle_rad = theta
    bend.developed_ba = ba
    bend.developed_k = k
    return k


def _edges_within(nodes: set[int], children: dict[int, list[tuple[int, Bend]]]):
    for p, kids in children.items():
        if p not in nodes:
            continue
        for c, b in kids:
            if c in nodes:
                yield p, c, b


def _bend_lines(tree_edges, placed, patches, cfg, o0, u0, v0) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for parent_idx, child_idx, bend in tree_edges:
        if parent_idx not in bend.tangents:
            continue
        pa, pb = bend.tangents[parent_idx]
        # Use the angle/allowance recorded at placement (the flattened normals are
        # coplanar now, so recomputing from them would read ~0 degrees).
        theta = bend.developed_angle_rad or 0.0
        k = bend.developed_k if bend.developed_k is not None else _k_factor(
            cfg, bend.inside_radius, bend.gauge
        )
        ba = bend.developed_ba if bend.developed_ba is not None else theta * (
            bend.inside_radius + k * bend.gauge
        )
        p1 = _project(pa, o0, u0, v0)
        p2 = _project(pb, o0, u0, v0)
        lines.append(
            {
                "p1": [p1[0], p1[1]],
                "p2": [p2[0], p2[1]],
                "angle_deg": round(math.degrees(theta), 2),
                "inside_radius_mm": round(bend.inside_radius, 3),
                "gauge_mm": round(bend.gauge, 3),
                "k": k,
                "bend_allowance_mm": round(ba, 4),
            }
        )
    return lines


def _bbox2d(polys: list[list[tuple[float, float]]]):
    pts = [p for poly in polys for p in poly]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
