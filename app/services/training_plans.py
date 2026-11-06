"""Create an athlete's long-term plan from their goal (Phase 7, web in Phase 10).

Shared by ``scripts/create_plan.py`` and the account page
(``PUT /api/account/goal``). The plan itself is deterministic
(``app/training/plan_builder.py``). The goal (objective, event date,
weekly hours) is stored with the plan in ``plan_metadata`` and the weekly
hours in the athlete's ``availability``, where the coach reads them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AthleteProfile
from app.database.repositories import TrainingPlanRepository
from app.training.periodization import WEEKDAYS, DayRole
from app.training.plan_builder import (
    DEFAULT_TEMPLATE,
    PLAN_BUILDER_VERSION,
    PlanBuildError,
    build_plan,
)

MAX_OBJECTIVE_LEN = 200
DAY_ROLES: tuple[DayRole, ...] = ("rest", "recovery", "endurance", "key", "long")


class PlanExistsError(ValueError):
    pass


@dataclass(frozen=True)
class Goal:
    weekly_hours: tuple[float, float]
    objective: str | None = None
    event_date: dt.date | None = None
    start: dt.date | None = None
    # Weekday -> role; None: DEFAULT_TEMPLATE (rest Mon, key Tue/Thu, long Sat).
    week_template: dict[str, DayRole] | None = None

    def validate(self, today: dt.date) -> None:
        lo, hi = self.weekly_hours
        if not (1 <= lo <= hi <= 30):
            raise PlanBuildError("Weekly hours: between 1 and 30, minimum not above maximum")
        if self.objective is not None and len(self.objective) > MAX_OBJECTIVE_LEN:
            raise PlanBuildError(f"Objective: at most {MAX_OBJECTIVE_LEN} characters")
        if self.event_date is not None and self.event_date <= today:
            raise PlanBuildError("The event date must be in the future")
        if self.start is not None and not (
            today - dt.timedelta(days=7) <= self.start <= today + dt.timedelta(days=60)
        ):
            raise PlanBuildError(
                "The plan start must be within a week before or 60 days after today"
            )
        if self.week_template is not None:
            check_week_template(self.week_template)


def check_week_template(template: Mapping[str, str]) -> None:
    """Coaching limits on a custom week: recovery has to fit in somewhere."""
    if set(template) != set(WEEKDAYS) or any(r not in DAY_ROLES for r in template.values()):
        raise PlanBuildError("Week: give every day (Mon-Sun) one of " + ", ".join(DAY_ROLES))
    roles = list(template.values())
    if roles.count("rest") < 1:
        raise PlanBuildError("Week: at least one rest day")
    if roles.count("key") > 2:
        raise PlanBuildError("Week: at most two key (hard) days")
    if roles.count("long") > 2:
        raise PlanBuildError("Week: at most two long-ride days")
    if roles.count("key") + roles.count("long") > 3:
        raise PlanBuildError("Week: at most three key and long days together")
    days = [template[d] for d in WEEKDAYS]
    for i, role in enumerate(days):
        if role == "key" and days[(i + 1) % 7] == "key":
            raise PlanBuildError("Week: no two key days in a row")


def goal_of(plan_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """The goal stored with a plan (plans made before Phase 10 have none)."""
    goal = (plan_metadata or {}).get("goal")
    return goal if isinstance(goal, dict) else None


async def create_plan(
    session: AsyncSession,
    athlete: AthleteProfile,
    goal: Goal,
    *,
    today: dt.date,
    replace: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Build (and unless ``dry_run``, activate) the plan. Raises ``PlanBuildError``."""
    goal.validate(today)
    plans = TrainingPlanRepository(session)
    existing = await plans.get_active(athlete.id)
    if existing is not None and not replace and not dry_run:
        raise PlanExistsError(f"active plan {existing.name!r} exists; replace it explicitly")
    phases = build_plan(
        start=goal.start or today,
        weekly_hours=goal.weekly_hours,
        event_date=goal.event_date,
        template=goal.week_template or DEFAULT_TEMPLATE,
    )
    lo, hi = goal.weekly_hours
    objective = (goal.objective or "").strip() or athlete.primary_objective or None
    name = (
        f"{objective or 'Event'} on {goal.event_date.isoformat()}"
        if goal.event_date
        else f"Open-ended endurance plan ({lo:g}-{hi:g} h/week)"
    )
    first = min(p.start_date for p in phases.phases)
    last = max(p.end_date for p in phases.phases)
    out: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "name": name,
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "week_template": phases.week_template,
        "weeks": [
            {
                "start": p.start_date.isoformat(),
                "name": p.name,
                "type": p.type,
                "key_session": p.key_session,
                "tss": p.weekly_tss_target,
                "minutes": p.weekly_minutes_target,
            }
            for p in phases.phases
        ],
    }
    if dry_run:
        return out
    row = await plans.activate(
        athlete.id,
        name=name,
        objective=objective or "endurance",
        start_date=first,
        end_date=last,
        phases=phases.model_dump(mode="json"),
        plan_metadata={
            "builder": PLAN_BUILDER_VERSION,
            "goal": {
                "objective": objective,
                "event_date": goal.event_date.isoformat() if goal.event_date else None,
                "weekly_hours": [lo, hi],
            },
        },
    )
    if objective:
        athlete.primary_objective = objective
    athlete.availability = {**(athlete.availability or {}), "weekly_hours": [lo, hi]}
    out["plan_id"] = row.id
    out["replaced"] = None if existing is None else existing.name
    return out
