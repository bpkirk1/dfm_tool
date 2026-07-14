"""Stage 2 — consolidate curated findings into rule candidates + capability data.

Reads the (human/LLM-curated) ``findings-<supplier>.jsonl`` files and groups them
deterministically: same ``parameter`` across parts/revisions/suppliers becomes a
candidate rule with a ``seen_count`` and evidence list; revision chains that
recorded an accepted value become capability (CTF) data; conflicting requested
values are flagged (keeping the spread); image-only findings are routed to a
manual-review queue. No rule text is written here — that is Stage 3.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .model import Finding, read_jsonl


@dataclass
class RuleCandidate:
    parameter: str
    process: str
    seen_count: int
    evidence: list[str]
    suppliers: list[str]
    values_requested: list[float]
    value_min: float | None
    value_max: float | None
    conflict: bool
    concerns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityDatum:
    parameter: str
    supplier: str
    achieved: float | None
    parts: list[str]
    dates: list[str]
    evidence: list[str]
    resolution: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Consolidation:
    rule_candidates: list[RuleCandidate] = field(default_factory=list)
    capabilities: list[CapabilityDatum] = field(default_factory=list)
    image_only: list[dict] = field(default_factory=list)
    workflow: list[dict] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_candidates": [r.to_dict() for r in self.rule_candidates],
            "capabilities": [c.to_dict() for c in self.capabilities],
            "image_only": self.image_only,
            "workflow": self.workflow,
            "source_counts": self.source_counts,
        }


def load_findings(paths: Iterable[str | Path]) -> list[Finding]:
    out: list[Finding] = []
    for p in paths:
        out.extend(read_jsonl(p))
    return out


def find_findings_files(folder: str | Path) -> list[Path]:
    return sorted(Path(folder).glob("findings-*.jsonl"))


def _mode(values: list[str], default: str = "unknown") -> str:
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else default


def build_consolidation(findings: list[Finding]) -> Consolidation:
    con = Consolidation()
    con.source_counts = dict(Counter(f.supplier for f in findings))

    # Group by parameter for rule candidates.
    by_param: dict[str, list[Finding]] = {}
    for f in findings:
        if f.confidence == "image-only":
            con.image_only.append(
                {"id": f.id, "supplier": f.supplier, "doc": f.doc, "slide": f.slide,
                 "concern": f.concern}
            )
            continue
        if not f.parameter:
            con.workflow.append(
                {"id": f.id, "supplier": f.supplier, "process": f.process,
                 "concern": f.concern}
            )
            continue
        by_param.setdefault(f.parameter, []).append(f)

    for param in sorted(by_param):
        group = by_param[param]
        evidence = sorted({f.id for f in group})
        suppliers = sorted({f.supplier for f in group})
        requested = sorted({f.value_requested for f in group if f.value_requested is not None})
        con.rule_candidates.append(
            RuleCandidate(
                parameter=param,
                process=_mode([f.process for f in group]),
                seen_count=len(evidence),
                evidence=evidence,
                suppliers=suppliers,
                values_requested=list(requested),
                value_min=min(requested) if requested else None,
                value_max=max(requested) if requested else None,
                conflict=len(requested) > 1,
                concerns=[f.concern for f in group[:3]],
            )
        )
    # Strongest corroboration first, then alphabetical for determinism.
    con.rule_candidates.sort(key=lambda r: (-r.seen_count, r.parameter))

    # Capability data: any finding recording an accepted value.
    cap_groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        if f.value_accepted is not None:
            cap_groups.setdefault((f.parameter or "", f.supplier), []).append(f)
    for (param, supplier) in sorted(cap_groups):
        group = cap_groups[(param, supplier)]
        con.capabilities.append(
            CapabilityDatum(
                parameter=param or "(unspecified)",
                supplier=supplier,
                achieved=group[0].value_accepted,
                parts=sorted({f.part for f in group if f.part}),
                dates=sorted({f.date for f in group if f.date}),
                evidence=sorted({f.id for f in group}),
                resolution=_mode([f.resolution for f in group]),
            )
        )

    return con


def render_markdown(con: Consolidation) -> str:
    total = sum(con.source_counts.values())
    lines: list[str] = ["# Stage 2 — Consolidated Supplier DFM Findings", ""]
    src = ", ".join(f"{k} ({v})" for k, v in sorted(con.source_counts.items()))
    lines.append(f"Sources: {src} = **{total} findings**. Nothing here is a rule yet "
                 f"— Stage 3 converts approved items to `status: proposed` YAML + CTF entries.")
    lines.append("")
    lines.append("## 1. Candidate rules (grouped by parameter)")
    lines.append("")
    lines.append("| Parameter | Process | seen | Suppliers | Requested values | Conflict | Evidence |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in con.rule_candidates:
        vals = ", ".join(f"{v:g}" for v in r.values_requested) or "—"
        lines.append(
            f"| `{r.parameter}` | {r.process} | {r.seen_count} | {', '.join(r.suppliers)} "
            f"| {vals} | {'**yes**' if r.conflict else 'no'} | {', '.join(r.evidence)} |"
        )
    lines.append("")
    lines.append("## 2. Demonstrated capability (CTF candidates)")
    lines.append("")
    lines.append("| Parameter | Supplier | Achieved | Parts | Evidence | Resolution |")
    lines.append("|---|---|---|---|---|---|")
    for c in con.capabilities:
        ach = f"{c.achieved:g}" if c.achieved is not None else "—"
        lines.append(
            f"| `{c.parameter}` | {c.supplier} | {ach} | {', '.join(c.parts) or '—'} "
            f"| {', '.join(c.evidence)} | {c.resolution} |"
        )
    lines.append("")
    lines.append(f"## 3. Image-only queue (manual review) — {len(con.image_only)} item(s)")
    lines.append("")
    for it in con.image_only:
        lines.append(f"- {it['id']} · {it['doc']} slide {it.get('slide')} — {it['concern']}")
    lines.append("")
    lines.append(f"## 4. Workflow / non-parametric — {len(con.workflow)} item(s)")
    lines.append("")
    for it in con.workflow:
        lines.append(f"- {it['id']} ({it['process']}) — {it['concern']}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
