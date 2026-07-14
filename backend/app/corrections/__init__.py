"""Deterministic DFM correction advisor.

For every ``fail``/``flag`` finding, compute what the design would need to change
to comply, and export the set as a machine-readable fix file. No LLM, no
inference beyond the rule/feature facts already present — every recommendation is
template-built and cites its rule source (provenance).
"""
from .advisor import (
    Correction,
    build_corrections,
    build_envelope,
    compute_target,
    export_fixes_json,
    export_fixes_yaml,
)

__all__ = [
    "Correction",
    "build_corrections",
    "build_envelope",
    "compute_target",
    "export_fixes_json",
    "export_fixes_yaml",
]
