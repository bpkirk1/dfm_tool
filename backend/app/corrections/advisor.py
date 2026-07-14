"""Deterministic correction targets + fix-file export.

The advisor consumes the evaluator's per-rule results (verdict, measured value,
operator, effective limit, severity, source) and computes, for each violation, a
compliant target value with a small safety margin so the corrected design does
not land back in the "flag" band. Where no honest numeric target exists (free-text
callouts, unknown operators), it returns a *review* correction and never invents a
number (architectural rule #6 — proposed/uncomputable never fabricated).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import yaml

# Default safety margin if the YAML carries no corrections/scoring block. Mirrors
# the evaluator's marginal fraction so a corrected value clears the flag band.
DEFAULT_SAFETY_MARGIN = 0.10
_ROUND = 4
_SEVERITY_RANK = {"blocker": 4, "major": 3, "minor": 2, "info": 1}

FIX_SCHEMA = "dfm-fixes/1"


@dataclass
class Correction:
    rule_id: str
    parameter: str
    family: str
    verdict: str                 # "fail" | "flag"
    severity: str
    current_value: float | str | None
    limit: Any                   # effective limit (post capability override)
    operator: str
    target_value: float | None   # compliant value incl. margin (None if not computable)
    delta: float | None          # target - current, signed
    direction: str               # "increase" | "decrease" | "adjust" | "review"
    recommendation: str          # one-sentence deterministic instruction
    rationale: str               # why, citing the rule source (provenance)
    confidence: str              # "computed" | "advisory" | "manual"
    unit: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt(x: Any) -> str:
    n = _num(x)
    if n is None:
        return str(x)
    # Trim trailing zeros for readable, deterministic text (0.088, 135, 0.08).
    return f"{n:g}"


def _angle_band(limit: Any) -> tuple[float | None, float, float]:
    """(target, plus, minus) from an angle_tol limit dict."""
    if not isinstance(limit, dict):
        return None, 0.0, 0.0
    target = _num(limit.get("target"))
    if limit.get("tol") is not None:
        t = _num(limit["tol"]) or 0.0
        return target, t, t
    plus = _num(limit.get("plus", limit.get("tol_plus"))) or 0.0
    minus = _num(limit.get("minus", limit.get("tol_minus"))) or 0.0
    return target, plus, minus


def compute_target(
    operator: str, limit: Any, current: Any, safety_margin: float
) -> tuple[float | None, str, str]:
    """Deterministic compliant target for one rule.

    Returns ``(target_value, direction, confidence)``. ``target_value`` is None
    when no honest number can be derived (direction ``review``, confidence
    ``manual``) — the advisor never fabricates a value.
    """
    m = safety_margin
    cur = _num(current)

    if operator in ("gte", "gt"):
        lim = _num(limit)
        if lim is None:
            return None, "review", "manual"
        return round(lim * (1 + m), _ROUND), "increase", "computed"

    if operator in ("lte", "lt"):
        lim = _num(limit)
        if lim is None:
            return None, "review", "manual"
        return round(lim * (1 - m), _ROUND), "decrease", "computed"

    if operator == "between":
        if not isinstance(limit, (list, tuple)) or len(limit) != 2:
            return None, "review", "manual"
        lo, hi = _num(limit[0]), _num(limit[1])
        if lo is None or hi is None or cur is None:
            return None, "review", "manual"
        if cur < lo:
            return round(lo * (1 + m), _ROUND), "increase", "computed"
        if cur > hi:
            return round(hi * (1 - m), _ROUND), "decrease", "computed"
        # Inside the band but flagged (marginal): move toward the nearer bound,
        # margin applied inward.
        if (cur - lo) <= (hi - cur):
            return round(lo * (1 + m), _ROUND), "increase", "computed"
        return round(hi * (1 - m), _ROUND), "decrease", "computed"

    if operator == "angle_tol":
        target, _plus, _minus = _angle_band(limit)
        if target is None:
            return None, "review", "manual"
        return round(target, _ROUND), "adjust", "computed"

    if operator == "eq":
        lim = _num(limit)
        if lim is None:  # free-text callout — do not invent a number
            return None, "review", "manual"
        return round(lim, _ROUND), "adjust", "computed"

    return None, "review", "manual"


def _limit_text(row: dict[str, Any]) -> str:
    """Human-readable limit for a review recommendation."""
    detail = row.get("limit_detail")
    if detail:
        return str(detail)
    return str(row.get("limit_applied"))


def _is_placeholder_flag(row: dict[str, Any]) -> bool:
    """A flag raised only because a supplier-capability limit is unconfirmed."""
    if row.get("verdict") != "flag" or not row.get("supplier_adjustable"):
        return False
    return "placeholder" in (row.get("note") or "").lower()


def _recommendation(
    direction: str, parameter: str, current: Any, target: float | None,
    limit: Any, unit: str | None, safety_margin: float, row: dict[str, Any],
) -> str:
    u = f" {unit}" if unit else ""
    pct = _fmt(safety_margin * 100)
    cur_txt = _fmt(current) if _num(current) is not None else "the current value"
    if direction == "increase":
        return (
            f"Increase {parameter} from {cur_txt}{u} to at least {_fmt(target)}{u} "
            f"(limit {_fmt(limit)}{u} + {pct}% margin)."
        )
    if direction == "decrease":
        return (
            f"Reduce {parameter} from {cur_txt}{u} to at most {_fmt(target)}{u} "
            f"(limit {_fmt(limit)}{u} − {pct}% margin)."
        )
    if direction == "adjust":
        return f"Set {parameter} to {_fmt(target)}{u} (target of the specified band/callout)."
    # review
    return f"Verify {parameter} against: {_limit_text(row)}."


def build_corrections(
    results: list[dict[str, Any]], family: str, safety_margin: float
) -> list[Correction]:
    """Build worst-first corrections from evaluated rule results.

    ``results`` are the enforced-rule results from ``evaluate_family`` (proposed
    rules are already excluded there and thus never drive a correction). Only
    ``fail``/``flag`` verdicts produce a correction.
    """
    out: list[Correction] = []
    for row in results:
        verdict = row.get("verdict")
        if verdict not in ("fail", "flag"):
            continue

        operator = row.get("operator", "")
        limit = row.get("limit_applied")
        current = row.get("measured")
        unit = row.get("units")
        parameter = row.get("parameter", "")
        source = row.get("source", "")
        severity = row.get("severity", "major")

        if _is_placeholder_flag(row):
            # The value already meets the seeded placeholder; the real action is to
            # confirm the supplier capability, not to change geometry.
            correction = Correction(
                rule_id=row.get("rule_id", ""),
                parameter=parameter,
                family=family,
                verdict=verdict,
                severity=severity,
                current_value=current,
                limit=limit,
                operator=operator,
                target_value=None,
                delta=None,
                direction="review",
                recommendation=(
                    f"Confirm supplier capability for {parameter}: limit "
                    f"{_fmt(limit)}{(' ' + unit) if unit else ''} is an unconfirmed "
                    "placeholder — validate at FAI before relying on it."
                ),
                rationale=f"{severity} rule {row.get('rule_id', '')}. Source: {source}.",
                confidence="advisory",
                unit=unit,
            )
            out.append(correction)
            continue

        target, direction, confidence = compute_target(operator, limit, current, safety_margin)
        delta = None
        if target is not None and _num(current) is not None:
            delta = round(target - _num(current), _ROUND)

        rationale = (
            f"{severity} rule {row.get('rule_id', '')}: measured "
            f"{_fmt(current) if _num(current) is not None else 'value'} "
            f"vs {row.get('limit_detail') or (str(operator) + ' ' + _fmt(limit))}. "
            f"Source: {source}."
        )
        out.append(
            Correction(
                rule_id=row.get("rule_id", ""),
                parameter=parameter,
                family=family,
                verdict=verdict,
                severity=severity,
                current_value=current,
                limit=limit,
                operator=operator,
                target_value=target,
                delta=delta,
                direction=direction,
                recommendation=_recommendation(
                    direction, parameter, current, target, limit, unit, safety_margin, row
                ),
                rationale=rationale,
                confidence=confidence,
                unit=unit,
            )
        )

    # Worst-first: severity desc, then largest absolute change desc.
    out.sort(
        key=lambda c: (
            _SEVERITY_RANK.get(c.severity, 0),
            abs(c.delta) if c.delta is not None else 0.0,
        ),
        reverse=True,
    )
    return out


def _normalize(corrections: list[Any]) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for c in corrections:
        norm.append(c.to_dict() if isinstance(c, Correction) else dict(c))
    return norm


def build_envelope(
    corrections: list[Any],
    *,
    source_file: str | None,
    family: str | None,
    criteria_version: int | None,
    app_version: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Versioned fix-file envelope (schema ``dfm-fixes/1``)."""
    return {
        "schema": FIX_SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": source_file,
        "family": family,
        "criteria_version": criteria_version,
        "app_version": app_version,
        "corrections": _normalize(corrections),
    }


def export_fixes_json(envelope: dict[str, Any]) -> str:
    # Deterministic: fixed key order, stable indentation. Only generated_at varies.
    return json.dumps(envelope, indent=2, ensure_ascii=False)


def export_fixes_yaml(envelope: dict[str, Any]) -> str:
    return yaml.safe_dump(envelope, sort_keys=False, allow_unicode=True)
