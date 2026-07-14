"""Tests for the versioned SQLite criteria store (uses a tmp-path DB)."""
from __future__ import annotations

import textwrap

import pytest

from app.store.criteria_store import CriteriaStore

_V1 = textwrap.dedent(
    """
    meta:
      schema_version: "1.0"
      ruleset_version: "t1"
    process_families:
      stamping:
        rules:
          - {id: A, parameter: burr, operator: lte, limit: 0.04, severity: major, source: N2}
          - id: B
            parameter: pierce
            operator: gte
            limit: 0.15
            severity: blocker
            source: cap
            supplier_adjustable: true
            capability: {achieved_min: null, cpk: null, confirmed: false}
    """
).strip()

# v2: change A's limit, flip B's capability to confirmed, and add C.
_V2 = textwrap.dedent(
    """
    meta:
      schema_version: "1.0"
      ruleset_version: "t2"
    process_families:
      stamping:
        rules:
          - {id: A, parameter: burr, operator: lte, limit: 0.03, severity: major, source: N2}
          - id: B
            parameter: pierce
            operator: gte
            limit: 0.15
            severity: blocker
            source: cap
            supplier_adjustable: true
            capability: {achieved_min: 0.18, cpk: 1.5, confirmed: true}
          - {id: C, parameter: gap, operator: gte, limit: 0.10, severity: minor, source: N5}
    """
).strip()

_INVALID = textwrap.dedent(
    """
    meta: {ruleset_version: bad}
    process_families:
      stamping:
        rules:
          - {id: X, parameter: p, operator: NOT_AN_OPERATOR, limit: 1, severity: major, source: s}
    """
).strip()


@pytest.fixture()
def store(tmp_path):
    s = CriteriaStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def _seed(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_sync_from_yaml_imports_once_then_idempotent(tmp_path, store):
    yaml_path = _seed(tmp_path, "v1.yaml", _V1)
    first = store.sync_from_yaml(yaml_path)
    assert first["changed"] is True
    # unchanged file -> hash gate short-circuits, no new version
    second = store.sync_from_yaml(yaml_path)
    assert second["changed"] is False
    assert second["version_id"] == first["version_id"]
    assert len(store.list_versions()) == 1


def test_sync_creates_new_version_when_yaml_changes(tmp_path, store):
    p = _seed(tmp_path, "v.yaml", _V1)
    store.sync_from_yaml(p)
    p.write_text(_V2, encoding="utf-8")
    res = store.sync_from_yaml(p)
    assert res["changed"] is True
    assert len(store.list_versions()) == 2


def test_save_version_rejects_invalid_ruleset(store):
    with pytest.raises(ValueError):
        store.save_version(_INVALID, author="t", reason="t")
    assert store.list_versions() == []


def test_diff_versions_added_removed_changed(store):
    a = store.save_version(_V1, author="t", reason="v1")
    b = store.save_version(_V2, author="t", reason="v2")
    diff = store.diff_versions(a, b)
    assert "C" in diff["added"]
    assert diff["removed"] == []
    changed_ids = {c["rule_id"] for c in diff["changed"]}
    # A changed on limit; B changed on capability.confirmed (item 6 completeness)
    assert "A" in changed_ids
    assert "B" in changed_ids
    b_change = next(c for c in diff["changed"] if c["rule_id"] == "B")
    assert b_change["from"]["capability_confirmed"] is False
    assert b_change["to"]["capability_confirmed"] is True
    assert b_change["to"]["capability_achieved_min"] == 0.18


def test_diff_missing_version_raises_keyerror(store):
    store.save_version(_V1, author="t", reason="v1")
    with pytest.raises(KeyError):
        store.diff_versions(999, 1000)


def test_ctf_balloon_round_trip(store):
    rid = store.record_ctf(
        {"balloon_id": "B12", "family": "stamping", "nominal": 1.0, "status": "open"}
    )
    assert rid > 0
    rows = store.list_ctf()
    assert any(r["balloon_id"] == "B12" for r in rows)


def test_supplier_capability_round_trip(store):
    res = store.record_capability(
        {"rule_id": "A", "supplier": "Hoky", "achieved_min": 0.02, "confirmed": True}
    )
    assert res["kind"] == "supplier_capability"
    rows = store.list_supplier_capability()
    assert rows and rows[0]["rule_id"] == "A"
    assert rows[0]["confirmed"] is True
