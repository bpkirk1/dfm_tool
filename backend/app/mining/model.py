"""Shared data model + deterministic detectors for the supplier-feedback miner.

The miner is deliberately *mechanical*: it parses documents, preserves provenance
(doc + slide + date + supplier), detects numbers and parameter keywords, and
classifies process — but it never invents a concern, a value, or a resolution.
Semantic judgment (final wording, operator/severity, activation) stays with the
human reviewer, consistent with the tool's deterministic-core rule.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# --- Finding schema (mirrors new_suggestions/mining/findings-<supplier>.jsonl) --
_FIELDS = (
    "id", "part", "supplier", "doc", "slide", "date", "revision", "process",
    "concern", "parameter", "value_requested", "value_accepted", "our_response",
    "resolution", "confidence", "note",
)


@dataclass
class Finding:
    id: str
    supplier: str
    doc: str
    slide: int | None = None
    date: str | None = None
    revision: str | None = None
    process: str = "unknown"
    part: str | None = None
    concern: str = ""
    parameter: str | None = None
    value_requested: float | None = None
    value_accepted: float | None = None
    our_response: str | None = None
    resolution: str = "unknown"
    confidence: str = "text"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Stable key order matching the existing findings files.
        return {k: d.get(k) for k in _FIELDS}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        known = {k: d.get(k) for k in _FIELDS if k in d}
        return cls(**known)  # type: ignore[arg-type]


def read_jsonl(path: str | Path) -> list[Finding]:
    out: list[Finding] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(Finding.from_dict(json.loads(line)))
    return out


def write_jsonl(findings: Iterable[Finding], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(f.to_dict(), ensure_ascii=False) + "\n")
    return p


# --- deterministic detectors --------------------------------------------------
_SUPPLIER_KEYWORDS = {
    "AJET": ("ajet",),
    "Polygon": ("polygon", "pgcd"),
    "Hoky": ("hoky",),
    "SMTG": ("smtg",),
}

# Order matters: more specific processes first so a mixed deck lands sensibly.
_PROCESS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plating", ("plating", "pdni", "selective plat", "au30", "au 30", "electroplat")),
    ("molding", ("mold", "molded", "gate", "weld line", "weld-line", "ejector",
                 "runner", "cavity", "draft", "shrink", "resin", "lcp", "insert mold",
                 "sub-gate", "subgate", "vestige above", "flash")),
    ("cnc", ("cnc", "machining", "milling", "endcap", "backing-plate", "backing plate")),
    ("stamping", ("stamp", "blank", "pierce", "bend", "carrier", "tie bar", "tie-bar",
                  "tiebar", "coin", "burr", "strip", "pilot", "form", "v-notch",
                  "vcut", "v-cut", "standoff", "stand-off")),
    ("assembly", ("assembly", "clearance", "mating", "insertion", "interference")),
)

# keyword -> candidate parameter name (hint only; reviewer confirms).
_PARAM_HINTS: tuple[tuple[str, str], ...] = (
    ("inside radius", "min_inside_corner_radius_mm"),
    ("corner radius", "min_inside_corner_radius_mm"),
    ("right angle", "min_inside_corner_radius_mm"),
    ("burr", "burr_height"),
    ("tie bar", "tie_bar_width_mm"),
    ("tie-bar", "tie_bar_width_mm"),
    ("vestige", "tie_bar_vestige_position_tol_mm"),
    ("draft", "release_draft_min_deg"),
    ("wall thickness", "wall_thickness_min_mm"),
    ("min wall", "wall_thickness_min_mm"),
    ("weld line", "weldline_hole_radius_min_mm"),
    ("slot width", "slot_width_min_mm"),
    ("gate", "gate_residual_max_mm"),
    ("pierce", "min_pierced_width_or_dia"),
    ("feature to edge", "feature_to_edge"),
    ("flat", "bend_flat_length_min_mm"),
    ("unfold", "flat_pattern_feature_gap_mm"),
    ("plating width", "plating_strip_width_max_mm"),
    ("strip width", "strip_width_to_thickness_ratio"),
    ("true position", "molded_pin_hole_true_position_mm"),
    ("undercut", "undercut_wall_angle_deg"),
    ("shutoff", "shutoff_width_mm"),
    ("clearance", "assembly_clearance_mm"),
)

_RESOLUTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("accepted", ("accepted", "agree", "ok --sd", "approved", "confirmed --sd")),
    ("rejected", ("rejected", "not acceptable", "cannot", "declined")),
    ("compromised", ("compromise", "instead", "alternative")),
)

_NUM_UNIT = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*(mm|deg|°|µm|um|micro|uin|u-in|%)?", re.IGNORECASE
)
# YYYYMMDD, YYYY-MM-DD, YYYY.MM.DD, YYYY_MM_DD
_DATE_RE = re.compile(r"(20\d{2})[._-]?(\d{2})[._-]?(\d{2})")
_REV_RE = re.compile(
    r"(sdcomments|sd\s*comments|rev\.?\s*\d+|x\d\b|concept|a\d\b)", re.IGNORECASE
)


def detect_supplier(name: str, default: str = "unknown") -> str:
    low = name.lower()
    for supplier, keys in _SUPPLIER_KEYWORDS.items():
        if any(k in low for k in keys):
            return supplier
    return default


def detect_date(name: str) -> str | None:
    m = _DATE_RE.search(name)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    try:
        mi, di = int(mo), int(d)
        if 1 <= mi <= 12 and 1 <= di <= 31:
            return f"{y}-{mo}-{d}"
    except ValueError:
        pass
    return None


def detect_revision(name: str) -> str | None:
    m = _REV_RE.search(name)
    return m.group(0).strip() if m else None


def detect_process(text: str, default: str = "unknown") -> str:
    low = text.lower()
    for process, keys in _PROCESS_KEYWORDS:
        if any(k in low for k in keys):
            return process
    return default


def detect_parameter_hints(text: str) -> list[str]:
    low = text.lower()
    hits: list[str] = []
    for kw, param in _PARAM_HINTS:
        if kw in low and param not in hits:
            hits.append(param)
    return hits


def detect_numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM_UNIT.finditer(text):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        # Ignore bare years and obviously non-dimensional big ints.
        if 2020 <= val <= 2099 and m.group(2) in (None, ""):
            continue
        out.append(val)
    return out


def guess_resolution(text: str, default: str = "unknown") -> str:
    low = text.lower()
    for res, keys in _RESOLUTION_KEYWORDS:
        if any(k in low for k in keys):
            return res
    return default
