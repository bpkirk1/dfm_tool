"""Tests for the deterministic commentary generator (Phase 4)."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.commentary import build_commentary, build_commentary_markdown
from app.main import app
from app.report import RunInputs, build_report
from app.store.criteria_store import CriteriaStore

SEED = Path(__file__).resolve().parents[2] / "dfm-criteria.seed.yaml"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
client = TestClient(app)


def _store(tmp_path):
    s = CriteriaStore(tmp_path / "t.sqlite")
    s.sync_from_yaml(SEED)
    return s


def _section(sections, sid):
    return next((s for s in sections if s.id == sid), None)


def _counts(results):
    c = {"pass": 0, "flag": 0, "fail": 0, "manual": 0}
    for r in results:
        c[r["verdict"]] += 1
    return c


def _report(results, *, score=None, proposed=None, bands=None, corrections=None,
            thickness=None):
    return {
        "part_name": "P-TEST",
        "family": "stamping",
        "ruleset_version": "v-test",
        "criteria_version": 1,
        "summary": {
            "readiness_score": score,
            "counts": _counts(results),
            "results": results,
            "manual_check_parameters": [r["parameter"] for r in results
                                        if r["verdict"] == "manual"],
            "proposed": proposed or [],
        },
        "corrections": corrections or [],
        "thickness": thickness,
        "strip": None,
        "commentary_config": {"score_bands": bands} if bands else {},
    }


def _res(rule_id, parameter, verdict, **kw):
    base = {
        "rule_id": rule_id, "parameter": parameter, "operator": "lte", "measured": 1.0,
        "limit_applied": 0.5, "limit_detail": "<= 0.5", "verdict": verdict,
        "severity": "major", "source": "TEST-SRC", "supplier_adjustable": False,
        "units": None, "margin": -0.5, "note": "", "seen_count": None, "evidence": [],
        "marker": None, "consequence": None,
    }
    base.update(kw)
    return base


# --- structural tests ---------------------------------------------------------
def test_defect_example_has_critical_section_citing_source(tmp_path):
    store = _store(tmp_path)
    report = build_report(
        store, RunInputs(step_path=EXAMPLES / "bma_shield_defect.stp", family="stamping")
    )
    sections = build_commentary(report)
    crit = _section(sections, "critical")
    assert crit is not None and crit.paragraphs
    # at least one failed rule's source must appear in the narrative (provenance)
    fails = [r for r in report["summary"]["results"] if r["verdict"] == "fail"]
    assert fails
    blob = " ".join(crit.paragraphs)
    assert any(f["source"][:20] in blob for f in fails)
    store.close()


def test_clean_report_top_band_and_no_critical_section():
    results = [_res("A", "gap", "pass"), _res("B", "burr", "pass")]
    sections = build_commentary(_report(results, score=95.0))
    assert _section(sections, "critical") is None
    summary = _section(sections, "summary")
    assert "ready for tooling kickoff" in " ".join(summary.paragraphs)


def test_consequence_used_when_present_else_generic():
    results = [
        _res("WITH", "burr", "fail", severity="major",
             consequence="custom cracking risk phrase"),
        _res("WITHOUT", "gap", "fail", severity="major", consequence=None),
    ]
    crit = _section(build_commentary(_report(results, score=10.0)), "critical")
    blob = " ".join(crit.paragraphs).lower()
    assert "custom cracking risk phrase" in blob
    # generic major fallback for the rule lacking a consequence
    assert "significant manufacturability concern" in blob


def test_proposed_rules_only_in_proposed_section():
    results = [_res("ACTIVE", "burr", "fail")]
    proposed = [{"rule_id": "PROP-1", "parameter": "vcut", "operator": "eq",
                 "limit": "x", "severity": "minor", "source": "mined", "units": None,
                 "seen_count": None, "evidence": [], "consequence": None}]
    sections = build_commentary(_report(results, score=20.0, proposed=proposed))
    prop = _section(sections, "proposed")
    assert prop is not None
    assert any("PROP-1" in (it.get("text") or "") for it in prop.items)
    # PROP-1 must not leak into critical findings
    crit = _section(sections, "critical")
    assert "PROP-1" not in " ".join(crit.paragraphs)


def test_score_band_boundaries_from_config_honored():
    bands = [{"min": 80, "label": "BAND-HIGH"}, {"min": 0, "label": "BAND-LOW"}]
    hi = build_commentary(_report([_res("A", "g", "pass")], score=85.0, bands=bands))
    lo = build_commentary(_report([_res("A", "g", "pass")], score=50.0, bands=bands))
    assert "BAND-HIGH" in " ".join(_section(hi, "summary").paragraphs)
    assert "BAND-LOW" in " ".join(_section(lo, "summary").paragraphs)


def test_markdown_deterministic_apart_from_timestamp():
    results = [_res("A", "burr", "fail"), _res("B", "gap", "flag")]
    report = _report(results, score=40.0)
    s1 = build_commentary(report)
    s2 = build_commentary(report)
    md1 = build_commentary_markdown(s1, report, generated_at="2026-01-01T00:00:00+00:00")
    md2 = build_commentary_markdown(s2, report, generated_at="2026-01-01T00:00:00+00:00")
    assert md1 == md2


# --- endpoint smoke -----------------------------------------------------------
def test_commentary_md_endpoint_downloads():
    r = client.get("/commentary.md", params={"family": "stamping",
                                              "model": "bma_shield_defect.stp"})
    assert r.status_code == 200
    assert r.text.startswith("---")  # front-matter block
    assert "## Executive summary" in r.text
