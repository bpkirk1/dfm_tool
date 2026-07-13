"""The deterministic comparison operators.

Each operator takes a measured value and a rule limit and returns an
:class:`OperatorOutcome`. The operators never decide severity or readiness;
they only answer "does this value satisfy this limit, and by how much?". The
evaluator turns that into a pass/flag/fail verdict with provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Floating tolerance for equality of measured geometry (mm / deg).
EQ_ABS_TOL = 1e-6


@dataclass
class OperatorOutcome:
    """Result of comparing one measured value against one rule limit.

    - ``satisfied``: True if the value meets the limit, False if it violates it,
      None if the operator could not be evaluated automatically (e.g. an ``eq``
      rule whose limit is a free-text callout). None => needs a manual check.
    - ``margin``: signed distance from the limit in the rule's units. Positive
      means comfortably inside the limit; negative means a violation. None when
      a numeric margin is not meaningful.
    - ``detail``: human-readable description of the limit that was applied.
    """

    satisfied: bool | None
    margin: float | None
    detail: str


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def apply_operator(operator: str, value: Any, limit: Any) -> OperatorOutcome:
    v = _num(value)

    if operator in {"lt", "lte", "gt", "gte"}:
        lim = _num(limit)
        if v is None or lim is None:
            return OperatorOutcome(None, None, f"{operator} {limit}")
        if operator == "lt":
            return OperatorOutcome(v < lim, lim - v, f"< {lim}")
        if operator == "lte":
            return OperatorOutcome(v <= lim, lim - v, f"<= {lim}")
        if operator == "gt":
            return OperatorOutcome(v > lim, v - lim, f"> {lim}")
        return OperatorOutcome(v >= lim, v - lim, f">= {lim}")

    if operator == "eq":
        lim = _num(limit)
        if lim is None:
            # Free-text callout (e.g. "0.036 x 45deg, farside") — not auto-checkable.
            return OperatorOutcome(None, None, f"= {limit}")
        if v is None:
            return OperatorOutcome(None, None, f"= {lim}")
        return OperatorOutcome(abs(v - lim) <= EQ_ABS_TOL, -abs(v - lim), f"= {lim}")

    if operator == "between":
        if not isinstance(limit, (list, tuple)) or len(limit) != 2:
            return OperatorOutcome(None, None, f"between {limit}")
        lo, hi = _num(limit[0]), _num(limit[1])
        if v is None or lo is None or hi is None:
            return OperatorOutcome(None, None, f"between {limit}")
        inside = lo <= v <= hi
        margin = min(v - lo, hi - v)
        return OperatorOutcome(inside, margin, f"between {lo} and {hi}")

    if operator == "angle_tol":
        target, plus, minus = _angle_band(limit)
        if v is None or target is None:
            return OperatorOutcome(None, None, f"angle_tol {limit}")
        lo, hi = target - minus, target + plus
        inside = lo <= v <= hi
        margin = min(v - lo, hi - v)
        return OperatorOutcome(inside, margin, f"{target} +{plus}/-{minus} deg")

    return OperatorOutcome(None, None, f"unknown operator '{operator}'")


def _angle_band(limit: Any) -> tuple[float | None, float, float]:
    """Parse an angle_tol limit into (target, plus, minus).

    Accepts {target, tol} (symmetric) or {target, plus/tol_plus, minus/tol_minus}.
    """
    if not isinstance(limit, dict):
        return None, 0.0, 0.0
    target = _num(limit.get("target"))
    if "tol" in limit and limit.get("tol") is not None:
        t = _num(limit["tol"]) or 0.0
        return target, t, t
    plus = _num(limit.get("plus", limit.get("tol_plus"))) or 0.0
    minus = _num(limit.get("minus", limit.get("tol_minus"))) or 0.0
    return target, plus, minus
