"""Structured ERG workouts: the deterministic representation of a planned day.

``build_workout`` turns a workout draft (LLM or fallback, already in % FTP)
into a :class:`StructuredWorkout` with:

* name, date, duration and the FTP reference used for the watts;
* the objective it serves;
* sections (warmup, main, recovery, cooldown) and every step's target power
  as % FTP and in watts;
* an estimate of NP, IF and TSS.

The estimate treats each step as steady at the midpoint of its target range:
NP = (sum(t * P^4) / T)^(1/4), IF = NP / FTP, TSS = hours * IF^2 * 100. This
is the usual estimate for a planned ERG workout, and it matches Intervals.icu's
own estimate on a real workout (TSS 42 vs 42, IF 0.637 vs 0.635).

Pure functions; no I/O.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.training.sessions import SessionType
from app.training.zones import validate_ftp, zone_number

StepKind = Literal["warmup", "steady", "work", "recovery", "cooldown"]
Section = Literal["warmup", "main", "recovery", "cooldown"]

SECTION_OF: dict[str, Section] = {
    "warmup": "warmup",
    "steady": "main",
    "work": "main",
    "recovery": "recovery",
    "cooldown": "cooldown",
}

GENERATOR_VERSION = "v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepInput(_Frozen):
    """One step as planned, in % FTP (from the LLM draft or the fallback)."""

    kind: StepKind
    duration_s: int = Field(ge=1)
    low_pct: int = Field(ge=0)
    high_pct: int = Field(ge=0)
    cadence_rpm: int | None = None
    note: str = ""


class WorkoutStepSpec(_Frozen):
    index: int
    kind: StepKind
    section: Section
    duration_s: int
    low_pct: int
    high_pct: int
    low_watts: int
    high_watts: int
    zone: int  # zone of the upper target (the conservative classification)
    cadence_rpm: int | None
    note: str

    @property
    def mid_pct(self) -> float:
        return (self.low_pct + self.high_pct) / 2


class WorkoutObjective(_Frozen):
    session_type: SessionType
    target_duration_minutes: int
    primary_goal: str
    rationale: str


class WorkoutEstimate(_Frozen):
    normalized_power_watts: float
    intensity_factor: float
    tss: float
    average_power_watts: float


class StructuredWorkout(_Frozen):
    generator_version: str = GENERATOR_VERSION
    athlete_slug: str
    workout_date: dt.date
    name: str
    description: str
    ftp_watts: int
    objective: WorkoutObjective
    steps: tuple[WorkoutStepSpec, ...]
    total_s: int
    sections_s: dict[str, int]
    estimate: WorkoutEstimate

    @property
    def total_minutes(self) -> float:
        return self.total_s / 60

    @property
    def work_steps(self) -> list[WorkoutStepSpec]:
        return [s for s in self.steps if s.kind == "work"]


def _watts(pct: float, ftp: float) -> int:
    return round(pct / 100 * ftp)


def estimate(steps: Iterable[tuple[int, float]], ftp: float) -> WorkoutEstimate:
    """NP/IF/TSS for (duration_s, % FTP) steady steps."""
    pairs = [(t, pct / 100 * ftp) for t, pct in steps if t > 0]
    total = sum(t for t, _ in pairs)
    if total <= 0:
        return WorkoutEstimate(
            normalized_power_watts=0, intensity_factor=0, tss=0, average_power_watts=0
        )
    np_ = (sum(t * p**4 for t, p in pairs) / total) ** 0.25
    avg = sum(t * p for t, p in pairs) / total
    if_ = np_ / ftp
    tss = total / 3600 * if_**2 * 100
    return WorkoutEstimate(
        normalized_power_watts=round(np_, 1),
        intensity_factor=round(if_, 3),
        tss=round(tss, 1),
        average_power_watts=round(avg, 1),
    )


def build_workout(
    *,
    athlete_slug: str,
    workout_date: dt.date,
    name: str,
    description: str,
    ftp_watts: int | None,
    objective: WorkoutObjective,
    steps: Iterable[StepInput],
) -> StructuredWorkout:
    """Deterministic structured workout. Raises ``InvalidFTPError`` without a valid FTP."""
    ftp = validate_ftp(ftp_watts)
    specs: list[WorkoutStepSpec] = []
    for i, step in enumerate(steps, start=1):
        high_w = step.high_pct / 100 * ftp
        specs.append(
            WorkoutStepSpec(
                index=i,
                kind=step.kind,
                section=SECTION_OF[step.kind],
                duration_s=step.duration_s,
                low_pct=step.low_pct,
                high_pct=step.high_pct,
                low_watts=_watts(step.low_pct, ftp),
                high_watts=_watts(step.high_pct, ftp),
                zone=zone_number(high_w, ftp) if high_w > 0 else 1,
                cadence_rpm=step.cadence_rpm,
                note=step.note.strip(),
            )
        )
    sections: dict[str, int] = {"warmup": 0, "main": 0, "recovery": 0, "cooldown": 0}
    for s in specs:
        sections[s.section] += s.duration_s
    return StructuredWorkout(
        athlete_slug=athlete_slug,
        workout_date=workout_date,
        name=name.strip(),
        description=description.strip(),
        ftp_watts=int(ftp),
        objective=objective,
        steps=tuple(specs),
        total_s=sum(s.duration_s for s in specs),
        sections_s=sections,
        estimate=estimate(((s.duration_s, s.mid_pct) for s in specs), ftp),
    )
