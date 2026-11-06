"""Session types and the deterministic rules for each one.

The LLM chooses a session type; these tables decide what that type may
contain. Values are coaching conventions, documented in
``docs/WORKOUTS.md`` and versioned with the validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.training.intensity import Intensity

SessionType = Literal[
    "rest",
    "recovery",
    "endurance",
    "long_endurance",
    "tempo",
    "sweet_spot",
    "threshold",
    "vo2max",
]

# Highest intensity each session type may contain. Sweet spot (88-94 % FTP)
# reaches into Z4 (>= 90 %), so it counts as threshold.
SESSION_INTENSITY: dict[str, Intensity] = {
    "rest": Intensity.REST,
    "recovery": Intensity.RECOVERY,
    "endurance": Intensity.ENDURANCE,
    "long_endurance": Intensity.ENDURANCE,
    "tempo": Intensity.TEMPO,
    "sweet_spot": Intensity.THRESHOLD,
    "threshold": Intensity.THRESHOLD,
    "vo2max": Intensity.VO2MAX,
}


@dataclass(frozen=True, slots=True)
class IntervalRules:
    """Structure of the main set of a structured session."""

    min_work_pct_ftp: int  # a work step must reach at least this target (upper bound)
    work_step_min_s: int
    work_step_max_s: int
    total_work_max_s: int
    min_recovery_ratio: float  # recovery between efforts >= ratio x preceding work


INTERVAL_RULES: dict[str, IntervalRules] = {
    "tempo": IntervalRules(76, 10 * 60, 60 * 60, 90 * 60, 0.0),
    "sweet_spot": IntervalRules(88, 6 * 60, 30 * 60, 75 * 60, 0.15),
    "threshold": IntervalRules(95, 4 * 60, 20 * 60, 60 * 60, 0.25),
    "vo2max": IntervalRules(106, 60, 8 * 60, 30 * 60, 0.5),
}

# Duration bounds per session type (minutes), checked in addition to the
# objective's own target.
SESSION_DURATION_MINUTES: dict[str, tuple[int, int]] = {
    "recovery": (20, 75),
    "endurance": (30, 240),
    "long_endurance": (90, 360),
    "tempo": (45, 180),
    "sweet_spot": (45, 150),
    "threshold": (45, 120),
    "vo2max": (45, 105),
}


def intensity_from_label(label: str) -> Intensity:
    return Intensity[label.upper()]
