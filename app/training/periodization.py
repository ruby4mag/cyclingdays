"""Long-term plan structure (macro/meso cycle phases).

``training_plans.phases`` (JSONB) holds ``{"phases": [...]}`` validated by
``PlanPhases``, plus (Phase 7) the weekly template of day roles and the
builder settings. Phase 3 reads the current phase (intensity cap, weekly
load target). Phase 7 generates plans (``plan_builder.py``) and turns the
plan into a per-day prescription (``progression.py``).
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.training.intensity import Intensity
from app.training.sessions import SessionType

PhaseType = Literal["base", "build", "peak", "taper", "recovery", "transition"]
DayRole = Literal["rest", "recovery", "endurance", "key", "long"]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Phases that limit intensity regardless of readiness. Taper keeps short,
# sharp efforts by design, so it is not capped here.
PHASE_INTENSITY_CAP: dict[str, Intensity] = {
    "recovery": Intensity.ENDURANCE,
    "transition": Intensity.ENDURANCE,
}


class PlanPhase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    type: PhaseType
    start_date: dt.date
    end_date: dt.date
    weekly_tss_target: float | None = Field(default=None, ge=0, le=2000)
    weekly_minutes_target: int | None = Field(default=None, ge=0, le=3000)
    # Most demanding session type allowed on "key" days (Phase 7).
    key_session: SessionType | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _dates(self) -> PlanPhase:
        if self.end_date < self.start_date:
            raise ValueError(f"phase {self.name!r} ends before it starts")
        return self

    @property
    def intensity_cap(self) -> Intensity:
        return PHASE_INTENSITY_CAP.get(self.type, Intensity.VO2MAX)


class PlanPhases(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phases: list[PlanPhase] = Field(default_factory=list)
    # Weekday ("mon".."sun") -> role. None: no per-day prescription.
    week_template: dict[str, DayRole] | None = None
    builder: dict[str, Any] | None = None  # generator version and inputs

    @model_validator(mode="after")
    def _template(self) -> PlanPhases:
        if self.week_template is not None and set(self.week_template) != set(WEEKDAYS):
            raise ValueError(f"week_template needs exactly the keys {WEEKDAYS}")
        return self

    @model_validator(mode="after")
    def _no_overlap(self) -> PlanPhases:
        ordered = sorted(self.phases, key=lambda p: p.start_date)
        for a, b in itertools.pairwise(ordered):
            if b.start_date <= a.end_date:
                raise ValueError(f"phases {a.name!r} and {b.name!r} overlap")
        return self

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> PlanPhases:
        return cls.model_validate(value or {})

    def phase_for(self, day: dt.date) -> PlanPhase | None:
        for phase in self.phases:
            if phase.start_date <= day <= phase.end_date:
                return phase
        return None
