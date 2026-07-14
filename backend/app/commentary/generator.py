"""Compose the deterministic commentary sections and export md/json."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .. import config

_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)

# Fallback consequence phrasing when a rule carries no `consequence:` field.
_GENERIC_CONSEQUENCE = {
    "blocker": "This is a blocking manufacturability risk that must be resolved before tooling.",
    "major": "This is a significant manufacturability concern that should be corrected.",
    "minor": "This is a minor manufacturability concern to review.",
    "info": "This is an informational observation.",
}

_DEFAULT_SCORE_BANDS = [
    {"min": 90, "label": "ready for tooling kickoff with minor follow-ups"},
    {"min": 75, "label": "close to ready — a few corrections needed"},
    {"min": 50, "label": "significant DFM work remains"},
    {"min": 0, "label": "major redesign required before tooling"},
]


@dataclass
class CommentarySection:
    id: str
    title: str
    paragraphs: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _render(name: str, **ctx: Any) -> list[str]:
    """Render a fragment and split it into non-empty paragraphs."""
    text = _ENV.get_template(name).render(**ctx)
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _score_band(score: float | None, bands: list[dict]) -> str | None:
    if score is None:
        return None
    for band in sorted(bands, key=lambda b: b.get("min", 0), reverse=True):
        if score >= band.get("min", 0):
            return band.get("label")
    return None


def _is_capability_flag(row: dict) -> bool:
    return bool(row.get("supplier_adjustable")) and "placeholder" in (row.get("note") or "").lower()


def build_commentary(
    report: dict, show_manual: bool = True, show_strip: bool = True
) -> list[CommentarySection]:
    summary = report.get("summary", {}) or {}
    results = summary.get("results", []) or []
    counts = summary.get("counts", {}) or {}
    proposed = summary.get("proposed", []) or []
    manual_params = summary.get("manual_check_parameters", []) or []
    corrections = report.get("corrections", []) or []
    corr_by_rule = {c.get("rule_id"): c for c in corrections}

    bands = (report.get("commentary_config") or {}).get("score_bands") or _DEFAULT_SCORE_BANDS
    score = summary.get("readiness_score")

    fails = [r for r in results if r.get("verdict") == "fail"]
    flags = [r for r in results if r.get("verdict") == "flag"]
    cap_flags = [r for r in flags if _is_capability_flag(r)]
    margin_flags = [r for r in flags if not _is_capability_flag(r)]

    sections: list[CommentarySection] = []

    # 1. Executive summary --------------------------------------------------
    sections.append(
        CommentarySection(
            id="summary",
            title="Executive summary",
            paragraphs=_render(
                "summary.j2",
                part_name=report.get("part_name"),
                family=report.get("family"),
                ruleset_version=report.get("ruleset_version"),
                criteria_version=report.get("criteria_version"),
                score=score,
                band_label=_score_band(score, bands),
                counts=counts,
            ),
        )
    )

    # 2. Critical findings --------------------------------------------------
    if fails:
        paragraphs: list[str] = []
        items: list[dict] = []
        for r in fails:
            consequence = r.get("consequence") or _GENERIC_CONSEQUENCE.get(
                r.get("severity", "major"), _GENERIC_CONSEQUENCE["major"]
            )
            recommendation = (corr_by_rule.get(r.get("rule_id")) or {}).get("recommendation")
            paragraphs.extend(
                _render(
                    "critical_item.j2",
                    parameter=r.get("parameter"),
                    measured=r.get("measured"),
                    limit_detail=r.get("limit_detail"),
                    source=r.get("source"),
                    consequence=consequence,
                    recommendation=recommendation,
                )
            )
            items.append(
                {
                    "rule_id": r.get("rule_id"),
                    "parameter": r.get("parameter"),
                    "measured": r.get("measured"),
                    "limit_detail": r.get("limit_detail"),
                    "source": r.get("source"),
                    "consequence": consequence,
                    "recommendation": recommendation,
                }
            )
        sections.append(
            CommentarySection("critical", "Critical findings", paragraphs, items)
        )

    # 3. Flagged / marginal items ------------------------------------------
    if flags:
        paragraphs = []
        if cap_flags:
            paragraphs.extend(
                _render("flags_capability.j2", items=[r.get("parameter") for r in cap_flags])
            )
        if margin_flags:
            paragraphs.extend(
                _render("flags_margin.j2", items=[r.get("parameter") for r in margin_flags])
            )
        items = [
            {
                "rule_id": r.get("rule_id"),
                "parameter": r.get("parameter"),
                "kind": "capability" if _is_capability_flag(r) else "margin",
                "source": r.get("source"),
            }
            for r in flags
        ]
        sections.append(
            CommentarySection("flags", "Flagged / marginal items", paragraphs, items)
        )

    # 4. Manual verification list ------------------------------------------
    if show_manual and manual_params:
        limit_by_param = {
            r.get("parameter"): r.get("limit_detail")
            for r in results
            if r.get("verdict") == "manual"
        }
        items = [
            {"parameter": p, "limit_detail": limit_by_param.get(p),
             "text": f"{p} — verify against: {limit_by_param.get(p) or 'drawing/spec'}."}
            for p in manual_params
        ]
        sections.append(
            CommentarySection(
                "manual",
                "Manual verification",
                _render("manual.j2", count=len(manual_params)),
                items,
            )
        )

    # 5. Model thickness observations --------------------------------------
    thickness = report.get("thickness")
    if thickness:
        sections.append(
            CommentarySection(
                "thickness",
                "Model thickness observations",
                _render("thickness.j2", t=thickness),
            )
        )

    # 5b. Flat-state material (Phase 7 flat pattern) ------------------------
    flat = report.get("flat_pattern")
    if flat:
        flat_results = [
            r for r in results if (r.get("rule_id") or "").startswith("STMP-FLAT-")
        ]
        fc = Counter(r.get("verdict") for r in flat_results)
        check_summary = ", ".join(
            f"{fc[v]} {v}" for v in ("fail", "flag", "manual", "pass") if fc.get(v)
        )
        bbox = flat.get("developed_bbox_mm")
        bbox_txt = " x ".join(f"{n:g}" for n in bbox) if bbox else None
        sections.append(
            CommentarySection(
                id="flatpattern",
                title="Flat-state material",
                paragraphs=_render(
                    "flatpattern.j2",
                    fp=flat,
                    bbox=bbox_txt,
                    check_summary=check_summary,
                    reasons="; ".join(flat.get("reasons") or []),
                    assumptions="; ".join(flat.get("assumptions") or []),
                ),
                items=[
                    {"rule_id": r.get("rule_id"), "parameter": r.get("parameter"),
                     "verdict": r.get("verdict")}
                    for r in flat_results
                ],
            )
        )

    # 6. Strip layout notes -------------------------------------------------
    strip = report.get("strip")
    if show_strip and strip:
        sections.append(
            CommentarySection(
                "strip",
                "Strip layout notes",
                _render("strip.j2", s=strip),
                items=[{"text": ri} for ri in (strip.get("review_items") or [])],
            )
        )

    # 7. Proposed criteria observed ----------------------------------------
    if proposed:
        items = [
            {"rule_id": p.get("rule_id"), "parameter": p.get("parameter"),
             "source": p.get("source"),
             "text": f"{p.get('rule_id')} ({p.get('parameter')}) — {p.get('source')}"}
            for p in proposed
        ]
        sections.append(
            CommentarySection(
                "proposed",
                "Proposed criteria observed",
                _render("proposed.j2", count=len(proposed)),
                items,
            )
        )

    # 8. Recommended next steps --------------------------------------------
    steps: list[str] = []
    for r in fails:
        rec = (corr_by_rule.get(r.get("rule_id")) or {}).get("recommendation")
        steps.append(rec or f"Resolve {r.get('parameter')} ({r.get('rule_id')}).")
    for r in cap_flags:
        steps.append(f"Confirm supplier capability for {r.get('parameter')} ({r.get('rule_id')}) at FAI.")
    if show_manual and manual_params:
        steps.append(
            "Manually verify: " + ", ".join(manual_params) + "."
        )
    sections.append(
        CommentarySection(
            "next_steps",
            "Recommended next steps",
            _render("next_steps.j2", has_steps=bool(steps)),
            items=[{"text": s} for s in steps],
        )
    )

    return sections


# --- exports ------------------------------------------------------------------
def _front_matter(report: dict) -> str:
    summary = report.get("summary", {}) or {}
    lines = [
        "---",
        f"part: {report.get('part_name')}",
        f"family: {report.get('family')}",
        f"criteria_version: {report.get('criteria_version')}",
        f"ruleset_version: {report.get('ruleset_version')}",
        f"readiness_score: {summary.get('readiness_score')}",
        f"generated_by: {config.APP_NAME} v{config.APP_VERSION}",
        "---",
    ]
    return "\n".join(lines)


def build_commentary_markdown(
    sections: list[CommentarySection], report: dict, generated_at: str | None = None
) -> str:
    stamp = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[str] = [_front_matter(report), "", f"# DFM commentary — {report.get('part_name')}",
                      "", f"_Generated {stamp}. Deterministic; no AI-authored content._", ""]
    bullet_sections = {"manual", "next_steps", "proposed", "strip"}
    for s in sections:
        out.append(f"## {s.title}")
        out.append("")
        for p in s.paragraphs:
            out.append(p)
            out.append("")
        if s.id in bullet_sections and s.items:
            for it in s.items:
                text = it.get("text")
                if text:
                    out.append(f"- {text}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def build_commentary_json(
    sections: list[CommentarySection], report: dict, generated_at: str | None = None
) -> str:
    summary = report.get("summary", {}) or {}
    envelope = {
        "schema": "dfm-commentary/1",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "part_name": report.get("part_name"),
        "family": report.get("family"),
        "criteria_version": report.get("criteria_version"),
        "ruleset_version": report.get("ruleset_version"),
        "readiness_score": summary.get("readiness_score"),
        "sections": [s.to_dict() for s in sections],
    }
    return json.dumps(envelope, indent=2, ensure_ascii=False)
