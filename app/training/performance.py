"""Planned-versus-actual evaluation of a structured (ERG) workout.

Inputs are the planned steps and the recorded 1-Hz power. Alignment assumes
the recording starts with the first planned step. That holds for ERG
workouts started from icTrainer, which drive the trainer from t = 0.
Phase 7 can pass a different offset when the Intervals.icu interval
detection says otherwise (``start_offset_s``).

Definitions (see docs/TRAINING_ENGINE.md):

* A step's **deviation** = (mean actual power - target midpoint) / midpoint x 100,
  over the seconds that have power data.
* A step is **completed** if at least ``STEP_MIN_RECORDED`` of its planned
  seconds have data and |deviation| ≤ ``STEP_DEVIATION_TOLERANCE_PCT``.
* Workout **completion %** = recorded seconds inside the planned span /
  planned seconds x 100.
* **Work completion %** = actual kJ / planned kJ (target midpoints) inside the plan.
* Workout **power deviation %** = mean step deviation, weighted by each step's
  planned duration, over steps that have a power target and data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

STEP_MIN_RECORDED = 0.90
STEP_DEVIATION_TOLERANCE_PCT = 5.0
MAX_COMPLETION_PCT = 200.0  # matches the workout_performance CHECK constraint

StepStatus = Literal["completed", "under_target", "over_target", "incomplete", "not_started"]


@dataclass(frozen=True)
class PlannedStep:
    duration_s: int
    target_low_watts: float | None = None  # None = free ride / no power target
    target_high_watts: float | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError("step duration must be positive")
        low, high = self.target_low_watts, self.target_high_watts
        if (low is None) != (high is None):
            raise ValueError("give both target bounds or neither")
        if low is not None and high is not None and not 0 < low <= high:
            raise ValueError("targets must satisfy 0 < low <= high")

    @property
    def target_mid_watts(self) -> float | None:
        if self.target_low_watts is None or self.target_high_watts is None:
            return None
        return (self.target_low_watts + self.target_high_watts) / 2


@dataclass(frozen=True)
class StepResult:
    index: int
    label: str
    planned_s: int
    recorded_s: int
    avg_watts: float | None
    target_mid_watts: float | None
    deviation_pct: float | None
    in_range_fraction: float | None
    status: StepStatus


@dataclass(frozen=True)
class WorkoutEvaluation:
    planned_s: int
    recorded_s: int
    completion_pct: float
    work_completion_pct: float | None
    power_deviation_pct: float | None
    steps: tuple[StepResult, ...]
    extra_recorded_s: int  # data after the planned end (cool-down ride-on etc.)
    notes: tuple[str, ...]

    @property
    def completed_steps(self) -> int:
        return sum(1 for s in self.steps if s.status == "completed")


def _step_result(index: int, step: PlannedStep, window: Sequence[float | None]) -> StepResult:
    values = [v for v in window if v is not None]
    recorded = len(values)
    mid = step.target_mid_watts
    avg = sum(values) / recorded if recorded else None
    deviation = None
    in_range = None
    if mid is not None and avg is not None:
        deviation = (avg - mid) / mid * 100
        low, high = step.target_low_watts, step.target_high_watts
        assert low is not None and high is not None
        in_range = sum(1 for v in values if low <= v <= high) / recorded

    status: StepStatus
    if recorded == 0:
        status = "not_started"
    elif recorded < STEP_MIN_RECORDED * step.duration_s:
        status = "incomplete"
    elif deviation is None or abs(deviation) <= STEP_DEVIATION_TOLERANCE_PCT:
        status = "completed"
    elif deviation < 0:
        status = "under_target"
    else:
        status = "over_target"
    return StepResult(
        index, step.label, step.duration_s, recorded, avg, mid, deviation, in_range, status
    )


def evaluate_workout(
    steps: Sequence[PlannedStep],
    power_samples: Sequence[float | None],
    *,
    start_offset_s: int = 0,
) -> WorkoutEvaluation:
    if not steps:
        raise ValueError("a workout needs at least one step")
    if start_offset_s < 0:
        raise ValueError("start offset must be >= 0")
    for value in power_samples:
        if value is not None and value < 0:
            raise ValueError("negative power sample")

    samples = list(power_samples[start_offset_s:])
    results: list[StepResult] = []
    cursor = 0
    planned_kj = 0.0
    actual_kj = 0.0
    has_targets = False
    for i, step in enumerate(steps):
        window = samples[cursor : cursor + step.duration_s]
        results.append(_step_result(i, step, window))
        mid = step.target_mid_watts
        if mid is not None:
            has_targets = True
            planned_kj += mid * step.duration_s / 1000
            actual_kj += sum(v for v in window if v is not None) / 1000
        cursor += step.duration_s

    planned_s = cursor
    recorded_s = sum(r.recorded_s for r in results)
    extra = sum(1 for v in samples[planned_s:] if v is not None)
    completion = min(MAX_COMPLETION_PCT, recorded_s / planned_s * 100)
    work_completion = (
        min(MAX_COMPLETION_PCT, actual_kj / planned_kj * 100)
        if has_targets and planned_kj > 0
        else None
    )
    weighted = [(r.deviation_pct, r.planned_s) for r in results if r.deviation_pct is not None]
    deviation = sum(d * w for d, w in weighted) / sum(w for _, w in weighted) if weighted else None
    notes: list[str] = []
    if not has_targets:
        notes.append("no power targets in the plan; deviation not computed")
    if recorded_s == 0:
        notes.append("no power data inside the planned span")
    return WorkoutEvaluation(
        planned_s=planned_s,
        recorded_s=recorded_s,
        completion_pct=completion,
        work_completion_pct=work_completion,
        power_deviation_pct=deviation,
        steps=tuple(results),
        extra_recorded_s=extra,
        notes=tuple(notes),
    )
