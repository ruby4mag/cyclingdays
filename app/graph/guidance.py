"""Combine readiness, the long-term plan and recent performance into the day's limits.

``context["guidance"]`` is the single source of the deterministic limits the
graph and validator use:

* ``max_intensity``: the strictest of readiness and the plan prescription.
* ``max_session_type``, ``max_tss``, ``max_minutes``: from the plan
  prescription; None without a plan.
* ``target_tss``: the plan's intended load for the day, a guide rather than
  a limit.
* ``caps``: every restriction with its reason (readiness caps plus the plan's).

Pure; the inputs come from ``context.py``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from app.training.adaptation import SessionResult, adapt
from app.training.intensity import Intensity, cap
from app.training.periodization import PlanPhases
from app.training.plan_builder import monday_of
from app.training.progression import prescribe


def _day_tss(
    recent_activities: Sequence[dict[str, Any]],
    recent_planned: Sequence[dict[str, Any]],
    today: dt.date,
) -> dict[dt.date, float]:
    """Load per day: actual rides; a day not over yet without a ride counts its plan."""
    by_day: dict[dt.date, float] = {}
    for a in recent_activities:
        day = dt.date.fromisoformat(a["date"])
        by_day[day] = by_day.get(day, 0.0) + float(a.get("tss") or 0.0)
    for p in recent_planned:
        day = dt.date.fromisoformat(p["date"])
        if day >= today and day not in by_day and p.get("session_type") != "rest":
            by_day[day] = float(p.get("estimated_tss") or 0.0)
    return by_day


def build_guidance(
    *,
    plan_date: dt.date,
    today: dt.date,
    readiness: dict[str, Any],
    plan: PlanPhases | None,
    plan_name: str | None,
    recent_activities: Sequence[dict[str, Any]],
    recent_planned: Sequence[dict[str, Any]],
    results: Sequence[SessionResult],
) -> dict[str, Any]:
    readiness_cap = Intensity[str(readiness["max_intensity"]).upper()]
    caps = list(readiness.get("caps", []))
    tss = _day_tss(recent_activities, recent_planned, today)
    week_start = monday_of(plan_date)
    week_done = sum(v for d, v in tss.items() if week_start <= d < plan_date)

    compliance: float | None = None
    prescription = None
    adaptation = None
    if plan is not None:
        prev_phase = plan.phase_for(week_start - dt.timedelta(days=1))
        if prev_phase is not None and prev_phase.weekly_tss_target:
            prev_start = week_start - dt.timedelta(days=7)
            prev_actual = sum(v for d, v in tss.items() if prev_start <= d < week_start)
            compliance = prev_actual / prev_phase.weekly_tss_target
        adaptation = adapt(plan_date, results, last_week_compliance=compliance)
        prescription = prescribe(plan, plan_date, week_tss_done=week_done, adaptation=adaptation)
    else:
        adaptation = adapt(plan_date, results, last_week_compliance=None)

    max_intensity = readiness_cap
    if prescription is not None:
        max_intensity = cap(readiness_cap, prescription.max_intensity)
        if prescription.max_intensity < readiness_cap:
            caps.append(
                {
                    "max_intensity": prescription.max_intensity.label,
                    "reason": "long-term plan: " + "; ".join(prescription.reasons),
                }
            )
    elif adaptation.day_max_session is not None:
        # Without a plan, only the "recover" recommendation applies directly.
        max_intensity = cap(readiness_cap, Intensity.RECOVERY)
        caps.append({"max_intensity": "recovery", "reason": "; ".join(adaptation.reasons)})

    return {
        "plan": plan_name,
        "max_intensity": max_intensity.label,
        "max_session_type": None if prescription is None else prescription.max_session,
        "target_tss": None if prescription is None else round(prescription.target_tss, 1),
        "max_tss": None if prescription is None else round(prescription.max_tss, 1),
        "max_minutes": None if prescription is None else prescription.max_minutes,
        "prescription": None if prescription is None else prescription.as_dict(),
        "adaptation": adaptation.as_dict(),
        "week_tss_done": round(week_done, 1),
        "last_week_compliance": None if compliance is None else round(compliance, 2),
        "caps": caps,
    }
