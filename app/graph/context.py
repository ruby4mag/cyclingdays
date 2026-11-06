"""Deterministic context for the coaching graph.

Everything the LLM sees comes from here: the athlete profile, the Phase 3
training report and readiness, recent activities, the upcoming calendar and
the power-zone table. The whole dict is stored as the daily decision's
``input_snapshot``, so every decision can be reproduced and audited.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import PlannedWorkout
from app.database.repositories import (
    ActivityRepository,
    AthleteProfileRepository,
    CalendarEventRepository,
    PlannedWorkoutRepository,
    TrainingPlanRepository,
    WorkoutPerformanceRepository,
)
from app.graph.guidance import build_guidance
from app.services.training_metrics import TrainingMetricsService
from app.training.adaptation import SessionResult
from app.training.metrics import activity_load
from app.training.periodization import PlanPhases
from app.training.zones import power_zones

RECENT_ACTIVITY_DAYS = 14
RECENT_EVALUATION_DAYS = 14
RECENT_PLANNED_DAYS = 6
CALENDAR_DAYS = 7


class ContextError(Exception):
    """Planning impossible (e.g. unknown athlete). Reported, never guessed around."""


async def build_context(
    sessions: async_sessionmaker[AsyncSession],
    athlete_slug: str,
    plan_date: dt.date,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    report = await TrainingMetricsService(sessions).run(athlete_slug, day=plan_date)
    async with sessions() as session:
        athlete = await AthleteProfileRepository(session).get_by_slug(athlete_slug)
        if athlete is None:  # pragma: no cover - the metrics service raised already
            raise ContextError(f"athlete profile {athlete_slug!r} not found")
        activities = await ActivityRepository(session).list_until(
            athlete.id, plan_date - dt.timedelta(days=1)
        )
        events = await CalendarEventRepository(session).list_live_between(
            athlete.id, plan_date, plan_date + dt.timedelta(days=CALENDAR_DAYS - 1)
        )
        planned = await PlannedWorkoutRepository(session).list_between(
            athlete.id,
            plan_date - dt.timedelta(days=RECENT_PLANNED_DAYS),
            plan_date - dt.timedelta(days=1),
        )
        plan_row = await TrainingPlanRepository(session).get_active(athlete.id)
        performances = await WorkoutPerformanceRepository(session).list_between(
            athlete.id, plan_date - dt.timedelta(days=RECENT_EVALUATION_DAYS), plan_date
        )
        session_types: dict[int, str] = {}
        for perf in performances:
            if perf.planned_workout_id is not None:
                pw = await session.get(PlannedWorkout, perf.planned_workout_id)
                if pw is not None:
                    session_types[perf.planned_workout_id] = pw.workout_type
    today = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(athlete.timezone)).date()

    since = plan_date - dt.timedelta(days=RECENT_ACTIVITY_DAYS)
    recent = []
    for a in activities:
        if a.start_date_local < since:
            continue
        load = activity_load(
            duration_s=a.moving_time_s or a.elapsed_time_s,
            normalized_watts=a.normalized_power_watts,
            ftp_at_activity=a.ftp_at_activity_watts,
            profile_ftp=athlete.ftp_watts,
            source_load=a.source_training_load,
        )
        recent.append(
            {
                "date": a.start_date_local.isoformat(),
                "type": a.activity_type,
                "name": a.name,
                "indoor": a.is_indoor,
                "moving_minutes": None if a.moving_time_s is None else round(a.moving_time_s / 60),
                "normalized_power_watts": a.normalized_power_watts,
                "tss": None if load.tss is None else round(load.tss, 1),
                "intensity_factor": None
                if load.intensity_factor is None
                else round(load.intensity_factor, 3),
                "load_method": load.method,
                "avg_hr_bpm": a.avg_hr_bpm,
                "rpe": a.rpe,
                "feel": a.feel,
            }
        )

    zones = []
    if athlete.ftp_watts is not None:
        zones = [
            {
                "zone": z.number,
                "name": z.name,
                "from_pct_ftp": round(z.lower_fraction * 100),
                "to_pct_ftp": None if z.upper_fraction is None else round(z.upper_fraction * 100),
                "from_watts": round(z.lower_watts, 1),
                "to_watts": None if z.upper_watts is None else round(z.upper_watts, 1),
            }
            for z in power_zones(athlete.ftp_watts)
        ]

    plan: PlanPhases | None = None
    plan_notes: list[str] = []
    if plan_row is not None:
        try:
            plan = PlanPhases.from_json(plan_row.phases)
        except ValidationError as exc:
            plan_notes.append(f"active plan is invalid ({exc.error_count()} errors); ignored")
    else:
        plan_notes.append("no active long-term plan: no per-day prescription")
    results: list[SessionResult] = []
    evaluations: list[dict[str, Any]] = []
    for perf in performances:
        if perf.session_date is None or perf.session_date >= plan_date:
            continue
        stype = session_types.get(perf.planned_workout_id or -1)
        review = (perf.evaluation or {}).get("review") or {}
        results.append(
            SessionResult(
                date=perf.session_date,
                session_type=stype,
                completion_pct=perf.completion_pct,
                power_deviation_pct=perf.power_deviation_pct,
                rpe=perf.rpe,
                decoupling_pct=perf.decoupling_pct,
                recommendation=review.get("recommendation"),
            )
        )
        subjective = (perf.evaluation or {}).get("subjective") or {}
        evaluations.append(
            {
                "date": perf.session_date.isoformat(),
                "planned_session_type": stype,
                "outcome": perf.outcome,
                "completion_pct": perf.completion_pct,
                "power_deviation_pct": perf.power_deviation_pct,
                "decoupling_pct": perf.decoupling_pct,
                "rpe": perf.rpe,
                "expected_rpe": subjective.get("expected_rpe"),
                "review_recommendation": review.get("recommendation"),
                "review_summary": review.get("summary"),
            }
        )

    # JSON round-trip: the context goes into JSONB columns and LangGraph
    # checkpoints, which must hold plain JSON (dates become ISO strings).
    readiness = json.loads(json.dumps(report.readiness, default=str))
    phase_notes = [n for n in readiness.get("notes", []) if "plan" in n]
    recent_planned = [
        {
            "date": p.workout_date.isoformat(),
            "session_type": p.workout_type,
            "status": p.status,
            "estimated_tss": p.planned_training_load,
            "hard": bool((p.validation_result.get("metrics") or {}).get("hard")),
        }
        for p in planned
    ]
    guidance = build_guidance(
        plan_date=plan_date,
        today=today,
        readiness=readiness,
        plan=plan,
        plan_name=None if plan_row is None else plan_row.name,
        recent_activities=recent,
        recent_planned=recent_planned,
        results=results,
    )
    return {
        "plan_date": plan_date.isoformat(),
        "today": today.isoformat(),
        "athlete": {
            "slug": athlete.slug,
            "ftp_watts": athlete.ftp_watts,
            "weight_kg": athlete.weight_kg,
            "age_years": athlete.age_years,
            "experience_level": athlete.experience_level,
            "primary_objective": athlete.primary_objective,
            "secondary_objective": athlete.secondary_objective,
            "indoor_platform": athlete.indoor_platform,
            "availability": athlete.availability,
            "preferences": athlete.preferences,
        },
        "readiness": readiness,
        "load": {
            "calculation_version": report.calculation_version,
            "latest": report.latest,
            "weeks": report.weeks,
            "coverage_start": report.coverage_start,
            "activities_total": report.activities,
            "load_methods": report.load_methods,
        },
        "recent_activities": recent,
        "calendar": [
            {
                "date": e.start_date_local.isoformat(),
                "category": e.category,
                "type": e.event_type,
                "name": e.name,
                "planned_minutes": None
                if e.planned_duration_s is None
                else round(e.planned_duration_s / 60),
                "external_id": e.external_id,
            }
            for e in events
        ],
        "recent_planned": recent_planned,
        "recent_evaluations": evaluations,
        "plan_phase": phase_notes[0] if phase_notes else None,
        "guidance": json.loads(json.dumps(guidance, default=str)),
        "zones": zones,
        "data_notes": [*report.notes, *plan_notes],
    }
