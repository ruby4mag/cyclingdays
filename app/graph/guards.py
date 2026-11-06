"""Deterministic guards on LLM output. The LLM cannot bypass these.

The graph only accepts an objective or workout when these functions return
no violations. Violations are fed back to the model for correction a limited
number of times; after that the deterministic fallback (``fallback.py``) is
used.

Workout checks delegate to the Phase 5 validator
(``app/training/workout_validator.py``), the same gate the publisher runs
before writing to Intervals.icu. The LLM is therefore corrected against
exactly the rules that decide publication.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from app.agents.schemas import (
    SESSION_INTENSITY,
    RecoveryAssessment,
    TrainingObjective,
    WorkoutDraft,
    intensity_from_label,
)
from app.training.intensity import Intensity, cap
from app.training.progression import allowed
from app.training.readiness import HARD_SESSION_IF, HARD_SESSION_TSS
from app.training.sessions import SESSION_DURATION_MINUTES
from app.training.workout_generator import (
    StepInput,
    StructuredWorkout,
    WorkoutObjective,
    build_workout,
)
from app.training.workout_validator import (
    MAX_HARD_SESSIONS_7D,
    DayLoad,
    ValidationContext,
    validate_workout,
)

MIN_SESSION_MINUTES = 20
_NO_DATE = dt.date(2000, 1, 1)  # plan date for checks that do not use history


def effective_cap(deterministic: Intensity, recovery: RecoveryAssessment | None) -> Intensity:
    """The LLM may lower the deterministic cap, never raise it."""
    if recovery is None:
        return deterministic
    return cap(deterministic, intensity_from_label(recovery.recommended_max_intensity))


# --- validation context from the graph context ------------------------------------


def history_from_context(context: dict[str, Any]) -> list[DayLoad]:
    """Load of the 6 days before the plan date.

    A day with activities counts with their TSS. A day without activities
    that is not over yet (today or later) counts with its planned workout, so
    a planned-but-not-yet-ridden hard session still blocks a second one.
    """
    plan_date = dt.date.fromisoformat(context["plan_date"])
    today = dt.date.fromisoformat(context.get("today") or context["plan_date"])
    start = plan_date - dt.timedelta(days=6)
    days: dict[dt.date, DayLoad] = {}
    for a in context.get("recent_activities", []):
        day = dt.date.fromisoformat(a["date"])
        if not start <= day < plan_date:
            continue
        tss = a.get("tss")
        hard = (a.get("intensity_factor") or 0) >= HARD_SESSION_IF or (tss or 0) >= HARD_SESSION_TSS
        prev = days.get(day)
        total = (prev.tss or 0.0) + (tss or 0.0) if prev else tss
        days[day] = DayLoad(day, total, hard or bool(prev and prev.hard), "activity")
    for p in context.get("recent_planned", []):
        day = dt.date.fromisoformat(p["date"])
        if not start <= day < plan_date or day in days or day < today:
            continue
        if p.get("session_type") == "rest":
            continue
        days[day] = DayLoad(day, p.get("estimated_tss"), bool(p.get("hard")), "planned")
    return [days[d] for d in sorted(days)]


def validation_context(
    context: dict[str, Any], max_intensity: Intensity, target_minutes: int | None = None
) -> ValidationContext:
    latest = (context.get("load") or {}).get("latest") or {}
    guidance = context.get("guidance") or {}
    return ValidationContext(
        plan_date=dt.date.fromisoformat(context["plan_date"]),
        max_intensity=max_intensity,
        readiness_status=(context.get("readiness") or {}).get("status"),
        target_minutes=target_minutes,
        ctl=latest.get("ctl"),
        ctl_authoritative=bool(latest.get("ctl_authoritative")),
        history=history_from_context(context),
        max_session_type=guidance.get("max_session_type"),
        plan_max_tss=guidance.get("max_tss"),
        plan_max_minutes=guidance.get("max_minutes"),
    )


# --- objective ----------------------------------------------------------------------


def objective_violations(
    objective: TrainingObjective,
    max_intensity: Intensity,
    ctx: ValidationContext | None = None,
) -> list[str]:
    problems: list[str] = []
    level = SESSION_INTENSITY[objective.session_type]
    if level > max_intensity:
        problems.append(
            f"session_type {objective.session_type!r} ({level.label}) exceeds the maximum "
            f"allowed intensity {max_intensity.label!r}"
        )
    if objective.session_type == "rest":
        if objective.target_duration_minutes != 0:
            problems.append("a rest day must have target_duration_minutes = 0")
        return problems
    if (
        ctx is not None
        and ctx.max_session_type is not None
        and not allowed(objective.session_type, ctx.max_session_type)  # type: ignore[arg-type]
    ):
        problems.append(f"the long-term plan allows at most a {ctx.max_session_type} session today")
    if (
        ctx is not None
        and ctx.plan_max_minutes is not None
        and objective.target_duration_minutes > ctx.plan_max_minutes
    ):
        problems.append(f"today's plan allows at most {ctx.plan_max_minutes} minutes")
    if objective.target_duration_minutes < MIN_SESSION_MINUTES:
        problems.append(f"a training session must last at least {MIN_SESSION_MINUTES} minutes")
    bounds = SESSION_DURATION_MINUTES.get(objective.session_type)
    if bounds and not bounds[0] <= objective.target_duration_minutes <= bounds[1]:
        problems.append(f"a {objective.session_type} session lasts {bounds[0]}-{bounds[1]} minutes")
    if ctx is not None and ctx.history is not None and level >= Intensity.THRESHOLD:
        start = ctx.plan_date - dt.timedelta(days=6)
        hard = {d.date for d in ctx.history if d.hard and start <= d.date < ctx.plan_date}
        if len(hard) + 1 > MAX_HARD_SESSIONS_7D:
            problems.append(
                f"{len(hard)} hard sessions already in the last 6 days; choose an easier session"
            )
        if ctx.plan_date - dt.timedelta(days=1) in hard:
            problems.append("yesterday was hard: no hard session today")
    return problems


# --- workout ------------------------------------------------------------------------


def to_structured(
    draft: WorkoutDraft,
    objective: TrainingObjective,
    *,
    ftp_watts: int | None,
    athlete_slug: str,
    plan_date: dt.date,
) -> StructuredWorkout:
    """LLM/fallback draft → deterministic structured workout (raises on invalid FTP)."""
    return build_workout(
        athlete_slug=athlete_slug,
        workout_date=plan_date,
        name=draft.name,
        description=draft.description,
        ftp_watts=ftp_watts,
        objective=WorkoutObjective(
            session_type=objective.session_type,
            target_duration_minutes=objective.target_duration_minutes,
            primary_goal=objective.primary_goal,
            rationale=objective.rationale,
        ),
        steps=[
            StepInput(
                kind=s.kind,
                duration_s=s.duration_seconds,
                low_pct=s.power_low_pct_ftp,
                high_pct=s.power_high_pct_ftp,
                cadence_rpm=s.cadence_rpm,
                note=s.note,
            )
            for s in draft.steps
        ],
    )


def workout_violations(
    draft: WorkoutDraft,
    objective: TrainingObjective,
    max_intensity: Intensity,
    ftp_watts: int | None,
    ctx: ValidationContext | None = None,
    *,
    athlete_slug: str = "athlete",
) -> list[str]:
    """Full validator errors for a draft. ``ctx`` adds the weekly/load rules."""
    plan_date = ctx.plan_date if ctx else _NO_DATE
    workout = to_structured(
        draft, objective, ftp_watts=ftp_watts, athlete_slug=athlete_slug, plan_date=plan_date
    )
    ctx = ctx or ValidationContext(plan_date=plan_date, max_intensity=max_intensity)
    if ctx.max_intensity != max_intensity:
        ctx = dataclasses.replace(ctx, max_intensity=max_intensity)
    return validate_workout(workout, ctx).errors
