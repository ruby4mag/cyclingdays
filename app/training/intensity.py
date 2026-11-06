"""Ordered training-intensity levels shared by readiness, planning and validation.

Each level is the highest *training stimulus* a session may contain, keyed to
the power zones in ``zones.py``: recovery = Z1, endurance = Z2, tempo = Z3,
threshold = Z4, vo2max = Z5 and above. Sweet spot (88-94 % FTP) reaches into
Z4 and therefore counts as threshold.
"""

from __future__ import annotations

from enum import IntEnum


class Intensity(IntEnum):
    REST = 0
    RECOVERY = 1
    ENDURANCE = 2
    TEMPO = 3
    THRESHOLD = 4
    VO2MAX = 5

    @property
    def label(self) -> str:
        return self.name.lower()


def cap(*levels: Intensity) -> Intensity:
    """The most restrictive of the given caps."""
    return min(levels) if levels else Intensity.VO2MAX
