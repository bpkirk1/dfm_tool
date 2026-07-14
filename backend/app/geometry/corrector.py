"""Apply a Phase 3 fix file to a STEP model with a CAD kernel — conservatively.

Discipline (architectural rule #6 — never emit a silently-wrong model):

* Only ``confidence: "computed"`` corrections are ever applied. Advisory/manual
  corrections are skipped with a reason.
* Every parameter routes through :data:`HANDLERS`; anything without a handler is
  skipped ("no geometry handler for parameter X").
* A handler that cannot safely edit the topology raises :class:`Skip` with a
  reason rather than forcing it.
* After edits, the corrected STEP is re-run through the *same* deterministic
  pipeline (``extract_step`` -> ``evaluate_family`` via ``build_report``). If any
  rule regressed, the result is rejected and no file is offered for download.
* The input file is never modified; output is ``<name>_corrected_<ts>.stp``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import kernel

# rule verdict severity for improved/regressed comparison
_RANK = {"pass": 0, "flag": 1, "fail": 2}


class Skip(Exception):
    """Raised by a handler when an edit can't be applied safely."""


@dataclass
class CorrectionResult:
    applied: list[dict] = field(default_factory=list)      # correction + face refs + before/after
    skipped: list[dict] = field(default_factory=list)      # correction + reason
    reevaluation: dict = field(default_factory=dict)       # new report summary
    improved: list[str] = field(default_factory=list)      # rule ids fail->pass/flag
    regressed: list[str] = field(default_factory=list)     # rule ids that got worse
    output_path: str | None = None                         # None if rejected/no-op
    status: str = "noop"                                   # applied | rejected | noop | unavailable
    message: str | None = None
    score_before: float | None = None
    score_after: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --- kernel face helpers ------------------------------------------------------
def _shape(model):
    return model.val() if hasattr(model, "val") else model


def _cylindrical_faces(model) -> list[tuple[Any, float]]:
    """(face, radius) for every cylindrical face; kernel-guarded."""
    out: list[tuple[Any, float]] = []
    try:
        faces = _shape(model).Faces()
    except Exception:
        return out
    for f in faces:
        try:
            if f.geomType() != "CYLINDER":
                continue
            radius = _num(f.radius())
        except Exception:
            continue
        if radius is not None:
            out.append((f, radius))
    return out


# --- handlers -----------------------------------------------------------------
def _handle_bend_radius(model, corr: dict) -> tuple[Any, dict]:
    """Increase an undersized inside bend/fillet radius to the computed target.

    Best-effort: locates the smallest cylindrical face below target (the same
    concentric-cylinder signal the text parser uses) and attempts to re-fillet
    the adjoining edge chain. Any kernel objection aborts the edit (Skip).
    """
    target = _num(corr.get("target_value"))
    if target is None:
        raise Skip("no computed target radius")
    faces = _cylindrical_faces(model)
    if not faces:
        raise Skip("no cylindrical faces found in the B-rep")
    under = [(f, r) for f, r in faces if r < target - 1e-6]
    if not under:
        raise Skip(f"no cylindrical face below target R{target:g}")
    face, r = min(under, key=lambda t: t[1])
    delta = round(target - r, 6)
    try:
        # Grow the fillet by selecting the edges bordering the undersized fillet
        # face and re-filleting up to the target radius. If the topology (short
        # adjacent walls, intersecting features) rejects it, the kernel raises
        # and we skip rather than emit a distorted solid.
        edges = face.Edges()
        selector = _EdgeSet(edges)
        edited = model.edges(selector).fillet(delta)
    except Exception as exc:  # topology-blocked or unsupported
        raise Skip(f"re-fillet blocked by topology: {type(exc).__name__}: {exc}")
    info = {
        "rule_id": corr.get("rule_id"),
        "parameter": corr.get("parameter"),
        "handler": "bend_radius",
        "before": round(r, 4),
        "after": round(target, 4),
        "delta": delta,
        "unit": corr.get("unit") or "mm",
        "rationale": corr.get("rationale"),
        "face_ref": _face_ref(face),
    }
    return edited, info


def _handle_hole(model, corr: dict) -> tuple[Any, dict]:
    """Resize a cylindrical through-hole to meet a pierce/feature target.

    Conservative: only acts when a single clearly-undersized cylindrical hole
    face maps to the target; otherwise Skip. (Positional hole-to-edge moves are
    left to a later phase — reported as skipped.)
    """
    target = _num(corr.get("target_value"))
    if target is None:
        raise Skip("no computed target for hole adjustment")
    raise Skip("hole geometry editing not yet implemented for this topology")


# Registry: fix-file `parameter` -> handler. Extend per family here.
HANDLERS: dict[str, Callable[[Any, dict], tuple[Any, dict]]] = {
    "min_inside_corner_radius_mm": _handle_bend_radius,
    "min_pierced_width_or_dia": _handle_hole,
    "feature_to_edge": _handle_hole,
}


class _EdgeSet:
    """cadquery Selector matching a fixed set of edges (by identity/hash)."""

    def __init__(self, edges):
        self._targets = list(edges)

    def filter(self, objectlist):  # cadquery Selector protocol
        keep = []
        for o in objectlist:
            for t in self._targets:
                try:
                    if o.wrapped.IsSame(t.wrapped):
                        keep.append(o)
                        break
                except Exception:
                    continue
        return keep


def _face_ref(face) -> dict[str, Any]:
    try:
        c = face.Center()
        return {"center": [round(c.x, 3), round(c.y, 3), round(c.z, 3)]}
    except Exception:
        return {}


# --- orchestration ------------------------------------------------------------
def _verdict_map(report: dict) -> dict[str, str]:
    results = (report.get("summary", {}) or {}).get("results", []) or []
    return {r.get("rule_id"): r.get("verdict") for r in results if r.get("rule_id")}


def apply_fixes(
    step_path: str | Path,
    fix_file: dict,
    *,
    store,
    family: str | None = None,
    out_dir: str | Path,
) -> CorrectionResult:
    """Apply computed corrections, re-validate, and return a CorrectionResult."""
    from ..report import RunInputs, build_report  # lazy: avoids import cycle

    step_path = Path(step_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not kernel.available:
        return CorrectionResult(
            status="unavailable", message=kernel.UNAVAILABLE_MESSAGE
        )

    fam = family or fix_file.get("family")
    corrections = list(fix_file.get("corrections", []) or [])

    # Baseline evaluation of the untouched input.
    report_before = build_report(store, RunInputs(step_path=step_path, family=fam))
    before = _verdict_map(report_before)
    score_before = (report_before.get("summary", {}) or {}).get("readiness_score")

    result = CorrectionResult(score_before=score_before)

    try:
        model = kernel.load_step(step_path)
    except Exception as exc:
        result.status = "rejected"
        result.message = f"could not load STEP into kernel: {exc}"
        return result

    for corr in corrections:
        rule_id = corr.get("rule_id")
        param = corr.get("parameter")
        if corr.get("confidence") != "computed":
            result.skipped.append(
                {"rule_id": rule_id, "parameter": param,
                 "reason": f"confidence '{corr.get('confidence')}' — not auto-applied"}
            )
            continue
        handler = HANDLERS.get(param)
        if handler is None:
            result.skipped.append(
                {"rule_id": rule_id, "parameter": param,
                 "reason": f"no geometry handler for parameter '{param}'"}
            )
            continue
        try:
            model, info = handler(model, corr)
            result.applied.append(info)
        except Skip as s:
            result.skipped.append(
                {"rule_id": rule_id, "parameter": param, "reason": str(s)}
            )
        except Exception as exc:  # never let a kernel quirk abort the run
            result.skipped.append(
                {"rule_id": rule_id, "parameter": param,
                 "reason": f"unexpected edit error: {type(exc).__name__}: {exc}"}
            )

    if not result.applied:
        result.status = "noop"
        result.message = "no computed corrections could be applied"
        return result

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    corrected = out_dir / f"{step_path.stem}_corrected_{ts}.stp"
    try:
        kernel.save_step(model, corrected)
    except Exception as exc:
        result.status = "rejected"
        result.message = f"could not export corrected STEP: {exc}"
        return result

    # Verification loop: re-run the corrected model through the same pipeline.
    report_after = build_report(store, RunInputs(step_path=corrected, family=fam))
    after = _verdict_map(report_after)
    summary_after = report_after.get("summary", {}) or {}
    result.reevaluation = {
        "counts": summary_after.get("counts"),
        "readiness_score": summary_after.get("readiness_score"),
    }
    result.score_after = summary_after.get("readiness_score")

    for rid, base_v in before.items():
        new_v = after.get(rid)
        if base_v not in _RANK or new_v not in _RANK:
            continue
        if _RANK[new_v] < _RANK[base_v]:
            result.improved.append(rid)
        elif _RANK[new_v] > _RANK[base_v]:
            result.regressed.append(rid)

    if result.regressed:
        # Reject: never offer a model that made any rule worse.
        try:
            corrected.unlink(missing_ok=True)
        except Exception:
            pass
        result.status = "rejected"
        result.output_path = None
        result.message = (
            "correction rejected — re-evaluation regressed: "
            + ", ".join(sorted(result.regressed))
        )
        return result

    result.status = "applied"
    result.output_path = str(corrected)
    return result
