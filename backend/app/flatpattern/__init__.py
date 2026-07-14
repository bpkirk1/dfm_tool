"""Phase 7 — flat-pattern (developed blank) module.

Public surface:
    develop_flat_pattern(step_text, flat_cfg)  -> FlatPattern
    compute_flat_features(FlatPattern)         -> (features, details)
    analyze_flat(step_path_or_text, flat_cfg)  -> FlatResult (convenience)
    render_svg / render_png / render_dxf
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import compute_flat_features
from .render import render_dxf, render_png, render_svg
from .unfold import FlatPattern, develop_flat_pattern

__all__ = [
    "FlatPattern",
    "FlatResult",
    "develop_flat_pattern",
    "compute_flat_features",
    "analyze_flat",
    "render_svg",
    "render_png",
    "render_dxf",
]


@dataclass
class FlatResult:
    """Everything a caller needs after a flat-pattern run."""

    flat_pattern: FlatPattern
    features: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


def _read_step(source: str) -> tuple[str, str]:
    """Accept either raw STEP text or a path; return (text, source_file)."""
    if "\n" in source or "ISO-10303" in source[:200]:
        return source, ""
    p = Path(source)
    if p.exists():
        return p.read_text(encoding="utf-8", errors="ignore"), p.name
    return source, ""


def analyze_flat(step_path_or_text: str, flat_cfg: dict[str, Any] | None = None) -> FlatResult:
    text, source = _read_step(step_path_or_text)
    fp = develop_flat_pattern(text, flat_cfg, source_file=source)
    features, details = compute_flat_features(fp)
    return FlatResult(flat_pattern=fp, features=features, details=details)
