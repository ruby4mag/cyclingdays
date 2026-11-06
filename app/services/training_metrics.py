"""Compute and persist daily training metrics, then assess readiness.

Reads ``activities``, ``daily_recovery``, ``training_plans`` and the activity
sync coverage, and runs the pure functions in ``app.training``. It writes one
``training_metrics`` row per day (idempotent upsert keyed on
``calculation_version``) and returns the readiness assessment for the
planning day.

The readiness assessment is returned, not stored. Phase 4 stores it as part of
the coaching decision's input snapshot.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Activity, AthleteProfile, DailyRecovery
from app.database.repositories import (
    ActivityRepository,
    AthleteProfileRepository,
    DailyRecoveryRepository,
    SynchronizationStateRepository,
    TrainingMetricRepository,
    TrainingPlanRepository,
    UpsertCounts,
)
from app.integrations.intervals_mappers import SOURCE as INTERVALS_SOURCE
from app.training import CALCULATION_VERSION
from app.training.metrics import (
    ActivityLoad,
    DayMetrics,
    activity_load,
    daily_loads,
    pmc,
    weekly_loads,
)
from app.training.periodization import PlanPhase, PlanPhases
from app.training.readiness import (
    BASELINE_DAYS,
    ReadinessAssessment,
    ReadinessInput,
    RecentSession,
    WellnessDay,
    assess_readiness,
)

logger = logging.getLogger(__name__)

WEEKS_IN_REPORT = 8
RECENT_SESSION_DAYS = 3


class TrainingMetricsError(Exception):
    pass


@dataclass
class TrainingReport:
    athlete_slug: str
    day: str
    calculation_version: str
    coverage_start: str | None
    series_start: str | None
    activities: int
    load_methods: dict[str, int]
    metrics_written: dict[str, int]
    latest: dict[str, Any] | None
    weeks: list[dict[str, Any]]
    readiness: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def load_for(activity: Activity, profile_ftp: int | None) -> ActivityLoad:
    return activity_load(
        duration_s=activity.moving_time_s or activity.elapsed_time_s,
        normalized_watts=activity.normalized_power_watts,
        ftp_at_activity=activity.ftp_at_activity_watts,
        profile_ftp=profile_ftp,
        source_load=activity.source_training_load,
    )


def _wellness_day(rows: list[DailyRecovery]) -> WellnessDay:
    """Merge rows of one day (one per source); first non-null value wins."""

    def first(attr: str) -> Any:
        return next((getattr(r, attr) for r in rows if getattr(r, attr) is not None), None)

    return WellnessDay(
        date=rows[0].recovery_date,
        resting_hr_bpm=first("resting_hr_bpm"),
        hrv_rmssd_ms=first("hrv_rmssd_ms"),
        sleep_s=first("sleep_duration_s"),
        fatigue=first("subjective_fatigue"),
        soreness=first("subjective_soreness"),
        stress=first("subjective_stress"),
        injury=first("subjective_injury"),
        illness_reported=any(r.illness_reported for r in rows),
        pain_reported=any(r.pain_reported for r in rows),
    )


def _metric_row(athlete_id: int, m: DayMetrics, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "athlete_id": athlete_id,
        "metric_date": m.date,
        "calculation_version": CALCULATION_VERSION,
        "daily_tss": _round(m.daily_tss),
        "ctl": _round(m.ctl),
        "atl": _round(m.atl),
        "tsb": _round(m.tsb),
        "rolling_7d_tss": _round(m.rolling_7d_tss),
        "data_completeness": _round(m.data_completeness),
        "details": {
            "atl_completeness": _round(m.atl_completeness),
            "known_history_days": m.known_history_days,
            "ctl_authoritative": m.ctl_authoritative,
            "atl_authoritative": m.atl_authoritative,
            "tsb_authoritative": m.tsb_authoritative,
            "ctl_ramp_7d": _round(m.ctl_ramp_7d),
            "reasons": list(m.reasons),
            **extra,
        },
    }


class TrainingMetricsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._sessions = session_factory
        self._now = now

    async def run(
        self, athlete_slug: str, *, day: dt.date | None = None, store: bool = True
    ) -> TrainingReport:
        async with self._sessions() as session, session.begin():
            athlete = await AthleteProfileRepository(session).get_by_slug(athlete_slug)
            if athlete is None:
                raise TrainingMetricsError(f"athlete profile {athlete_slug!r} not found")
            if day is None:
                day = self._now().astimezone(ZoneInfo(athlete.timezone)).date()
            report = await self._run(session, athlete, day, store)
        logger.info(
            "training_metrics_computed",
            extra={
                "athlete_slug": athlete_slug,
                "day": report.day,
                "metrics_written": report.metrics_written,
                "readiness_status": report.readiness["status"],
                "max_intensity": report.readiness["max_intensity"],
            },
        )
        return report

    async def _run(
        self, session: AsyncSession, athlete: AthleteProfile, day: dt.date, store: bool
    ) -> TrainingReport:
        notes: list[str] = []
        coverage_start = await self._coverage_start(session, athlete.id)
        if coverage_start is None:
            notes.append("no synchronized activity history: load metrics are unknown")

        activities = await ActivityRepository(session).list_until(athlete.id, day)
        loads_by_date: dict[dt.date, list[ActivityLoad]] = {}
        methods: Counter[str] = Counter()
        recent: list[RecentSession] = []
        for activity in activities:
            load = load_for(activity, athlete.ftp_watts)
            methods[load.method] += 1
            loads_by_date.setdefault(activity.start_date_local, []).append(load)
            if day - dt.timedelta(days=RECENT_SESSION_DAYS) <= activity.start_date_local < day:
                recent.append(
                    RecentSession(activity.start_date_local, load.intensity_factor, load.tss)
                )

        starts = [d for d in (coverage_start, min(loads_by_date, default=None)) if d]
        series: list[DayMetrics] = []
        counts = UpsertCounts()
        if starts:
            start = min(min(starts), day)
            days = daily_loads(loads_by_date, start, day, coverage_start)
            series = pmc(days)
            if store:
                repo = TrainingMetricRepository(session)
                for load_day, metrics in zip(days, series, strict=True):
                    day_methods = Counter(
                        load.method for load in loads_by_date.get(load_day.date, [])
                    )
                    extra = {
                        "activity_count": load_day.activity_count,
                        "known_tss": _round(load_day.known_tss),
                        "covered": load_day.covered,
                        "load_methods": dict(day_methods),
                    }
                    counts.add(await repo.upsert(_metric_row(athlete.id, metrics, extra)))
            weeks = weekly_loads(days)[-WEEKS_IN_REPORT:]
        else:
            weeks = []

        previous_day = next((m for m in reversed(series) if m.date == day - dt.timedelta(1)), None)
        wellness = await self._wellness(session, athlete.id, day)
        phase, has_plan = await self._plan_phase(session, athlete.id, day, notes)
        readiness: ReadinessAssessment = assess_readiness(
            ReadinessInput(
                day=day,
                wellness=wellness,
                load=previous_day,
                recent_sessions=recent,
                plan_phase=phase,
                has_active_plan=has_plan,
            )
        )
        if athlete.ftp_watts is None:
            notes.append("athlete FTP unknown: power-based load cannot be computed")

        latest = series[-1] if series else None
        return TrainingReport(
            athlete_slug=athlete.slug,
            day=day.isoformat(),
            calculation_version=CALCULATION_VERSION,
            coverage_start=coverage_start.isoformat() if coverage_start else None,
            series_start=series[0].date.isoformat() if series else None,
            activities=len(activities),
            load_methods=dict(methods),
            metrics_written=asdict(counts),
            latest=None
            if latest is None
            else {
                "date": latest.date.isoformat(),
                "ctl": _round(latest.ctl, 1),
                "atl": _round(latest.atl, 1),
                "tsb": _round(latest.tsb, 1),
                "rolling_7d_tss": _round(latest.rolling_7d_tss, 1),
                "data_completeness": _round(latest.data_completeness, 3),
                "ctl_authoritative": latest.ctl_authoritative,
                "atl_authoritative": latest.atl_authoritative,
                "reasons": list(latest.reasons),
            },
            weeks=[
                {
                    "week_start": w.week_start.isoformat(),
                    "tss": _round(w.tss, 1),
                    "activities": w.activity_count,
                    "known_days": w.known_days,
                    "complete": w.complete,
                }
                for w in weeks
            ],
            readiness=readiness.as_dict(),
            notes=notes,
        )

    async def _coverage_start(self, session: AsyncSession, athlete_id: int) -> dt.date | None:
        state = await SynchronizationStateRepository(session).get(
            athlete_id, INTERVALS_SOURCE, "activities"
        )
        if state is None or state.last_success_at is None:
            return None
        value = (state.cursor or {}).get("coverage_start") or (state.cursor or {}).get("oldest")
        return dt.date.fromisoformat(value) if value else None

    async def _wellness(
        self, session: AsyncSession, athlete_id: int, day: dt.date
    ) -> list[WellnessDay]:
        rows = await DailyRecoveryRepository(session).list_between(
            athlete_id, day - dt.timedelta(days=BASELINE_DAYS + 7), day
        )
        by_date: dict[dt.date, list[DailyRecovery]] = {}
        for row in rows:
            by_date.setdefault(row.recovery_date, []).append(row)
        return [_wellness_day(items) for _, items in sorted(by_date.items())]

    async def _plan_phase(
        self, session: AsyncSession, athlete_id: int, day: dt.date, notes: list[str]
    ) -> tuple[PlanPhase | None, bool]:
        plan = await TrainingPlanRepository(session).get_active(athlete_id)
        if plan is None:
            return None, False
        try:
            return PlanPhases.from_json(plan.phases).phase_for(day), True
        except ValidationError as exc:
            notes.append(
                f"active plan {plan.name!r} has invalid phases: {exc.error_count()} error(s)"
            )
            logger.warning("training_plan_invalid", extra={"plan_id": plan.id})
            return None, True
