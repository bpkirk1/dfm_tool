"""The deterministic rule evaluator.

Given a process family's rules and a dict of measured features, produce one
result per rule — each carrying its verdict, the limit applied, the measured
value, severity, and the cited source (provenance). No verdict is ever produced
without a rule id and source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models.criteria import ProcessFamily, Rule
from .operators import apply_operator

# A passing value this close (fractionally) to its limit is downgraded to a flag.
MARGINAL_FRACTION = 0.10

SEVERITY_WEIGHT = {"blocker": 10.0, "major": 5.0, "minor": 2.0, "info": 0.5}
VERDICT_CREDIT = {"pass": 1.0, "flag": 0.5, "fail": 0.0}


@dataclass
class EvalResult:
    rule_id: str
    parameter: str
    operator: str
    measured: Any
    limit_applied: Any
    limit_detail: str
    verdict: str  # pass | flag | fail | manual
    severity: str
    source: str
    supplier_adjustable: bool
    units: str | None
    margin: float | None
    note: str = ""
    seen_count: int | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "parameter": self.parameter,
            "operator": self.operator,
            "measured": self.measured,
            "limit_applied": self.limit_applied,
            "limit_detail": self.limit_detail,
            "verdict": self.verdict,
            "severity": self.severity,
            "source": self.source,
            "supplier_adjustable": self.supplier_adjustable,
            "units": self.units,
            "margin": self.margin,
            "note": self.note,
            "seen_count": self.seen_count,
            "evidence": self.evidence,
        }


@dataclass
class ReportSummary:
    family: str
    ruleset_version: str
    results: list[EvalResult] = field(default_factory=list)
    readiness_score: float | None = None
    counts: dict[str, int] = field(default_factory=dict)
    manual_check_parameters: list[str] = field(default_factory=list)
    # Mined-but-not-yet-approved rules. Surfaced for transparency; NEVER scored.
    proposed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "ruleset_version": self.ruleset_version,
            "readiness_score": self.readiness_score,
            "counts": self.counts,
            "manual_check_parameters": self.manual_check_parameters,
            "results": [r.to_dict() for r in self.results],
            "proposed": self.proposed,
        }


def _is_marginal(margin: float | None, limit: Any) -> bool:
    if margin is None or margin < 0:
        return False
    try:
        ref = abs(float(limit))
    except (TypeError, ValueError):
        return False
    band = MARGINAL_FRACTION * ref if ref > 0 else MARGINAL_FRACTION
    return margin < band


def _evaluate_rule(rule: Rule, features: dict[str, Any]) -> EvalResult:
    value = features.get(rule.parameter)
    limit = rule.effective_limit()
    outcome = apply_operator(rule.operator, value, limit)

    note = ""
    if outcome.satisfied is None:
        verdict = "manual"
        if value is None:
            note = "Feature not auto-measured — needs a manual check."
        else:
            note = "Limit is a non-numeric callout — verify manually."
    elif outcome.satisfied is False:
        verdict = "fail"
    else:
        verdict = "pass"
        if rule.supplier_adjustable and not (
            rule.capability and rule.capability.confirmed
        ):
            verdict = "flag"
            note = "Limit is an unconfirmed supplier-capability placeholder — confirm at FAI."
        elif _is_marginal(outcome.margin, limit):
            verdict = "flag"
            note = "Within margin of the limit — marginal."

    return EvalResult(
        rule_id=rule.id,
        parameter=rule.parameter,
        operator=rule.operator,
        measured=value,
        limit_applied=limit,
        limit_detail=outcome.detail,
        verdict=verdict,
        severity=rule.severity,
        source=rule.source,
        supplier_adjustable=rule.supplier_adjustable,
        units=rule.units,
        margin=outcome.margin,
        note=note,
        seen_count=rule.seen_count,
        evidence=list(rule.evidence),
    )


def _proposed_entry(rule: Rule) -> dict[str, Any]:
    """A read-only summary of a mined rule that is not yet enforced."""
    return {
        "rule_id": rule.id,
        "parameter": rule.parameter,
        "operator": rule.operator,
        "limit": rule.limit,
        "severity": rule.severity,
        "source": rule.source,
        "units": rule.units,
        "seen_count": rule.seen_count,
        "evidence": list(rule.evidence),
    }


def evaluate_family(
    family_name: str,
    family: ProcessFamily,
    features: dict[str, Any],
    ruleset_version: str = "unknown",
) -> ReportSummary:
    family_status = getattr(family, "status", "active")

    # Only `active` rules in an `active` family drive a verdict/score. Everything
    # mined-but-unapproved (`proposed`) is surfaced separately, never scored.
    enforced = [r for r in family.rules if r.is_enforced(family_status)]
    proposed = [
        _proposed_entry(r) for r in family.rules if not r.is_enforced(family_status)
    ]

    results = [_evaluate_rule(rule, features) for rule in enforced]

    counts = {"pass": 0, "flag": 0, "fail": 0, "manual": 0}
    for r in results:
        counts[r.verdict] += 1

    earned = possible = 0.0
    for r in results:
        if r.verdict == "manual":
            continue
        w = SEVERITY_WEIGHT.get(r.severity, 1.0)
        possible += w
        earned += w * VERDICT_CREDIT[r.verdict]
    score = round(100.0 * earned / possible, 1) if possible else None

    manual_params = [r.parameter for r in results if r.verdict == "manual"]

    return ReportSummary(
        family=family_name,
        ruleset_version=ruleset_version,
        results=results,
        readiness_score=score,
        counts=counts,
        manual_check_parameters=manual_params,
        proposed=proposed,
    )
