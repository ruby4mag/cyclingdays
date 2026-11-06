"""Workout planner: selects the day's objective, then writes the ERG workout.

Both outputs pass deterministic guards (``app/graph/guards.py``, which run the
Phase 5 workout validator) before the graph accepts them; failures are sent
back for correction.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentOutcome, run_agent
from app.agents.llm import StructuredLLM
from app.agents.schemas import TrainingObjective, WorkoutDraft
from app.graph import guards
from app.graph.prompts import OBJECTIVE_SELECTION, WORKOUT_PLANNING
from app.training.intensity import Intensity
from app.training.workout_validator import load_limits


def _recent_days(context: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"date": d.date.isoformat(), "tss": d.tss, "hard": d.hard, "source": d.source}
        for d in guards.history_from_context(context)
    ]


def _guidance(context: dict[str, Any]) -> dict[str, Any]:
    g = context.get("guidance") or {}
    keys = (
        "plan",
        "max_session_type",
        "target_tss",
        "max_tss",
        "max_minutes",
        "week_tss_done",
        "last_week_compliance",
    )
    out = {k: g.get(k) for k in keys}
    presc = g.get("prescription") or {}
    out["day_role"] = presc.get("role")
    out["phase_type"] = presc.get("phase_type")
    out["reasons"] = presc.get("reasons", [])
    out["adaptation"] = (g.get("adaptation") or {}).get("reasons", [])
    return out


async def select_objective(
    llm: StructuredLLM,
    context: dict[str, Any],
    training_analysis: dict[str, Any],
    recovery_assessment: dict[str, Any],
    max_intensity: Intensity,
    *,
    max_corrections: int,
) -> AgentOutcome[TrainingObjective]:
    payload = {
        "plan_date": context["plan_date"],
        "athlete": context["athlete"],
        "effective_max_intensity": max_intensity.label,
        "readiness_status": context["readiness"]["status"],
        "training_analysis": training_analysis,
        "recovery_assessment": recovery_assessment,
        "plan_phase": context["plan_phase"],
        "plan_guidance": _guidance(context),
        "recent_evaluations": context.get("recent_evaluations", []),
        "weekly_load": context["load"]["weeks"],
        "upcoming_calendar": context["calendar"],
        "recent_days": _recent_days(context),
    }
    ctx = guards.validation_context(context, max_intensity)
    return await run_agent(
        llm,
        OBJECTIVE_SELECTION,
        TrainingObjective,
        payload,
        validate=lambda o: guards.objective_violations(o, max_intensity, ctx),
        max_corrections=max_corrections,
    )


async def plan_workout(
    llm: StructuredLLM,
    context: dict[str, Any],
    objective: TrainingObjective,
    max_intensity: Intensity,
    *,
    max_corrections: int,
) -> AgentOutcome[WorkoutDraft]:
    ftp = context["athlete"]["ftp_watts"]
    ctx = guards.validation_context(context, max_intensity, objective.target_duration_minutes)
    session_tss, week_tss = load_limits(ctx)
    prior_week = sum(d.tss or 0.0 for d in ctx.history or [])
    payload = {
        "plan_date": context["plan_date"],
        "ftp_watts": ftp,
        "power_zones": context["zones"],
        "effective_max_intensity": max_intensity.label,
        "objective": objective.model_dump(mode="json"),
        "plan_guidance": _guidance(context),
        "load_limits": {
            "session_tss_max": round(session_tss),
            "rolling_7d_tss_max": round(week_tss),
            "previous_6_days_tss": round(prior_week),
        },
    }
    return await run_agent(
        llm,
        WORKOUT_PLANNING,
        WorkoutDraft,
        payload,
        validate=lambda d: guards.workout_violations(
            d,
            objective,
            max_intensity,
            ftp,
            ctx,
            athlete_slug=context["athlete"]["slug"],
        ),
        max_corrections=max_corrections,
    )
