"""Versioned criteria store (SQLite) + CTF capability history.

Design intent (per the architectural rules):

* The YAML file is the documented single source of truth. On every sync we hash
  the current YAML; if it differs from the latest stored version we import it as
  a NEW version (author, timestamp, reason). So editing the YAML always changes
  the next report's verdict, *and* we keep a full, diffable history.
* Every result downstream can cite the ``ruleset_version`` that produced it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..models.criteria import CriteriaSet, load_criteria

_SCHEMA = """
CREATE TABLE IF NOT EXISTS criteria_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ruleset_version TEXT NOT NULL,
    author        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    content_yaml  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ctf_capability (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    balloon_id  TEXT NOT NULL,
    family      TEXT,
    nominal     REAL,
    tol_plus    REAL,
    tol_minus   REAL,
    drawing_sheet TEXT,
    cpk_target  REAL,
    cpk_actual  REAL,
    sample_n    INTEGER,
    status      TEXT,
    recorded_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CriteriaStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- versioning -----------------------------------------------------------

    def latest(self) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM criteria_versions ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()

    def list_versions(self) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT id, ruleset_version, author, reason, created_at, content_hash "
            "FROM criteria_versions ORDER BY id DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def save_version(
        self, content_yaml: str, author: str, reason: str
    ) -> int:
        # Validate before storing — never persist a broken ruleset.
        data = yaml.safe_load(content_yaml) or {}
        cs = CriteriaSet(**data)
        problems = cs.validate_semantics()
        if problems:
            raise ValueError("Refusing to store invalid criteria: " + "; ".join(problems))

        cur = self._conn.execute(
            "INSERT INTO criteria_versions "
            "(ruleset_version, author, reason, created_at, content_hash, content_yaml) "
            "VALUES (?,?,?,?,?,?)",
            (
                cs.meta.ruleset_version,
                author,
                reason,
                _now(),
                _hash(content_yaml),
                content_yaml,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def sync_from_yaml(self, yaml_path: str | Path) -> dict[str, Any]:
        """Import the YAML as a new version iff it changed since the last sync."""
        yaml_path = Path(yaml_path)
        content = yaml_path.read_text(encoding="utf-8")
        h = _hash(content)
        latest = self.latest()
        if latest is not None and latest["content_hash"] == h:
            return {"changed": False, "version_id": latest["id"]}
        reason = (
            "Initial seed import" if latest is None else "YAML edited — re-imported"
        )
        vid = self.save_version(content, author="file-sync", reason=reason)
        return {"changed": True, "version_id": vid}

    def get_criteria(self, version_id: int | None = None) -> CriteriaSet:
        if version_id is None:
            row = self.latest()
            if row is None:
                raise RuntimeError("No criteria versions stored — sync from YAML first.")
        else:
            row = self._conn.execute(
                "SELECT * FROM criteria_versions WHERE id=?", (version_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No criteria version id={version_id}")
        data = yaml.safe_load(row["content_yaml"]) or {}
        return CriteriaSet(**data)

    def get_yaml(self, version_id: int) -> str:
        row = self._conn.execute(
            "SELECT content_yaml FROM criteria_versions WHERE id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No criteria version id={version_id}")
        return row["content_yaml"]

    # -- diff -----------------------------------------------------------------

    def diff_versions(self, id_a: int, id_b: int) -> dict[str, Any]:
        """Rule-level diff between two stored versions: added/removed/changed."""
        a = self.get_criteria(id_a)
        b = self.get_criteria(id_b)
        rules_a = _flatten_rules(a)
        rules_b = _flatten_rules(b)

        added = [rid for rid in rules_b if rid not in rules_a]
        removed = [rid for rid in rules_a if rid not in rules_b]
        changed = []
        for rid in rules_a.keys() & rules_b.keys():
            if rules_a[rid] != rules_b[rid]:
                changed.append(
                    {"rule_id": rid, "from": rules_a[rid], "to": rules_b[rid]}
                )
        return {
            "from_version": id_a,
            "to_version": id_b,
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    # -- CTF capability -------------------------------------------------------

    def record_ctf(self, entry: dict[str, Any]) -> int:
        cur = self._conn.execute(
            "INSERT INTO ctf_capability "
            "(balloon_id, family, nominal, tol_plus, tol_minus, drawing_sheet, "
            " cpk_target, cpk_actual, sample_n, status, recorded_at) "
            "VALUES (:balloon_id,:family,:nominal,:tol_plus,:tol_minus,:drawing_sheet,"
            ":cpk_target,:cpk_actual,:sample_n,:status,:recorded_at)",
            {
                "balloon_id": entry.get("balloon_id"),
                "family": entry.get("family"),
                "nominal": entry.get("nominal"),
                "tol_plus": entry.get("tol_plus"),
                "tol_minus": entry.get("tol_minus"),
                "drawing_sheet": entry.get("drawing_sheet"),
                "cpk_target": entry.get("cpk_target"),
                "cpk_actual": entry.get("cpk_actual"),
                "sample_n": entry.get("sample_n"),
                "status": entry.get("status"),
                "recorded_at": _now(),
            },
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_ctf(self) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM ctf_capability ORDER BY id DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


def _flatten_rules(cs: CriteriaSet) -> dict[str, dict[str, Any]]:
    """Map rule id -> comparable rule dict across all families."""
    out: dict[str, dict[str, Any]] = {}
    for fam_name, fam in cs.process_families.items():
        for rule in fam.rules:
            out[rule.id] = {
                "family": fam_name,
                "parameter": rule.parameter,
                "operator": rule.operator,
                "limit": rule.limit,
                "severity": rule.severity,
                "supplier_adjustable": rule.supplier_adjustable,
            }
    return out
