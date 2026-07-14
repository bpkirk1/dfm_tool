from app.engine.evaluator import evaluate_family
from app.models.criteria import ProcessFamily, Scoring


def _family():
    return ProcessFamily(
        rules=[
            {"id": "R-FAIL", "parameter": "burr", "operator": "lte", "limit": 0.04,
             "severity": "major", "source": "Note 2"},
            {"id": "R-PASS", "parameter": "gap", "operator": "gte", "limit": 0.10,
             "severity": "minor", "source": "Note 5"},
            {"id": "R-PLACEHOLDER", "parameter": "pierce", "operator": "gte",
             "limit": 0.15, "severity": "blocker", "source": "supplier cap",
             "supplier_adjustable": True,
             "capability": {"achieved_min": None, "cpk": None, "confirmed": False}},
            {"id": "R-MANUAL", "parameter": "draft", "operator": "lte", "limit": 1.0,
             "severity": "major", "source": "Sht4"},
        ]
    )


def test_verdicts_pass_fail_flag_manual():
    fam = _family()
    features = {"burr": 0.06, "gap": 0.20, "pierce": 0.30}  # draft not measured
    summary = evaluate_family("stamping", fam, features, "v-test")
    by_id = {r.rule_id: r for r in summary.results}

    assert by_id["R-FAIL"].verdict == "fail"
    assert by_id["R-PASS"].verdict == "pass"
    # passes the placeholder but unconfirmed -> flag, with provenance note
    assert by_id["R-PLACEHOLDER"].verdict == "flag"
    assert "placeholder" in by_id["R-PLACEHOLDER"].note.lower()
    # not measured -> manual
    assert by_id["R-MANUAL"].verdict == "manual"
    assert "draft" in summary.manual_check_parameters


def test_every_result_has_provenance():
    summary = evaluate_family("stamping", _family(), {}, "v-test")
    for r in summary.results:
        assert r.rule_id
        assert r.source  # no unexplained verdicts


def test_confirmed_capability_overrides_placeholder():
    fam = ProcessFamily(
        rules=[
            {"id": "R", "parameter": "pierce", "operator": "gte", "limit": 0.15,
             "severity": "blocker", "source": "cap", "supplier_adjustable": True,
             "capability": {"achieved_min": 0.18, "cpk": 1.4, "confirmed": True}},
        ]
    )
    # measured 0.16 passes seeded 0.15 but FAILS confirmed capability 0.18
    summary = evaluate_family("stamping", fam, {"pierce": 0.16}, "v")
    assert summary.results[0].verdict == "fail"
    assert summary.results[0].limit_applied == 0.18


def test_score_is_zero_to_hundred():
    summary = evaluate_family("stamping", _family(), {"burr": 0.06, "gap": 0.20, "pierce": 0.30}, "v")
    assert summary.readiness_score is not None
    assert 0.0 <= summary.readiness_score <= 100.0


def _marginal_family():
    # A single gte rule; a measured value just above the limit lands in the flag
    # band only when marginal_fraction is wide enough.
    return ProcessFamily(
        rules=[
            {"id": "R", "parameter": "gap", "operator": "gte", "limit": 1.0,
             "severity": "major", "source": "Note"},
        ]
    )


def test_missing_scoring_falls_back_to_defaults():
    # No scoring passed -> historical defaults. margin 0.05 on limit 1.0 is within
    # the default 10% band -> flag.
    summary = evaluate_family("stamping", _marginal_family(), {"gap": 1.05}, "v")
    assert summary.results[0].verdict == "flag"
    assert "marginal" in summary.results[0].note.lower()


def test_custom_scoring_block_is_honored():
    fam = _marginal_family()
    # Tighten the marginal band to ~1% -> the same 0.05 margin is no longer
    # marginal, so the verdict is a clean pass. Config changed behavior, no code.
    tight = Scoring(marginal_fraction=0.01)
    summary = evaluate_family("stamping", fam, {"gap": 1.05}, "v", scoring=tight)
    assert summary.results[0].verdict == "pass"

    # Widen it far enough that 0.05 is inside the band -> flag.
    wide = Scoring(marginal_fraction=0.10)
    summary = evaluate_family("stamping", fam, {"gap": 1.05}, "v", scoring=wide)
    assert summary.results[0].verdict == "flag"


def test_custom_verdict_credit_changes_score():
    fam = _marginal_family()
    # A flag normally earns half credit (score 50). Give a flag full credit and
    # the score climbs to 100 — purely config-driven.
    summary = evaluate_family("stamping", fam, {"gap": 1.05}, "v")
    assert summary.results[0].verdict == "flag"
    base = summary.readiness_score

    generous = Scoring(verdict_credit={"pass": 1.0, "flag": 1.0, "fail": 0.0})
    summary2 = evaluate_family("stamping", fam, {"gap": 1.05}, "v", scoring=generous)
    assert summary2.readiness_score > base
