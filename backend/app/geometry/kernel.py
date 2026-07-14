"""Optional CAD-kernel bridge (cadquery / OCP-OpenCASCADE).

The core tool is deliberately dependency-free: STEP is parsed as text and
unmeasurable features are reported honestly. This module is the *only* place a
CAD kernel is touched, and it is entirely optional. Following the lazy-import
pattern in ``extractors/pdf_extractor.py``, cadquery is imported at module load;
if it (or its OCP backend) is missing, ``available`` is False and every entry
point degrades gracefully instead of crashing.

Install the extra with::

    pip install -r requirements-geometry.txt
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

UNAVAILABLE_MESSAGE = (
    "geometry correction unavailable — install the optional CAD kernel "
    "(pip install -r requirements-geometry.txt)"
)

try:  # imported lazily so the whole app works without a CAD kernel
    import cadquery as cq  # type: ignore

    available = True
    import_error: str | None = None
except Exception as exc:  # ImportError, or OCP shared-lib load failures
    cq = None  # type: ignore
    available = False
    import_error = f"{type(exc).__name__}: {exc}"


class KernelUnavailable(RuntimeError):
    """Raised when a kernel operation is attempted without cadquery installed."""


def require() -> None:
    if not available:
        raise KernelUnavailable(UNAVAILABLE_MESSAGE)


def status() -> dict[str, Any]:
    """Machine-readable availability, safe to expose to the UI/API."""
    return {
        "available": available,
        "message": None if available else UNAVAILABLE_MESSAGE,
        "detail": import_error,
        "backend": "cadquery" if available else None,
    }


def load_step(path: str | Path):
    """Import a STEP file into a cadquery Workplane/Shape. Kernel required."""
    require()
    return cq.importers.importStep(str(path))  # type: ignore[union-attr]


def save_step(model, path: str | Path) -> Path:
    """Export a cadquery model to STEP. Never called on the input path."""
    require()
    out = Path(path)
    cq.exporters.export(model, str(out))  # type: ignore[union-attr]
    return out
