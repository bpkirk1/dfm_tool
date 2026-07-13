"""Drawing (PDF) reader — pulls the text layer for notes, tolerances, and
ballooned CTF callouts.

Phase 1 reads the embedded text layer with ``pdfplumber`` (most of these
drawings carry a clean one). OCR / vision is a later enhancement. The output is
descriptive context for the report and a list of detected CTF balloons; it does
not by itself drive pass/fail (the deterministic engine does).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# "NOTE 3:" / "3." style note leads; tolerance callouts like 0.080 +/-0.005.
_NOTE_RE = re.compile(r"^\s*(?:NOTE\s*)?(\d{1,2})[.)]\s+(.*\S)", re.IGNORECASE)
_TOL_RE = re.compile(
    r"(\d+\.\d+)\s*(?:\+/-|±|\+-)\s*(\d+\.\d+)"
)
_ANGLE_TOL_RE = re.compile(
    r"(\d{1,3})\s*(?:°|deg)\s*\+?\s*(\d+)\s*/\s*-?\s*(\d+)", re.IGNORECASE
)


@dataclass
class DrawingData:
    source_file: str
    available: bool = True
    page_count: int = 0
    notes: list[str] = field(default_factory=list)
    tolerances: list[dict[str, float]] = field(default_factory=list)
    angle_tolerances: list[dict[str, float]] = field(default_factory=list)
    ctf_balloons: list[str] = field(default_factory=list)
    raw_text_chars: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "available": self.available,
            "page_count": self.page_count,
            "notes": self.notes,
            "tolerances": self.tolerances,
            "angle_tolerances": self.angle_tolerances,
            "ctf_balloons": self.ctf_balloons,
            "raw_text_chars": self.raw_text_chars,
            "warnings": self.warnings,
        }


def extract_pdf(path: str | Path) -> DrawingData:
    path = Path(path)
    data = DrawingData(source_file=path.name)

    try:
        import pdfplumber  # imported lazily so the engine works without it
    except ImportError:
        data.available = False
        data.warnings.append("pdfplumber not installed — drawing text not parsed.")
        return data

    try:
        with pdfplumber.open(str(path)) as pdf:
            data.page_count = len(pdf.pages)
            text_parts = [(pg.extract_text() or "") for pg in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - report, don't crash a run
        data.available = False
        data.warnings.append(f"Could not read PDF: {exc}")
        return data

    full_text = "\n".join(text_parts)
    data.raw_text_chars = len(full_text)
    if not full_text.strip():
        data.warnings.append(
            "No text layer found — drawing may be scanned. OCR is a later phase."
        )

    for line in full_text.splitlines():
        m = _NOTE_RE.match(line)
        if m:
            data.notes.append(f"Note {m.group(1)}: {m.group(2)}")
        for t in _TOL_RE.finditer(line):
            data.tolerances.append(
                {"nominal": float(t.group(1)), "tol": float(t.group(2))}
            )
        for a in _ANGLE_TOL_RE.finditer(line):
            data.angle_tolerances.append(
                {
                    "target": float(a.group(1)),
                    "plus": float(a.group(2)),
                    "minus": float(a.group(3)),
                }
            )

    return data
