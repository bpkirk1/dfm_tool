"""Deterministic engineering-commentary generator.

Turns a built report dict into reviewer-style prose. Every sentence is
template-rendered from structured data already in the report (verdicts, margins,
Phase 3 corrections, thickness, strip, provenance) — no LLM, no network. Same
input -> byte-identical output apart from the timestamp. Wording lives in
``commentary/templates/*.j2`` so it can change without touching Python.
"""
from .generator import (
    CommentarySection,
    build_commentary,
    build_commentary_json,
    build_commentary_markdown,
)

__all__ = [
    "CommentarySection",
    "build_commentary",
    "build_commentary_json",
    "build_commentary_markdown",
]
