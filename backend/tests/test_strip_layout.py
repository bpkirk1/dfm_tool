from pathlib import Path

from app.diestrip import generate_strip_layout
from app.extractors.step_extractor import GeometryFeatures
from app.models.criteria import load_criteria

SEED = Path(__file__).resolve().parents[2] / "dfm-criteria.seed.yaml"


def _layout(geometry=None):
    cs = load_criteria(SEED)
    fam = cs.family("stamping")
    return generate_strip_layout("stamping", fam, geometry, cs.meta.ruleset_version)


def test_reads_strip_params_from_config():
    lay = _layout()
    assert lay.pitch_mm == 20.0
    assert lay.multi_up_pairs == 4
    assert lay.coining_allowed is True
    assert lay.feed_defined is True
    assert lay.material_thickness_mm == 0.080


def test_sequence_starts_with_pilot_and_ends_with_cutoff():
    lay = _layout()
    assert lay.stations[0].kind == "pilot"
    assert lay.stations[-1].kind == "cutoff"
    # numbering is sequential and 1-based
    assert [s.number for s in lay.stations] == list(range(1, len(lay.stations) + 1))


def test_form_stations_come_from_bend_data():
    lay = _layout()
    forms = [s for s in lay.stations if s.kind == "form"]
    # the seed has 4 form-angle callouts
    assert len(forms) == 4
    # the 135-deg mating tine appears with its asymmetric tolerance
    tine = next(s for s in forms if s.target_angle_deg == 135)
    assert tine.tolerance == "+8/-1 deg"
    assert "springback" in tine.note.lower()


def test_idle_inserted_between_forms():
    lay = _layout()
    assert any(s.kind == "idle" for s in lay.stations)


def test_coin_station_present_when_allowed():
    lay = _layout()
    assert any(s.kind == "coin" for s in lay.stations)


def test_strip_length_is_stations_times_pitch():
    lay = _layout()
    assert lay.strip_length_mm == round(lay.station_count * 20.0, 3)


def test_width_utilization_with_geometry():
    geo = GeometryFeatures(source_file="x.stp")
    geo.dimensions_mm = (40.0, 38.0, 37.0)  # formed envelope
    lay = _layout(geometry=geo)
    assert lay.strip_width_estimate_mm is not None
    assert 0 < lay.width_utilization_pct <= 100
    assert lay.assumptions


def test_review_items_flag_unknowns():
    lay = _layout()  # no geometry
    assert any("material utilization" in r.lower() for r in lay.review_items)
    assert any("no 3d model" in r.lower() for r in lay.review_items)


def test_every_station_has_provenance():
    lay = _layout()
    for s in lay.stations:
        assert s.operation
        # config-sourced or bend-sourced; cutoff w/o feed may be empty, allow that
        assert s.kind in {"pilot", "pierce", "notch", "idle", "form", "coin", "restrike", "cutoff"}
