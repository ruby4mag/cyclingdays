"""Structured-output schemas for the coaching agents.

These are the *only* shapes the LLM may return. They are sent to OpenAI as
strict JSON schemas (``responses.parse``) and validated again by Pydantic on
receipt. Every field is required (strict mode); "no value" is ``null``.

Bounds here are a first, structural line of defence. The binding safety
checks are deterministic and live in ``app/graph/guards.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.training.sessions import SESSION_INTENSITY, SessionType, intensity_from_label

IntensityLabel = Literal["rest", "recovery", "endurance", "tempo", "threshold", "vo2max"]

# Session types live with the deterministic rules (app/training/sessions.py).
__all__ = [
    "SESSION_INTENSITY",
    "IntensityLabel",
    "PerformanceReview",
    "RecoveryAssessment",
    "SessionType",
    "TrainingAnalysis",
    "TrainingObjective",
    "WorkoutDraft",
    "WorkoutStep",
    "intensity_from_label",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrainingAnalysis(_Strict):
    summary: str = Field(max_length=1500)
    load_trend: Literal["building", "maintaining", "declining", "insufficient_data"]
    fatigue_level: Literal["low", "moderate", "high", "unknown"]
    key_observations: list[str] = Field(max_length=8)
    concerns: list[str] = Field(max_length=8)
    data_quality_notes: list[str] = Field(max_length=8)


class RecoveryAssessment(_Strict):
    summary: str = Field(max_length=1500)
    recovery_state: Literal["well_recovered", "adequately_recovered", "under_recovered", "unknown"]
    # May only lower the deterministic cap; a higher value is clamped (guards.py).
    recommended_max_intensity: IntensityLabel
    reasons: list[str] = Field(max_length=8)


class TrainingObjective(_Strict):
    session_type: SessionType
    target_duration_minutes: int = Field(ge=0, le=600)
    primary_goal: str = Field(max_length=500)
    rationale: str = Field(max_length=1500)
    long_term_link: str = Field(max_length=500)


class WorkoutStep(_Strict):
    kind: Literal["warmup", "steady", "work", "recovery", "cooldown"]
    duration_seconds: int = Field(ge=10, le=14400)
    power_low_pct_ftp: int = Field(ge=30, le=160)
    power_high_pct_ftp: int = Field(ge=30, le=160)
    cadence_rpm: int | None = Field(ge=50, le=130)
    note: str = Field(max_length=200)

    @model_validator(mode="after")
    def _ordered(self) -> WorkoutStep:
        if self.power_low_pct_ftp > self.power_high_pct_ftp:
            raise ValueError("power_low_pct_ftp must be <= power_high_pct_ftp")
        return self


class WorkoutDraft(_Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=1500)
    steps: list[WorkoutStep] = Field(min_length=1, max_length=80)

    @property
    def total_seconds(self) -> int:
        return sum(s.duration_seconds for s in self.steps)


class PerformanceReview(_Strict):
    """LLM review of one completed ride. It can only recommend holding or easing off."""

    summary: str = Field(max_length=1500)
    went_well: list[str] = Field(max_length=6)
    concerns: list[str] = Field(max_length=6)
    # No "increase" option by design: progression comes from the plan, never
    # from a single completed workout.
    recommendation: Literal["keep", "reduce", "recover"]
    next_session_note: str = Field(max_length=500)
