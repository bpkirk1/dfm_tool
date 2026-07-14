"""Unit tests for extractors/thickness.py — the kernel-free stock-gauge analysis.

Tiny hand-built STEP fixtures (a compact ``_StepBuilder`` emits exactly the
entities the parser reads: planar ADVANCED_FACEs with ordered EDGE_LOOPs and
coaxial CYLINDRICAL_SURFACE pairs for bends).
"""
from __future__ import annotations

from app.extractors.thickness import (
    _coaxial_bends,
    _wall_samples,
    _planar_faces,
    analyze_thickness,
    parse_entities,
)


def _ref_dir(normal):
    return (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)


class _StepBuilder:
    def __init__(self) -> None:
        self._id = 0
        self.lines: list[str] = []

    def _emit(self, body: str) -> int:
        self._id += 1
        self.lines.append(f"#{self._id}={body};")
        return self._id

    def point(self, x, y, z) -> int:
        return self._emit(f"CARTESIAN_POINT('',({x},{y},{z}))")

    def direction(self, x, y, z) -> int:
        return self._emit(f"DIRECTION('',({x},{y},{z}))")

    def _loop(self, verts: list[int]) -> int:
        vpts = [self._emit(f"VERTEX_POINT('',#{v})") for v in verts]
        oes = []
        n = len(vpts)
        for k in range(n):
            ec = self._emit(
                f"EDGE_CURVE('',#{vpts[k]},#{vpts[(k + 1) % n]},#{verts[0]},.T.)"
            )
            oes.append(self._emit(f"ORIENTED_EDGE('',*,*,#{ec},.T.)"))
        refs = ",".join(f"#{o}" for o in oes)
        return self._emit(f"EDGE_LOOP('',({refs}))")

    def plane_face(self, outer_pts, normal) -> int:
        origin = self.point(*outer_pts[0])
        axis = self.direction(*normal)
        ref = self.direction(*_ref_dir(normal))
        a2 = self._emit(f"AXIS2_PLACEMENT_3D('',#{origin},#{axis},#{ref})")
        plane = self._emit(f"PLANE('',#{a2})")
        overts = [self.point(*p) for p in outer_pts]
        oloop = self._loop(overts)
        bound = self._emit(f"FACE_OUTER_BOUND('',#{oloop},.T.)")
        return self._emit(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)")

    def cylinder(self, loc, axis, radius) -> int:
        p = self.point(*loc)
        d = self.direction(*axis)
        r = self.direction(*_ref_dir(axis))
        a2 = self._emit(f"AXIS2_PLACEMENT_3D('',#{p},#{d},#{r})")
        return self._emit(f"CYLINDRICAL_SURFACE('',#{a2},{radius})")

    def text(self) -> str:
        return "ISO-10303-21;\nDATA;\n" + "\n".join(self.lines) + "\nENDSEC;\n"


def _wall_pair(b: _StepBuilder, x0: float, gap: float, size: float = 4.0) -> None:
    """Two stacked parallel planes (normal +z) separated by `gap` — one flat wall."""
    b.plane_face(
        [(x0, 0.0, 0.0), (x0 + size, 0.0, 0.0), (x0 + size, size, 0.0), (x0, size, 0.0)],
        (0.0, 0.0, 1.0),
    )
    b.plane_face(
        [(x0, 0.0, gap), (x0 + size, 0.0, gap), (x0 + size, size, gap), (x0, size, gap)],
        (0.0, 0.0, 1.0),
    )


def test_coaxial_bends_finds_inner_outer_pair():
    b = _StepBuilder()
    # inner R1.0 / outer R1.08 sharing the Y axis at (9,0,1) -> gauge 0.08
    b.cylinder((9.0, 0.0, 1.0), (0.0, 1.0, 0.0), "1.0")
    b.cylinder((9.0, 0.0, 1.0), (0.0, 1.0, 0.0), "1.08")
    ents = parse_entities(b.text())
    bends = _coaxial_bends(ents)
    assert len(bends) == 1
    gauge, inside_radius, _loc = bends[0]
    assert abs(gauge - 0.08) < 1e-6
    assert abs(inside_radius - 1.0) < 1e-6


def test_wall_samples_pairs_stacked_planes():
    b = _StepBuilder()
    _wall_pair(b, x0=0.0, gap=0.08)
    ents = parse_entities(b.text())
    faces = _planar_faces(ents)
    assert len(faces) == 2
    walls = _wall_samples(faces)
    assert walls, "expected the two stacked planes to pair into a wall sample"
    assert abs(walls[0][0] - 0.08) < 1e-6


def test_analyze_thickness_reports_gauge():
    b = _StepBuilder()
    b.cylinder((9.0, 0.0, 1.0), (0.0, 1.0, 0.0), "1.0")
    b.cylinder((9.0, 0.0, 1.0), (0.0, 1.0, 0.0), "1.08")
    _wall_pair(b, x0=0.0, gap=0.08)
    res = analyze_thickness(parse_entities(b.text()))
    assert res is not None
    assert abs(res["expected_thickness_mm"] - 0.08) < 1e-6
    assert res["uniformity_status"] in {"uniform", "anomalies", "inconclusive"}


def test_robustness_gate_inconclusive_on_feature_rich_input():
    b = _StepBuilder()
    # Establish a thin stock gauge (0.08) from a couple of bends.
    b.cylinder((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), "1.0")
    b.cylinder((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), "1.08")
    # Then spray many thick, well-separated "walls" (gap 0.5 >> 2*gauge) so the
    # off-gauge count blows past the robustness gate -> inconclusive, not a flood
    # of false anomalies.
    for k in range(12):
        _wall_pair(b, x0=20.0 * k, gap=0.5, size=4.0)
    res = analyze_thickness(parse_entities(b.text()))
    assert res is not None
    assert res["uniformity_status"] == "inconclusive"
    assert res["inconsistencies"] == []
    # inconclusive still passes the DFM rule (no false blocker)
    assert res["consistent"] is True


def test_malformed_records_do_not_crash_parse():
    text = (
        "ISO-10303-21;\nDATA;\n"
        "#1=CARTESIAN_POINT('',());\n"                # empty tuple
        "#2=CYLINDRICAL_SURFACE('',#999,);\n"          # dangling ref, no radius
        "#3=CARTESIAN_POINT('',(1.0,2.0,3.0));\n"
        "garbage line without id\n"
        "ENDSEC;\n"
    )
    ents = parse_entities(text)  # must not raise
    assert 3 in ents
    # analyze tolerates the junk and simply finds nothing to measure
    assert analyze_thickness(ents) is None
