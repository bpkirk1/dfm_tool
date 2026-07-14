"""Stage 3 — emit proposed rules, CTF entries, and a review queue.

Everything produced here is ``status: proposed`` and destined for human review;
nothing is ever activated by the miner. Where a field needs engineering judgment
(operator, severity, final id/limit) the miner emits a best-guess *clearly marked
as auto-generated* plus inline "confirm" comments, so a reviewer edits rather than
authors from scratch. This is Stage 3 of the workflow in
``new_suggestions/06-supplier-feedback-mining.md``.
"""
from __future__ import annotations

import json
from datetime import date as _date
from typing import Any

import yaml

from .consolidate import Consolidation, RuleCandidate

_PROCESS_CODE = {
    "stamping": "STMP", "molding": "MOLD", "plating": "PLAT",
    "cnc": "CNC", "assembly": "ASSY", "unknown": "GEN",
}

# parameter-name signals -> operator (best guess; reviewer confirms).
_GTE_HINTS = ("min", "radius", "gap", "clearance", "width", "length", "land",
              "thickness", "dia", "diameter")
_LTE_HINTS = ("max", "burr", "residual", "vestige", "tol", "step", "deviation",
              "warp", "shrink", "flash")


def _slug(parameter: str) -> str:
    s = parameter.upper()
    for suffix in ("_MM", "_DEG", "_UIN", "_PCT"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace("_", "-")


def _infer_operator(parameter: str) -> str:
    low = parameter.lower()
    if any(h in low for h in _LTE_HINTS):
        return "lte"
    if any(h in low for h in _GTE_HINTS):
        return "gte"
    return "eq"


def _infer_limit(rc: RuleCandidate, operator: str) -> float | None:
    if not rc.values_requested:
        return None
    # Most conservative: a floor (gte) takes the largest asked value; a ceiling
    # (lte) takes the smallest. eq -> leave for the reviewer (None).
    if operator == "gte":
        return rc.value_max
    if operator == "lte":
        return rc.value_min
    return None


def proposed_rule_dict(rc: RuleCandidate) -> dict[str, Any]:
    operator = _infer_operator(rc.parameter)
    limit = _infer_limit(rc, operator)
    code = _PROCESS_CODE.get(rc.process, "GEN")
    rule: dict[str, Any] = {
        "id": f"{code}-{_slug(rc.parameter)}",
        "parameter": rc.parameter,
        "operator": operator,          # AUTO — confirm
        "limit": limit,                # AUTO (most-conservative requested) — confirm
        "severity": "major",           # AUTO default — confirm
        "status": "proposed",
        "source": (
            f"mined from {', '.join(rc.suppliers)} supplier DFM; evidence "
            f"{', '.join(rc.evidence)} (see findings-*.jsonl)"
        ),
        "supplier_adjustable": True,
        "seen_count": rc.seen_count,
        "evidence": rc.evidence,
    }
    if rc.conflict:
        rule["conflict_note"] = (
            f"suppliers disagree; requested spread "
            f"{rc.value_min:g}..{rc.value_max:g} — most conservative kept above"
        )
    return rule


def emit_proposed_rules(con: Consolidation, run_date: str | None = None) -> str:
    run_date = run_date or _date.today().isoformat()
    grouped: dict[str, list[dict]] = {}
    for rc in con.rule_candidates:
        code = _PROCESS_CODE.get(rc.process, "GEN")
        grouped.setdefault(code, []).append(proposed_rule_dict(rc))

    doc = {
        "mine_auto": {
            "run_date": run_date,
            "agent": "dfm-miner (deterministic Stage 3)",
            "note": (
                "AUTO-GENERATED proposed rules. operator/limit/severity are "
                "best-guess and MUST be confirmed by a reviewer before activation. "
                "Nothing here is active."
            ),
        }
    }
    for code, rules in sorted(grouped.items()):
        doc[f"{code.lower()}_rules"] = rules

    header = (
        "# =============================================================================\n"
        "# AUTO-GENERATED PROPOSED RULES (dfm-miner Stage 3)\n"
        "# Everything is status: proposed. operator/limit/severity are auto-inferred\n"
        "# and must be confirmed by a human before merge/activation.\n"
        "# =============================================================================\n\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def emit_ctf_entries(con: Consolidation, run_date: str | None = None) -> str:
    run_date = run_date or _date.today().isoformat()
    entries = []
    for c in con.capabilities:
        entries.append(
            {
                "rule_id": None,  # reviewer maps to a rule id where applicable
                "supplier": c.supplier,
                "parameter": c.parameter,
                "achieved_min": c.achieved,
                "cpk": None,
                "confirmed": c.resolution == "accepted",
                "context": (
                    f"mined capability; parts {', '.join(c.parts) or 'n/a'}; "
                    f"dates {', '.join(c.dates) or 'n/a'}; resolution {c.resolution}"
                ),
                "evidence": c.evidence,
            }
        )
    return json.dumps(
        {
            "schema": "dfm-ctf-import/1",
            "generated": run_date,
            "source": "dfm-miner Stage 3 (auto)",
            "note": "Supplier-keyed capability data. Import via POST /api/ctf after review.",
            "entries": entries,
        },
        indent=2,
        ensure_ascii=False,
    )


def emit_review_queue(con: Consolidation, run_date: str | None = None) -> str:
    run_date = run_date or _date.today().isoformat()
    lines = [
        f"# REVIEW QUEUE — dfm-miner auto ({run_date})",
        "",
        "Approve = flip `[ ]` to `[x]`. operator/limit/severity on proposed rules are "
        "AUTO-INFERRED — verify before merge. Rejected items stay in the findings files "
        "for audit.",
        "",
        "## A. Proposed rules",
        "",
    ]
    for rc in con.rule_candidates:
        rule = proposed_rule_dict(rc)
        conflict = " · **CONFLICT**" if rc.conflict else ""
        limit = f"{rule['limit']:g}" if rule["limit"] is not None else "TBD"
        lines.append(
            f"- [ ] **{rule['id']}** ({rule['parameter']}) {rule['operator']} {limit} "
            f"— seen {rc.seen_count} · {', '.join(rc.evidence)}{conflict}"
        )
    lines += ["", "## B. CTF capability entries", ""]
    for c in con.capabilities:
        ach = f"{c.achieved:g}" if c.achieved is not None else "n/a"
        conf = "confirmed" if c.resolution == "accepted" else "unconfirmed"
        lines.append(
            f"- [ ] {c.supplier}: {c.parameter} = {ach} ({conf}) — {', '.join(c.evidence)}"
        )
    lines += ["", "## C. Image-only queue (needs decks/models open)", ""]
    for it in con.image_only:
        lines.append(f"- {it['id']} · {it['doc']} slide {it.get('slide')}")
    return "\n".join(lines).rstrip() + "\n"
