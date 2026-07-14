"""Pydantic models for the DFM criteria store.

These mirror `dfm-criteria.seed.yaml`. The application loads rules from config;
no DFM limit is ever hardcoded in the engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Operators the deterministic evaluator understands. Adding a rule never requires
# a new operator unless the comparison itself is genuinely new.
VALID_OPERATORS = {"lt", "lte", "gt", "gte", "eq", "between", "angle_tol"}
VALID_SEVERITIES = {"blocker", "major", "minor", "info"}
# Governance: only `active` rules are enforced. `proposed` rules are mined from a
# reference DFM but await a human sign-off and must NOT drive a verdict/score.
VALID_STATUSES = {"active", "proposed"}


class Capability(BaseModel):
    """Supplier-confirmed capability for a supplier_adjustable rule."""

    model_config = ConfigDict(extra="allow")
    achieved_min: float | None = None
    cpk: float | None = None
    confirmed: bool = False


class Rule(BaseModel):
    """A single deterministic DFM check loaded from config."""

    model_config = ConfigDict(extra="allow")

    id: str
    parameter: str
    operator: str
    # limit is intentionally loose: a number, a string callout, a [lo, hi] pair,
    # or an angle_tol dict {target, plus, minus} / {target, tol}.
    limit: Any
    severity: str = "major"
    source: str = ""
    supplier_adjustable: bool = False
    units: str | None = None
    capability: Capability | None = None
    # Governance/provenance (mining pipeline). Absent => active for back-compat.
    status: str = "active"
    seen_count: int | None = None
    evidence: list[str] = Field(default_factory=list)
    # Optional 3D-viewer marker tag. Decouples marker localization from rule ids
    # so renaming a rule in YAML doesn't silently break marker pinning.
    marker: str | None = None

    def is_enforced(self, family_status: str = "active") -> bool:
        """A rule drives a verdict only if both it and its family are active."""
        return self.status == "active" and family_status == "active"

    def validate_semantics(self) -> list[str]:
        problems: list[str] = []
        if self.operator not in VALID_OPERATORS:
            problems.append(f"{self.id}: unknown operator '{self.operator}'")
        if self.severity not in VALID_SEVERITIES:
            problems.append(f"{self.id}: unknown severity '{self.severity}'")
        if self.status not in VALID_STATUSES:
            problems.append(f"{self.id}: unknown status '{self.status}'")
        return problems

    def effective_limit(self) -> Any:
        """Use confirmed supplier capability over the seeded placeholder.

        For supplier_adjustable minimums (gte/gt), a confirmed achieved_min is the
        real, demonstrated capability and should drive the verdict.
        """
        if (
            self.supplier_adjustable
            and self.capability
            and self.capability.confirmed
            and self.capability.achieved_min is not None
            and self.operator in {"gte", "gt"}
        ):
            return self.capability.achieved_min
        return self.limit


class FormAngle(BaseModel):
    model_config = ConfigDict(extra="allow")
    feature: str
    target: float
    tol: float | None = None
    tol_plus: float | None = None
    tol_minus: float | None = None
    source: str = ""


class Material(BaseModel):
    model_config = ConfigDict(extra="allow")
    family: str | None = None
    thickness_mm: float | None = None
    thickness_tol_mm: float | None = None


class ProcessFamily(BaseModel):
    model_config = ConfigDict(extra="allow")
    applies_to_example: str | None = None
    material: Material | None = None
    rules: list[Rule] = Field(default_factory=list)
    form_angles: list[FormAngle] = Field(default_factory=list)
    # A whole family can be proposed (e.g. a newly-mined process). Absent =>
    # active. A proposed family suppresses enforcement of all its rules.
    status: str = "active"

    def model_post_init(self, __context: Any) -> None:
        # forming.form_angles_deg -> typed FormAngle list, if present.
        forming = getattr(self, "forming", None)
        if isinstance(forming, dict):
            raw = forming.get("form_angles_deg", [])
            self.form_angles = [FormAngle(**a) for a in raw]


class Scoring(BaseModel):
    """Tunable scoring constants for the evaluator.

    These materially affect verdicts (marginal band) and the readiness score, so
    per architectural rule #1 they live in config, not code. The defaults here
    mirror the historical in-code values so a YAML without a ``scoring:`` block
    behaves exactly as before.
    """

    model_config = ConfigDict(extra="allow")
    marginal_fraction: float = 0.10
    severity_weight: dict[str, float] = Field(
        default_factory=lambda: {"blocker": 10.0, "major": 5.0, "minor": 2.0, "info": 0.5}
    )
    verdict_credit: dict[str, float] = Field(
        default_factory=lambda: {"pass": 1.0, "flag": 0.5, "fail": 0.0}
    )


class Corrections(BaseModel):
    """Config for the deterministic correction advisor.

    ``safety_margin`` nudges a compliant target just past the limit so a fixed
    value doesn't land back in the evaluator's marginal (flag) band. Defaults to
    the same fraction the evaluator uses.
    """

    model_config = ConfigDict(extra="allow")
    safety_margin: float = 0.10


class Meta(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: str = "1.0"
    ruleset_version: str = "unknown"
    units: str = "mm"
    last_edited_by: str = "seed"
    notes: str | None = None
    scoring: Scoring = Field(default_factory=Scoring)
    corrections: Corrections = Field(default_factory=Corrections)


class CriteriaSet(BaseModel):
    model_config = ConfigDict(extra="allow")
    meta: Meta = Field(default_factory=Meta)
    process_families: dict[str, ProcessFamily] = Field(default_factory=dict)
    ctf_tracking: dict[str, Any] = Field(default_factory=dict)

    def family(self, name: str) -> ProcessFamily:
        if name not in self.process_families:
            raise KeyError(
                f"Unknown process family '{name}'. Known: {list(self.process_families)}"
            )
        return self.process_families[name]

    def validate_semantics(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        for fam_name, fam in self.process_families.items():
            if fam.status not in VALID_STATUSES:
                problems.append(f"family '{fam_name}': unknown status '{fam.status}'")
            for rule in fam.rules:
                problems.extend(rule.validate_semantics())
                if rule.id in seen:
                    problems.append(f"duplicate rule id '{rule.id}'")
                seen.add(rule.id)
        return problems


def load_criteria(path: str | Path) -> CriteriaSet:
    """Parse the YAML criteria file into a validated CriteriaSet."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cs = CriteriaSet(**data)
    problems = cs.validate_semantics()
    if problems:
        raise ValueError(
            "Criteria file failed validation:\n  - " + "\n  - ".join(problems)
        )
    return cs
