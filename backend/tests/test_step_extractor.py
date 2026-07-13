from pathlib import Path

from app.extractors.step_extractor import extract_step

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "synthetic-sig-strip.stp"


def test_reads_stock_thickness_and_bbox():
    geo = extract_step(FIXTURE)
    assert geo.point_count >= 8
    # thin strip: smallest extent is the 0.080 mm gauge (task #3 in the prompt)
    assert geo.stock_thickness_mm == 0.08
    # strip bounding box length is 300 mm
    assert geo.dimensions_mm is not None
    assert abs(max(geo.dimensions_mm) - 300.0) < 1e-6
    assert geo.features["bbox_length_mm"] == 300.0


def test_formed_part_does_not_fake_a_stock_gauge(tmp_path):
    # A chunky 3D envelope (formed contact) — smallest extent is NOT the gauge.
    p = tmp_path / "formed.stp"
    pts = [
        (0, 0, 0), (40, 0, 0), (40, 38, 0), (0, 38, 0),
        (0, 0, 37), (40, 0, 37), (40, 38, 37), (0, 38, 37),
    ]
    body = "\n".join(
        f"#{i}=CARTESIAN_POINT('',({x}.0,{y}.0,{z}.0));"
        for i, (x, y, z) in enumerate(pts, start=10)
    )
    p.write_text(f"DATA;\n{body}\nENDSEC;\n", encoding="utf-8")
    geo = extract_step(p)
    assert geo.is_sheet_like is False
    assert geo.stock_thickness_mm is None  # honest: no fake 37 mm gauge
    assert geo.min_extent_mm == 37.0
    assert any("Formed/3D" in w for w in geo.warnings)


def test_handles_missing_points_gracefully(tmp_path):
    p = tmp_path / "empty.stp"
    p.write_text("ISO-10303-21;\nDATA;\nENDSEC;\n", encoding="utf-8")
    geo = extract_step(p)
    assert geo.point_count == 0
    assert geo.stock_thickness_mm is None
    assert geo.warnings
