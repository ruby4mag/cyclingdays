"""Deterministic training calculations (Phase 3) and workout validation (Phase 5).

Everything here is pure Python with no I/O and no LLM involvement. Formulas
and assumptions are documented in ``docs/TRAINING_ENGINE.md``. Any change to a
formula must bump ``CALCULATION_VERSION`` so stored metrics stay reproducible.

Versions:
* ``v1`` (Phase 3): initial formulas.
* ``v2`` (Phase 6): same formulas; duplicate recordings of one ride (see
  ``activity_dedup``) are excluded from load, so totals can differ from v1.
"""

CALCULATION_VERSION = "v2"
