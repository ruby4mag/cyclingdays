"""Read-only dashboard queries (Phase 8).

Every number shown on the dashboard comes from here. This module only reads.
It never calls Intervals.icu or the LLM and never writes, so opening the
dashboard can't change the coaching state.

The aggregations are module-level pure functions (unit tested without a
database). ``DashboardService`` loads the rows and feeds them through.

Honesty rules (same as the coach):

* Values that the training engine marks as not authoritative keep that flag,
  so the page can show them as provisional instead of as facts.
* Missing data stays ``None``; it's never shown as zero.
* Each power-zone total says where its numbers come from.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    Activity,
    AthleteProfile,
    CalendarEvent,
    CoachDecision,
    PlannedWorkout,
    SyncCheck,
    TrainingMetric,
)
from app.database.repositories import (
    ActivityRepository,
    AthleteProfileRepository,
    PlannedWorkoutRepository,
    SynchronizationStateRepository,
    TrainingMetricRepository,
    TrainingPlanRepository,
    WorkoutPerformanceRepository,
)
from app.integrations.intervals_mappers import cycling_ftp
from app.services.training_metrics import load_for
from app.training import CALCULATION_VERSION
from app.training.periodization import WEEKDAYS, PlanPhases
from app.training.zones import ZONE_DEFINITIONS, InvalidFTPError, power_zones

DASHBOARD_VERSION = "dashboard/v1"

MAX_FITNESS_DAYS = 365
MAX_WEEKS = 52
MAX_ZONE_DAYS = 365
MAX_WORKOUT_DAYS = 365
MAX_CALENDAR_DAYS = 62
CALENDAR_WEEKS_BACK = 2
CALENDAR_DEFAULT_DAYS = 35
MAX_ASSESSMENTS = 50

ASSESSMENT_TYPES = ("daily_plan", "performance_review")
_ICU_ZONE_ID = re.compile(r"^Z([1-7])$")


class AthleteNotFoundError(LookupError):
    pass


class InvalidRangeError(ValueError):
    pass


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _r(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def weekly_volume(
    *,
    mondays: Sequence[dt.date],
    today: dt.date,
    daily_tss: Mapping[dt.date, float | None],
    rides: Iterable[dict[str, Any]],
    plan: PlanPhases | None,
) -> list[dict[str, Any]]:
    """Per-week load and volume, next to the plan's targets.

    ``daily_tss`` comes from ``training_metrics`` (the deterministic load the
    coach uses). ``rides`` are activity dicts with ``date``, ``moving_s`` and
    ``distance_m`` (duplicates already removed). ``tss_complete`` is False when
    a day of a past week has no metric row.
    """
    by_week: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for ride in rides:
        by_week[week_start(ride["date"])].append(ride)
    weeks = []
    for monday in mondays:
        days = [monday + dt.timedelta(days=i) for i in range(7)]
        elapsed = [d for d in days if d <= today]
        values = [daily_tss.get(d) for d in elapsed]
        known = [v for v in values if v is not None]
        week_rides = by_week.get(monday, [])
        moving = sum(r["moving_s"] or 0 for r in week_rides)
        distance = sum(r["distance_m"] or 0 for r in week_rides)
        phase = None if plan is None else plan.phase_for(monday)
        target = None if phase is None else phase.weekly_tss_target
        tss = round(sum(known), 1) if known else None
        finished = days[-1] < today
        weeks.append(
            {
                "week_start": monday.isoformat(),
                "finished": finished,
                "tss": tss,
                "tss_complete": bool(elapsed) and len(known) == len(elapsed),
                "hours": round(moving / 3600, 2),
                "rides": len(week_rides),
                "distance_km": round(distance / 1000, 1),
                "plan_phase": None if phase is None else phase.name,
                "plan_type": None if phase is None else phase.type,
                "target_tss": target,
                "target_hours": (
                    None
                    if phase is None or phase.weekly_minutes_target is None
                    else round(phase.weekly_minutes_target / 60, 2)
                ),
                "compliance_pct": (
                    round(tss / target * 100, 1)
                    if finished and target and tss is not None
                    else None
                ),
            }
        )
    return weeks


def icu_zone_seconds(raw: Any) -> dict[int, int] | None:
    """Intervals.icu ``icu_zone_times`` ([{"id": "Z1", "secs": 120}, …]) by zone number.

    Ids other than Z1-Z7 (e.g. a sweet-spot band) are ignored. None when the
    field is absent or unusable.
    """
    if not isinstance(raw, list):
        return None
    out: dict[int, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        match = _ICU_ZONE_ID.match(str(item.get("id", "")))
        secs = item.get("secs")
        if match and isinstance(secs, int | float) and secs >= 0:
            out[int(match.group(1))] = out.get(int(match.group(1)), 0) + int(secs)
    return out or None


def aggregate_zones(rides: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Total time per power zone.

    Each ride dict has ``coach_zones`` (``evaluation["zones"]`` or None) and
    ``icu_zone_times`` (the raw Intervals.icu field or None). The coach's own
    zones from the power stream are preferred. Intervals.icu's are used only
    for rides without them, and the totals say how many rides came from each
    source, because Intervals.icu uses the athlete's zone settings there.
    """
    totals = {number: 0 for number, *_ in ZONE_DEFINITIONS}
    by_source = {"coach": dict(totals), "intervals_icu": dict(totals)}
    counts = {"coach": 0, "intervals_icu": 0, "none": 0}
    for ride in rides:
        coach = ride.get("coach_zones")
        seconds: dict[int, int] | None = None
        source = "none"
        if isinstance(coach, dict) and isinstance(coach.get("seconds_by_zone"), dict):
            seconds = {int(k): int(v) for k, v in coach["seconds_by_zone"].items()}
            source = "coach"
        else:
            seconds = icu_zone_seconds(ride.get("icu_zone_times"))
            if seconds is not None:
                source = "intervals_icu"
        counts[source] += 1
        if seconds is None:
            continue
        for zone, secs in seconds.items():
            if zone in totals:
                totals[zone] += secs
                by_source[source][zone] += secs
    total = sum(totals.values())
    return {
        "zones": [
            {
                "zone": number,
                "name": name,
                "lower_pct": round(lower * 100),
                "upper_pct": None if upper is None else round(upper * 100),
                "seconds": totals[number],
                "coach_seconds": by_source["coach"][number],
                "intervals_icu_seconds": by_source["intervals_icu"][number],
                "fraction": round(totals[number] / total, 4) if total else None,
            }
            for number, name, lower, upper in ZONE_DEFINITIONS
        ],
        "total_seconds": total,
        "rides": sum(counts.values()),
        "rides_coach": counts["coach"],
        "rides_intervals_icu": counts["intervals_icu"],
        "rides_without_zones": counts["none"],
    }


def plan_progress(
    plan: PlanPhases,
    *,
    today: dt.date,
    daily_tss: Mapping[dt.date, float | None],
    minutes_by_day: Mapping[dt.date, float],
) -> dict[str, Any]:
    """Each plan week with its target and what was done (to date)."""
    weeks = []
    current_index = None
    done_target = 0.0
    done_tss = 0.0
    for index, phase in enumerate(sorted(plan.phases, key=lambda p: p.start_date)):
        if phase.end_date < today:
            state = "past"
        elif phase.start_date > today:
            state = "future"
        else:
            state = "current"
            current_index = index
        tss = None
        minutes = None
        if state != "future":
            days = [
                phase.start_date + dt.timedelta(days=i)
                for i in range((min(phase.end_date, today) - phase.start_date).days + 1)
            ]
            known = [v for d in days if (v := daily_tss.get(d)) is not None]
            tss = round(sum(known), 1) if known else None
            minutes = round(sum(minutes_by_day.get(d, 0.0) for d in days))
        target = phase.weekly_tss_target
        if state == "past" and target and tss is not None:
            done_target += target
            done_tss += tss
        weeks.append(
            {
                "index": index,
                "name": phase.name,
                "type": phase.type,
                "start_date": phase.start_date.isoformat(),
                "end_date": phase.end_date.isoformat(),
                "key_session": phase.key_session,
                "target_tss": target,
                "target_minutes": phase.weekly_minutes_target,
                "tss": tss,
                "minutes": minutes,
                "state": state,
                "compliance_pct": (
                    round(tss / target * 100, 1)
                    if state == "past" and target and tss is not None
                    else None
                ),
            }
        )
    return {
        "weeks": weeks,
        "current_index": current_index,
        "weeks_total": len(weeks),
        "weeks_done": sum(1 for w in weeks if w["state"] == "past"),
        "completed_weeks_compliance_pct": (
            round(done_tss / done_target * 100, 1) if done_target else None
        ),
    }


def calendar_days(
    *,
    start: dt.date,
    end: dt.date,
    today: dt.date,
    plan: PlanPhases | None,
    planned: Sequence[dict[str, Any]],
    rides: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One entry per day: plan role, the coach's workout, rides and other events.

    ``state`` summarizes the day: ``completed`` (a ride was done), ``missed``
    (a past coach workout without a ride), ``planned`` (an upcoming coach
    workout), ``rest``, or None (nothing planned and nothing done).
    """
    planned_by_day = {p["date"]: p for p in planned}
    rides_by_day: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for ride in rides:
        rides_by_day[ride["date"]].append(ride)
    events_by_day: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_day[event["date"]].append(event)

    out = []
    day = start
    while day <= end:
        phase = None if plan is None else plan.phase_for(day)
        role = (
            None
            if plan is None or plan.week_template is None or phase is None
            else plan.week_template[WEEKDAYS[day.weekday()]]
        )
        workout = planned_by_day.get(day)
        day_rides = rides_by_day.get(day, [])
        if day_rides:
            state: str | None = "completed"
        elif workout is not None and workout["type"] == "rest":
            state = "rest"
        elif workout is not None and (workout["status"] == "missed" or day < today):
            state = "missed"
        elif workout is not None:
            state = "planned"
        elif role == "rest":
            state = "rest"
        else:
            state = None
        out.append(
            {
                "date": day.isoformat(),
                "weekday": WEEKDAYS[day.weekday()],
                "is_today": day == today,
                "plan_phase": None if phase is None else phase.name,
                "plan_role": role,
                "workout": None if workout is None else _json_dates(workout),
                "rides": [_json_dates(r) for r in day_rides],
                "events": [_json_dates(e) for e in events_by_day.get(day, [])],
                "state": state,
            }
        )
        day += dt.timedelta(days=1)
    return out


def _json_dates(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, dt.date) else v for k, v in item.items()}


def workout_steps(structure: dict[str, Any]) -> list[dict[str, Any]]:
    """The published-workout steps, reduced to what the page draws."""
    steps = (structure.get("workout") or {}).get("steps") or []
    keys = (
        "index",
        "kind",
        "section",
        "duration_s",
        "low_pct",
        "high_pct",
        "low_watts",
        "high_watts",
        "zone",
        "cadence_rpm",
        "note",
    )
    return [{k: s.get(k) for k in keys} for s in steps if isinstance(s, dict)]


def daily_plan_assessment(decision: CoachDecision) -> dict[str, Any]:
    out = decision.output or {}
    objective = out.get("objective") or {}
    recovery = out.get("recovery_assessment") or {}
    analysis = out.get("training_analysis") or {}
    return {
        "id": decision.id,
        "type": "daily_plan",
        "date": decision.decision_date.isoformat(),
        "created_at": decision.created_at.isoformat(),
        "outcome": out.get("outcome"),
        "session_type": objective.get("session_type"),
        "goal": objective.get("primary_goal"),
        "rationale": objective.get("rationale") or decision.rationale,
        "long_term_link": objective.get("long_term_link"),
        "recovery_state": recovery.get("recovery_state"),
        "recovery_summary": recovery.get("summary"),
        "training_summary": analysis.get("summary"),
        "concerns": analysis.get("concerns") or [],
        "deterministic_cap": out.get("deterministic_cap"),
        "effective_cap": out.get("effective_cap"),
        "fallback_reason": out.get("fallback_reason"),
        "abort_reason": out.get("abort_reason"),
        "llm_calls": out.get("llm_calls"),
        "model": decision.model_name,
        "prompt_versions": out.get("prompt_versions") or {},
    }


def performance_review_assessment(decision: CoachDecision) -> dict[str, Any]:
    out = decision.output or {}
    review = out.get("review") or {}
    activity = ((decision.input_snapshot or {}).get("payload") or {}).get("evaluation", {})
    return {
        "id": decision.id,
        "type": "performance_review",
        "date": decision.decision_date.isoformat(),
        "created_at": decision.created_at.isoformat(),
        "accepted": out.get("accepted"),
        "recommendation": review.get("recommendation"),
        "summary": review.get("summary") or decision.rationale,
        "went_well": review.get("went_well") or [],
        "concerns": review.get("concerns") or [],
        "next_session_note": review.get("next_session_note"),
        "outcome": activity.get("outcome"),
        "activity_name": (activity.get("activity") or {}).get("name"),
        "model": decision.model_name,
        "prompt_version": decision.prompt_version,
    }


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


class DashboardService:
    """Loads and shapes everything the dashboard shows, for one athlete."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    # -- common ---------------------------------------------------------------

    async def _athlete(self, session: AsyncSession, slug: str) -> AthleteProfile:
        athlete = await AthleteProfileRepository(session).get_by_slug(slug)
        if athlete is None:
            raise AthleteNotFoundError(f"athlete {slug!r} not found; seed the profile first")
        return athlete

    def _today(self, athlete: AthleteProfile) -> dt.date:
        return self._now().astimezone(ZoneInfo(athlete.timezone)).date()

    @staticmethod
    def _check(value: int, maximum: int, name: str) -> None:
        if not 1 <= value <= maximum:
            raise InvalidRangeError(f"{name} must be between 1 and {maximum}")

    async def _daily_tss(
        self, session: AsyncSession, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> dict[dt.date, float | None]:
        rows = await TrainingMetricRepository(session).list_between(
            athlete_id, CALCULATION_VERSION, oldest, newest
        )
        return {r.metric_date: r.daily_tss for r in rows}

    async def _rides(
        self, session: AsyncSession, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> list[Activity]:
        rows = await ActivityRepository(session).list_between(athlete_id, oldest, newest)
        return [a for a in rows if a.duplicate_of_activity_id is None]

    async def _plan(self, session: AsyncSession, athlete_id: int) -> tuple[Any, PlanPhases | None]:
        row = await TrainingPlanRepository(session).get_active(athlete_id)
        return row, None if row is None else PlanPhases.from_json(row.phases)

    # -- sections -------------------------------------------------------------

    async def summary(self, slug: str) -> dict[str, Any]:
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            metrics = await TrainingMetricRepository(session).list_between(
                athlete.id, CALCULATION_VERSION, today - dt.timedelta(days=7), today
            )
            latest = metrics[-1] if metrics else None
            upcoming = await PlannedWorkoutRepository(session).list_between(
                athlete.id, today, today + dt.timedelta(days=14)
            )
            next_workout = next((w for w in upcoming if w.workout_type != "rest"), None)
            decision = await session.scalar(
                select(CoachDecision)
                .where(
                    CoachDecision.athlete_id == athlete.id,
                    CoachDecision.decision_type == "daily_plan",
                )
                .order_by(CoachDecision.decision_date.desc(), CoachDecision.id.desc())
                .limit(1)
            )
            sync_states = await SynchronizationStateRepository(session).list_for_athlete(athlete.id)
            checks = await self._latest_sync_checks(session, athlete.id)
            plan_row, plan = await self._plan(session, athlete.id)

        intervals_ftp = cycling_ftp(athlete.intervals_snapshot or {})
        try:
            zones = [
                {
                    "zone": z.number,
                    "name": z.name,
                    "lower_watts": round(z.lower_watts),
                    "upper_watts": None if z.upper_watts is None else round(z.upper_watts),
                }
                for z in power_zones(athlete.ftp_watts)
            ]
        except InvalidFTPError:
            zones = []
        phase = None if plan is None else plan.phase_for(today)
        return {
            "version": DASHBOARD_VERSION,
            "today": today.isoformat(),
            "athlete": {
                "slug": athlete.slug,
                "name": athlete.display_name,
                "age_years": athlete.age_years,
                "weight_kg": athlete.weight_kg,
                "height_cm": athlete.height_cm,
                "experience_level": athlete.experience_level,
                "primary_objective": athlete.primary_objective,
                "secondary_objective": athlete.secondary_objective,
                "timezone": athlete.timezone,
                "indoor_platform": athlete.indoor_platform,
                "wearable": athlete.wearable,
                "intervals_athlete_id": athlete.intervals_athlete_id,
            },
            "ftp": {
                "watts": athlete.ftp_watts,
                "updated_at": (
                    None if athlete.ftp_updated_at is None else athlete.ftp_updated_at.isoformat()
                ),
                "w_per_kg": (
                    round(athlete.ftp_watts / athlete.weight_kg, 2)
                    if athlete.ftp_watts and athlete.weight_kg
                    else None
                ),
                "intervals_icu_watts": intervals_ftp,
                "matches_intervals_icu": (
                    None if intervals_ftp is None else intervals_ftp == athlete.ftp_watts
                ),
                "zones": zones,
            },
            "fitness": None if latest is None else _metric_point(latest),
            "readiness": _readiness(decision),
            "next_workout": None if next_workout is None else _workout_card(next_workout),
            "plan": (
                None
                if plan_row is None
                else {
                    "name": plan_row.name,
                    "objective": plan_row.objective,
                    "phase": None if phase is None else phase.name,
                    "phase_type": None if phase is None else phase.type,
                    "key_session": None if phase is None else phase.key_session,
                    "week_target_tss": None if phase is None else phase.weekly_tss_target,
                    "role_today": (
                        None
                        if plan is None or plan.week_template is None or phase is None
                        else plan.week_template[WEEKDAYS[today.weekday()]]
                    ),
                }
            ),
            "sync": [
                {
                    "resource": s.resource,
                    "last_success_at": (
                        None if s.last_success_at is None else s.last_success_at.isoformat()
                    ),
                    "last_error": s.last_error,
                    "consecutive_failures": s.consecutive_failures,
                }
                for s in sync_states
            ],
            "sync_checks": checks,
        }

    async def _latest_sync_checks(
        self, session: AsyncSession, athlete_id: int
    ) -> list[dict[str, Any]]:
        rows = (
            await session.scalars(
                select(SyncCheck)
                .where(SyncCheck.athlete_id == athlete_id)
                .order_by(SyncCheck.check_name, SyncCheck.created_at.desc(), SyncCheck.id.desc())
            )
        ).all()
        latest: dict[str, SyncCheck] = {}
        for row in rows:  # newest first per check; the table holds only changes
            latest.setdefault(row.check_name, row)
        return [
            {
                "check": r.check_name,
                "status": r.status,
                "summary": r.summary,
                "since": r.created_at.isoformat(),
            }
            for r in latest.values()
        ]

    async def fitness(self, slug: str, days: int) -> dict[str, Any]:
        self._check(days, MAX_FITNESS_DAYS, "days")
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            rows = await TrainingMetricRepository(session).list_between(
                athlete.id, CALCULATION_VERSION, today - dt.timedelta(days=days - 1), today
            )
        points = [_metric_point(r) for r in rows]
        return {
            "today": today.isoformat(),
            "days": days,
            "calculation_version": CALCULATION_VERSION,
            "points": points,
            "latest": points[-1] if points else None,
        }

    async def weekly(self, slug: str, weeks: int) -> dict[str, Any]:
        self._check(weeks, MAX_WEEKS, "weeks")
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            first = week_start(today) - dt.timedelta(weeks=weeks - 1)
            daily = await self._daily_tss(session, athlete.id, first, today)
            rides = await self._rides(session, athlete.id, first, today)
            _, plan = await self._plan(session, athlete.id)
        mondays = [first + dt.timedelta(weeks=i) for i in range(weeks)]
        return {
            "today": today.isoformat(),
            "weeks": weekly_volume(
                mondays=mondays,
                today=today,
                daily_tss=daily,
                rides=[
                    {
                        "date": a.start_date_local,
                        "moving_s": a.moving_time_s,
                        "distance_m": a.distance_m,
                    }
                    for a in rides
                ],
                plan=plan,
            ),
        }

    async def zones(self, slug: str, days: int) -> dict[str, Any]:
        self._check(days, MAX_ZONE_DAYS, "days")
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            oldest = today - dt.timedelta(days=days - 1)
            rides = await self._rides(session, athlete.id, oldest, today)
            perf = {
                p.activity_id: p
                for p in await WorkoutPerformanceRepository(session).list_between(
                    athlete.id, oldest, today
                )
            }
        result = aggregate_zones(
            {
                "coach_zones": (
                    (perf[a.id].evaluation or {}).get("zones") if a.id in perf else None
                ),
                "icu_zone_times": (a.raw_payload or {}).get("icu_zone_times"),
            }
            for a in rides
        )
        return {"today": today.isoformat(), "days": days, **result}

    async def workouts(self, slug: str, days: int) -> dict[str, Any]:
        self._check(days, MAX_WORKOUT_DAYS, "days")
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            oldest = today - dt.timedelta(days=days - 1)
            all_rides = await ActivityRepository(session).list_between(athlete.id, oldest, today)
            perf = {
                p.activity_id: p
                for p in await WorkoutPerformanceRepository(session).list_between(
                    athlete.id, oldest, today
                )
            }
            planned_ids = {p.planned_workout_id for p in perf.values() if p.planned_workout_id}
            planned = (
                {
                    w.id: w
                    for w in (
                        await session.scalars(
                            select(PlannedWorkout).where(PlannedWorkout.id.in_(planned_ids))
                        )
                    ).all()
                }
                if planned_ids
                else {}
            )
        items = []
        for a in sorted(all_rides, key=lambda x: x.start_time, reverse=True):
            if a.duplicate_of_activity_id is not None:
                continue
            load = load_for(a, athlete.ftp_watts)
            p = perf.get(a.id)
            evaluation = {} if p is None else (p.evaluation or {})
            review = evaluation.get("review") or {}
            workout = (
                None
                if p is None or p.planned_workout_id is None
                else planned.get(p.planned_workout_id)
            )
            items.append(
                {
                    "id": a.id,
                    "date": a.start_date_local.isoformat(),
                    "start_time": a.start_time.isoformat(),
                    "name": a.name,
                    "type": a.activity_type,
                    "indoor": a.is_indoor,
                    "origin": a.origin,
                    "device": a.device_name,
                    "minutes": None if a.moving_time_s is None else round(a.moving_time_s / 60),
                    "distance_km": None if a.distance_m is None else round(a.distance_m / 1000, 1),
                    "avg_power_watts": _r(a.avg_power_watts, 0),
                    "normalized_power_watts": _r(a.normalized_power_watts, 0),
                    "avg_hr_bpm": _r(a.avg_hr_bpm, 0),
                    "tss": _r(load.tss),
                    "tss_method": load.method,
                    "intensity_factor": _r(load.intensity_factor, 2),
                    "rpe": a.rpe,
                    "feel": a.feel,
                    "planned_name": None if workout is None else workout.name,
                    "planned_type": None if workout is None else workout.workout_type,
                    "evaluated": p is not None,
                    "outcome": None if p is None else p.outcome,
                    "completion_pct": None if p is None else _r(p.completion_pct),
                    "power_deviation_pct": None if p is None else _r(p.power_deviation_pct),
                    "decoupling_pct": None if p is None else _r(p.decoupling_pct),
                    "method": (evaluation.get("power") or {}).get("method"),
                    "recommendation": review.get("recommendation"),
                    "review_summary": review.get("summary"),
                    "notes": evaluation.get("notes") or [],
                }
            )
        duplicates = sum(1 for a in all_rides if a.duplicate_of_activity_id is not None)
        return {
            "today": today.isoformat(),
            "days": days,
            "workouts": items,
            "duplicates_hidden": duplicates,
        }

    async def calendar(
        self, slug: str, start: dt.date | None = None, end: dt.date | None = None
    ) -> dict[str, Any]:
        """Days ``start``..``end``; default: 2 weeks back to 3 weeks ahead (Mon-Sun)."""
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            start = start or week_start(today) - dt.timedelta(weeks=CALENDAR_WEEKS_BACK)
            end = end or start + dt.timedelta(days=CALENDAR_DEFAULT_DAYS - 1)
            if end < start:
                raise InvalidRangeError("end must not be before start")
            if (end - start).days + 1 > MAX_CALENDAR_DAYS:
                raise InvalidRangeError(f"the range may span at most {MAX_CALENDAR_DAYS} days")
            planned_rows = await PlannedWorkoutRepository(session).list_between(
                athlete.id, start, end
            )
            rides = await self._rides(session, athlete.id, start, end)
            perf = {
                p.activity_id: p
                for p in await WorkoutPerformanceRepository(session).list_between(
                    athlete.id, start, end
                )
            }
            events = (
                await session.scalars(
                    select(CalendarEvent)
                    .where(
                        CalendarEvent.athlete_id == athlete.id,
                        CalendarEvent.deleted_at.is_(None),
                        CalendarEvent.start_date_local.between(start, end),
                    )
                    .order_by(CalendarEvent.start_date_local, CalendarEvent.id)
                )
            ).all()
            _, plan = await self._plan(session, athlete.id)
        coach_external = {w.external_id for w in planned_rows}
        coach_events = {w.intervals_event_id for w in planned_rows if w.intervals_event_id}
        days = calendar_days(
            start=start,
            end=end,
            today=today,
            plan=plan,
            planned=[
                {
                    "date": w.workout_date,
                    "id": w.id,
                    "name": w.name,
                    "type": w.workout_type,
                    "status": w.status,
                    "minutes": (
                        None if w.planned_duration_s is None else round(w.planned_duration_s / 60)
                    ),
                    "tss": _r(w.planned_training_load),
                    "published": w.published_at is not None,
                }
                for w in planned_rows
            ],
            rides=[
                {
                    "date": a.start_date_local,
                    "id": a.id,
                    "name": a.name,
                    "type": a.activity_type,
                    "minutes": None if a.moving_time_s is None else round(a.moving_time_s / 60),
                    "tss": _r(load_for(a, athlete.ftp_watts).tss),
                    "outcome": perf[a.id].outcome if a.id in perf else None,
                }
                for a in rides
            ],
            events=[
                {
                    "date": e.start_date_local,
                    "name": e.name,
                    "category": e.category,
                    "type": e.event_type,
                }
                for e in events
                if e.external_id not in coach_external and e.source_event_id not in coach_events
            ],
        )
        return {
            "today": today.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": days,
        }

    async def assessments(self, slug: str, limit: int) -> dict[str, Any]:
        self._check(limit, MAX_ASSESSMENTS, "limit")
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            rows = (
                await session.scalars(
                    select(CoachDecision)
                    .where(
                        CoachDecision.athlete_id == athlete.id,
                        CoachDecision.decision_type.in_(ASSESSMENT_TYPES),
                    )
                    .order_by(CoachDecision.decision_date.desc(), CoachDecision.id.desc())
                    .limit(limit)
                )
            ).all()
        return {
            "assessments": [
                daily_plan_assessment(r)
                if r.decision_type == "daily_plan"
                else performance_review_assessment(r)
                for r in rows
            ]
        }

    async def plan(self, slug: str) -> dict[str, Any]:
        async with self._sessions() as session:
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            row, plan = await self._plan(session, athlete.id)
            if row is None or plan is None or not plan.phases:
                return {"today": today.isoformat(), "plan": None}
            first = min(p.start_date for p in plan.phases)
            daily = await self._daily_tss(session, athlete.id, first, today)
            rides = await self._rides(session, athlete.id, first, today)
            ctl_rows = await TrainingMetricRepository(session).list_between(
                athlete.id, CALCULATION_VERSION, first, today
            )
        minutes: dict[dt.date, float] = defaultdict(float)
        for a in rides:
            minutes[a.start_date_local] += (a.moving_time_s or 0) / 60
        progress = plan_progress(plan, today=today, daily_tss=daily, minutes_by_day=minutes)
        builder = plan.builder or {}
        return {
            "today": today.isoformat(),
            "plan": {
                "id": row.id,
                "name": row.name,
                "objective": row.objective,
                "objective_detail": athlete.primary_objective,
                "start_date": row.start_date.isoformat(),
                "end_date": None if row.end_date is None else row.end_date.isoformat(),
                "event_date": builder.get("event_date"),
                "open_ended": builder.get("event_date") is None,
                "weekly_hours": builder.get("weekly_hours"),
                "builder_version": builder.get("version"),
                "week_template": plan.week_template,
                **progress,
                "ctl_at_start": None if not ctl_rows else _r(ctl_rows[0].ctl),
                "ctl_now": None if not ctl_rows else _r(ctl_rows[-1].ctl),
                "ctl_authoritative": (
                    None
                    if not ctl_rows
                    else bool((ctl_rows[-1].details or {}).get("ctl_authoritative"))
                ),
            },
        }


def _metric_point(row: TrainingMetric) -> dict[str, Any]:
    details = row.details or {}
    return {
        "date": row.metric_date.isoformat(),
        "ctl": _r(row.ctl),
        "atl": _r(row.atl),
        "tsb": _r(row.tsb),
        "daily_tss": _r(row.daily_tss),
        "rolling_7d_tss": _r(row.rolling_7d_tss),
        "completeness": _r(row.data_completeness, 2),
        "ctl_authoritative": bool(details.get("ctl_authoritative")),
        "atl_authoritative": bool(details.get("atl_authoritative")),
        "tsb_authoritative": bool(details.get("tsb_authoritative")),
        "reasons": details.get("reasons") or [],
    }


def _readiness(decision: CoachDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    readiness = (decision.input_snapshot or {}).get("readiness") or {}
    out = decision.output or {}
    return {
        "plan_date": decision.decision_date.isoformat(),
        "assessed_at": decision.created_at.isoformat(),
        "status": readiness.get("status"),
        "confidence": readiness.get("confidence"),
        "max_intensity": readiness.get("max_intensity"),
        "effective_cap": out.get("effective_cap"),
        "caps": readiness.get("caps") or [],
        "factors": [
            {k: f.get(k) for k in ("name", "status", "value", "detail", "z_score")}
            for f in readiness.get("factors") or []
            if isinstance(f, dict)
        ],
    }


def _workout_card(w: PlannedWorkout) -> dict[str, Any]:
    workout = (w.structure or {}).get("workout") or {}
    estimate = workout.get("estimate") or {}
    objective = workout.get("objective") or {}
    return {
        "id": w.id,
        "date": w.workout_date.isoformat(),
        "name": w.name,
        "type": w.workout_type,
        "status": w.status,
        "objective": w.objective or objective.get("primary_goal"),
        "rationale": objective.get("rationale"),
        "description": workout.get("description"),
        "minutes": None if w.planned_duration_s is None else round(w.planned_duration_s / 60),
        "tss": _r(w.planned_training_load or estimate.get("tss")),
        "intensity_factor": estimate.get("intensity_factor"),
        "normalized_power_watts": estimate.get("normalized_power_watts"),
        "ftp_watts": w.ftp_reference_watts,
        "published": w.published_at is not None,
        "published_at": None if w.published_at is None else w.published_at.isoformat(),
        "intervals_event_id": w.intervals_event_id,
        "steps": workout_steps(w.structure or {}),
    }
