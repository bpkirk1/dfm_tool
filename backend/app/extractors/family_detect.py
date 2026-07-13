"""Heuristic process-family detection from the uploaded inputs.

Deterministic and cheap: looks at filenames and any extracted drawing text for
stamping vs molding signals. The engineer can always override the pick in the UI.
"""
from __future__ import annotations

from .pdf_extractor import DrawingData

_STAMP_HINTS = ("strip", "leadframe", "lead-frame", "stamp", "pierce", "coin", "carrier", "sig-t")
_MOLD_HINTS = ("mold", "mould", "plastic", "resin", "flash", "gate", "draft", "sink", "center-strip", "_asm")
_CNC_HINTS = ("cnc", "machin", "backing-plate", "backing_plate", "endcap", "end-cap", "milled")


def detect_family(
    pdf_name: str = "",
    step_name: str = "",
    drawing: DrawingData | None = None,
    default: str = "stamping",
) -> str:
    haystack = f"{pdf_name} {step_name}".lower()
    if drawing:
        haystack += " " + " ".join(drawing.notes).lower()

    scores = {
        "stamping": sum(h in haystack for h in _STAMP_HINTS),
        "molding": sum(h in haystack for h in _MOLD_HINTS),
        "cnc_machining": sum(h in haystack for h in _CNC_HINTS),
    }
    best = max(scores, key=scores.get)
    # Only pick a non-default family when there's a positive, unambiguous signal.
    if scores[best] > 0 and list(scores.values()).count(scores[best]) == 1:
        return best
    return default
