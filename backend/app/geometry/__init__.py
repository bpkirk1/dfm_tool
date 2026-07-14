"""Optional geometry-correction backend (CAD kernel).

Entirely optional: if cadquery/OCP is not installed, ``kernel.available`` is
False and the rest of the app is unaffected. See ``kernel`` for the bridge and
``corrector`` for the conservative, self-verifying fix-file applier.
"""
from . import kernel
from .corrector import CorrectionResult, HANDLERS, apply_fixes

__all__ = ["kernel", "CorrectionResult", "HANDLERS", "apply_fixes"]
