"""Deterministic supplier-feedback miner (Phase 6 tooling).

Automates the mechanical parts of the staged workflow in
``new_suggestions/06-supplier-feedback-mining.md``:

* Stage 1 (extract): parse .pptx/.xlsx/.eml/.pdf into a provenance-rich findings
  scaffold — never inventing a concern or value.
* Stage 2 (consolidate): group curated findings by parameter into rule
  candidates + capability (CTF) data, flag conflicts, queue image-only items.
* Stage 3 (emit): write proposed-rules.yaml, CTF entries, and a REVIEW_QUEUE.md —
  all ``status: proposed`` for human approval. The miner never activates a rule.

Run with ``python -m app.mining <extract|consolidate|emit> ...``.
"""
from . import consolidate, emit, extract, model
from .consolidate import Consolidation, build_consolidation
from .model import Finding

__all__ = [
    "model", "extract", "consolidate", "emit",
    "Finding", "Consolidation", "build_consolidation",
]
