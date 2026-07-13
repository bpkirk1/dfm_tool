from pathlib import Path

from app.models.criteria import load_criteria

SEED = Path(__file__).resolve().parents[2] / "dfm-criteria.seed.yaml"


def test_seed_yaml_loads_and_validates():
    cs = load_criteria(SEED)
    assert "stamping" in cs.process_families
    assert "molding" in cs.process_families
    assert cs.meta.ruleset_version  # versioned
    # no semantic problems in the shipped seed
    assert cs.validate_semantics() == []


def test_supplier_adjustable_rules_present():
    cs = load_criteria(SEED)
    stamping = cs.family("stamping")
    adjustable = [r for r in stamping.rules if r.supplier_adjustable]
    assert any(r.id == "STMP-MIN-PIERCE" for r in adjustable)


def test_form_angles_parsed():
    cs = load_criteria(SEED)
    stamping = cs.family("stamping")
    assert any(a.target == 135 for a in stamping.form_angles)
