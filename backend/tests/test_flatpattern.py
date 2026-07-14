"""Phase 7 — flat-pattern module tests.

Tiny hand-built STEP fixtures (a ``_StepBuilder`` emits exactly the entities the
kernel-free parser reads: planar ADVANCED_FACEs with ordered EDGE_LOOPs, plus
coaxial CYLINDRICAL_SURFACE pairs for bends). This keeps the tests self-contained
and deterministic while exercising the real unfolding + check + render paths.
"""
from __future__ import annotations

import math

import pytest

from app.flatpattern import (
    compute_flat_features,
    develop_flat_pattern,
    render_dxf,
    render_svg,
)
from app.flatpattern.unfold import FlatPattern, PatchMeta

_K_TABLE = {
    "k_factor_default": 0.40,
    "k_factor_by_r_over_t": [
        {"r_over_t_max": 1.0, "k": 0.35},
        {"r_over_t_max": 3.0, "k": 0.40},
        {"r_over_t_max": 999, "k": 0.45},
    ],
}


class _StepBuilder:
    def __init__(self) -> None:
        self._id = 0
        self.lines: list[str] = []

    def _n(self) -> int:
        self._id += 1
        return self._id

    def _emit(self, body: str) -> int:
        i = self._n()
        self.lines.append(f"#{i}={body};")
        return i

    def point(self, x: float, y: float, z: float) -> int:
        return self._emit(f"CARTESIAN_POINT('',({x},{y},{z}))")

    def direction(self, x: float, y: float, z: float) -> int:
        return self._emit(f"DIRECTION('',({x},{y},{z}))")

    def _loop(self, verts: list[int]) -> int:
        vpts = [self._emit(f"VERTEX_POINT('',#{v})") for v in verts]
        oes = []
        n = len(vpts)
        for k in range(n):
            v1 = vpts[k]
            v2 = vpts[(k + 1) % n]
            # curve ref points at a non-vertex entity (the first vertex's point);
            # the parser only reads the two VERTEX_POINT refs from an EDGE_CURVE.
            ec = self._emit(f"EDGE_CURVE('',#{v1},#{v2},#{verts[0]},.T.)")
            oes.append(self._emit(f"ORIENTED_EDGE('',*,*,#{ec},.T.)"))
        refs = ",".join(f"#{o}" for o in oes)
        return self._emit(f"EDGE_LOOP('',({refs}))")

    def plane_face(self, outer_pts, normal, holes=None) -> int:
        origin = self.point(*outer_pts[0])
        axis = self.direction(*normal)
        ref = self.direction(*_ref_dir(normal))
        a2 = self._emit(f"AXIS2_PLACEMENT_3D('',#{origin},#{axis},#{ref})")
        plane = self._emit(f"PLANE('',#{a2})")
        overts = [self.point(*p) for p in outer_pts]
        oloop = self._loop(overts)
        bound = self._emit(f"FACE_OUTER_BOUND('',#{oloop},.T.)")
        bounds = [bound]
        for hole in holes or []:
            hverts = [self.point(*p) for p in hole]
            hloop = self._loop(hverts)
            bounds.append(self._emit(f"FACE_BOUND('',#{hloop},.T.)"))
        blist = ",".join(f"#{b}" for b in bounds)
        return self._emit(f"ADVANCED_FACE('',({blist}),#{plane},.T.)")

    def cylinder(self, loc, axis, radius) -> int:
        p = self.point(*loc)
        d = self.direction(*axis)
        r = self.direction(*_ref_dir(axis))
        a2 = self._emit(f"AXIS2_PLACEMENT_3D('',#{p},#{d},#{r})")
        return self._emit(f"CYLINDRICAL_SURFACE('',#{a2},{radius})")

    def raw(self, body: str) -> int:
        return self._emit(body)

    def text(self) -> str:
        return "ISO-10303-21;\nDATA;\n" + "\n".join(self.lines) + "\nENDSEC;\n"


def _ref_dir(normal):
    # any unit vector not parallel to the normal
    if abs(normal[0]) < 0.9:
        return (1.0, 0.0, 0.0)
    return (0.0, 1.0, 0.0)


def _bracket_step() -> str:
    """A single 90-degree bend: horizontal base (x0..9) + vertical leg (z1..9)."""
    b = _StepBuilder()
    # base: z=0, top face normal +z, x in [0,9], y in [0,5]
    b.plane_face(
        [(0.0, 0.0, 0.0), (9.0, 0.0, 0.0), (9.0, 5.0, 0.0), (0.0, 5.0, 0.0)],
        (0.0, 0.0, 1.0),
    )
    # leg: plane x=10, material-top face normal -x, z in [1,9]
    b.plane_face(
        [(10.0, 0.0, 1.0), (10.0, 5.0, 1.0), (10.0, 5.0, 9.0), (10.0, 0.0, 9.0)],
        (-1.0, 0.0, 0.0),
    )
    # coaxial inner/outer cylinders (bend): axis along Y at (9,0,1), R=1.0 / 1.5
    b.cylinder((9.0, 0.0, 1.0), (0.0, 1.0, 0.0), "1.0")
    b.cylinder((9.0, 0.0, 1.0), (0.0, 1.0, 0.0), "1.5")
    return b.text()


def _flat_blank_with_web() -> str:
    b = _StepBuilder()
    b.plane_face(
        [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (20.0, 10.0, 0.0), (0.0, 10.0, 0.0)],
        (0.0, 0.0, 1.0),
        holes=[
            [(5.0, 3.0, 0.0), (9.0, 3.0, 0.0), (9.0, 7.0, 0.0), (5.0, 7.0, 0.0)],
            [(9.1, 3.0, 0.0), (13.0, 3.0, 0.0), (13.0, 7.0, 0.0), (9.1, 7.0, 0.0)],
        ],
    )
    return b.text()


def _flat_blank_plain() -> str:
    b = _StepBuilder()
    b.plane_face(
        [(0.0, 0.0, 0.0), (12.0, 0.0, 0.0), (12.0, 6.0, 0.0), (0.0, 6.0, 0.0)],
        (0.0, 0.0, 1.0),
    )
    return b.text()


def _drawn_feature_step() -> str:
    """Flat face plus a non-developable spline surface => topology unresolvable."""
    b = _StepBuilder()
    b.plane_face(
        [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 6.0, 0.0), (0.0, 6.0, 0.0)],
        (0.0, 0.0, 1.0),
    )
    b.raw("B_SPLINE_SURFACE_WITH_KNOTS('',3,3,((#1,#1),(#1,#1)),.UNSPECIFIED.)")
    return b.text()


# --- 1. single 90-degree bend: developed length = legs + BA, config-driven -----
def test_single_bend_developed_length_and_config_driven():
    step = _bracket_step()
    fp = develop_flat_pattern(step, _K_TABLE)
    assert fp.status == "ok"
    assert fp.developed_bend_count == 1
    assert len(fp.bend_lines) == 1
    assert fp.bend_lines[0]["angle_deg"] == pytest.approx(90.0, abs=0.5)

    # r/t = 1.0/0.5 = 2 -> K=0.40 from the table
    ba = (math.pi / 2) * (1.0 + 0.40 * 0.5)
    expected = 9.0 + ba + 8.0
    assert max(fp.developed_bbox_mm) == pytest.approx(expected, abs=0.05)
    assert fp.bend_lines[0]["bend_allowance_mm"] == pytest.approx(ba, abs=0.01)

    # Config-driven proof: changing the K-factor changes the developed length.
    lo = develop_flat_pattern(step, {"k_factor_default": 0.30, "k_factor_by_r_over_t": []})
    hi = develop_flat_pattern(step, {"k_factor_default": 0.50, "k_factor_by_r_over_t": []})
    w_lo = max(lo.developed_bbox_mm)
    w_hi = max(hi.developed_bbox_mm)
    assert w_hi > w_lo
    assert w_lo == pytest.approx(17.0 + (math.pi / 2) * (1.0 + 0.30 * 0.5), abs=0.05)
    assert w_hi == pytest.approx(17.0 + (math.pi / 2) * (1.0 + 0.50 * 0.5), abs=0.05)


# --- 2. two cutouts with a known narrow web -----------------------------------
def test_narrow_web_between_cutouts_fails():
    fp = develop_flat_pattern(_flat_blank_with_web(), _K_TABLE)
    assert fp.status == "ok"
    features, details = compute_flat_features(fp)
    assert features["flat_min_web_mm"] == pytest.approx(0.1, abs=0.01)
    assert features["flat_min_web_mm"] < 0.12  # below the seeded limit -> fails
    assert details["min_web"]["value_mm"] == pytest.approx(0.1, abs=0.01)


# --- 3. drawn/unresolvable topology -> partial, checks manual -----------------
def test_drawn_feature_is_partial_and_manual():
    fp = develop_flat_pattern(_drawn_feature_step(), _K_TABLE)
    assert fp.status == "partial"
    assert fp.reasons
    assert any("developable" in r.lower() for r in fp.reasons)
    features, details = compute_flat_features(fp)
    # Every flat feature None => the engine returns "manual", never a silent pass.
    assert all(v is None for v in features.values())
    assert details["reasons"]


# --- 4. overlap => blocker measurement > 0 ------------------------------------
def test_overlapping_patches_flagged():
    fp = FlatPattern(status="ok")
    fp.patches = [
        PatchMeta(0, [(0, 0), (4, 0), (4, 4), (0, 4)], [], ((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0, 0, 0)),
        PatchMeta(1, [(2, 2), (6, 2), (6, 6), (2, 6)], [], ((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0, 0, 0)),
    ]
    features, _ = compute_flat_features(fp)
    assert features["flat_patch_overlap_mm"] > 0.0


# --- 5. already-flat part passes through unchanged -----------------------------
def test_already_flat_blank():
    fp = develop_flat_pattern(_flat_blank_plain(), _K_TABLE)
    assert fp.status == "ok"
    assert fp.developed_bend_count == 0
    assert fp.developed_bbox_mm is not None
    assert max(fp.developed_bbox_mm) == pytest.approx(12.0, abs=0.05)


# --- 6. deterministic SVG / DXF export ----------------------------------------
def test_svg_is_deterministic():
    fp = develop_flat_pattern(_bracket_step(), _K_TABLE)
    a = render_svg(fp)
    b = render_svg(fp)
    assert a == b
    assert a.startswith("<svg")


def test_dxf_is_deterministic_or_skipped():
    fp = develop_flat_pattern(_bracket_step(), _K_TABLE)
    a = render_dxf(fp)
    if a is None:
        pytest.skip("ezdxf not installed — DXF export degrades gracefully")
    b = render_dxf(fp)
    assert a == b
    assert b"OUTLINE" in a


# --- 7. flat-check verdicts flow through the real rule engine ------------------
def test_flat_checks_flow_through_engine():
    from app import config
    from app.engine import evaluate_family
    from app.models.criteria import load_criteria

    criteria = load_criteria(config.CRITERIA_SEED_PATH)
    fam = criteria.family("stamping")
    fp = develop_flat_pattern(_flat_blank_with_web(), getattr(fam, "flat_pattern", None))
    features, _ = compute_flat_features(fp)
    summary = evaluate_family("stamping", fam, features)
    verdicts = {r.rule_id: r.verdict for r in summary.results}
    assert verdicts["STMP-FLAT-MIN-WEB"] == "fail"          # 0.1 mm < 0.12 mm
    assert verdicts["STMP-FLAT-OVERLAP"] != "fail"          # no overlap (0 mm)
