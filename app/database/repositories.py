"""Data-access repositories.

Repositories are added in the phase that first needs them. Upserts use
PostgreSQL ``INSERT … ON CONFLICT`` on the natural keys declared in
``models.py``, so re-running a synchronization never creates duplicates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Table, and_, func, literal_column, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Executable

from app.database.models import (
    INACTIVE_WORKOUT_STATUSES,
    Activity,
    AthleteProfile,
    Base,
    CalendarEvent,
    DailyRecovery,
    PlannedWorkout,
    PublishingAudit,
    SynchronizationState,
    TrainingMetric,
    TrainingPlan,
    WebhookEvent,
    WorkoutPerformance,
)


class AthleteProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_slug(self, slug: str) -> AthleteProfile | None:
        result = await self._session.execute(
            select(AthleteProfile).where(AthleteProfile.slug == slug)
        )
        return result.scalar_one_or_none()

    async def create(self, **values: Any) -> AthleteProfile:
        profile = AthleteProfile(**values)
        self._session.add(profile)
        await self._session.flush()
        return profile


@dataclass
class UpsertCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def add(self, outcome: str | None) -> None:
        if outcome == "inserted":
            self.inserted += 1
        elif outcome == "updated":
            self.updated += 1
        else:
            self.unchanged += 1


async def _upsert(
    session: AsyncSession,
    model: type[Base],
    values: dict[str, Any],
    conflict_columns: Sequence[str],
) -> str | None:
    """Insert or update one row; returns 'inserted', 'updated' or None (unchanged).

    The UPDATE only fires when ``raw_payload`` differs, so unchanged rows keep
    their ``updated_at`` and are reported as unchanged.
    """
    table = cast(Table, model.__table__)
    stmt = insert(table).values(**values)
    update_cols = {
        name: stmt.excluded[name]
        for name in values
        if name not in conflict_columns and name != "athlete_id"
    }
    upsert: Executable = stmt.on_conflict_do_update(
        index_elements=list(conflict_columns),
        set_=update_cols,
        where=table.c.raw_payload.is_distinct_from(stmt.excluded.raw_payload)
        | (table.c.deleted_at.is_not(None) if "deleted_at" in table.c else literal_column("false")),
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    row = (await session.execute(upsert)).first()
    if row is None:
        return None
    return "inserted" if row.inserted else "updated"


class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, athlete_id: int, values: dict[str, Any]) -> str | None:
        return await _upsert(
            self._session,
            Activity,
            {**values, "athlete_id": athlete_id},
            ("source", "source_activity_id"),
        )

    async def list_until(
        self, athlete_id: int, newest: dt.date, *, include_duplicates: bool = False
    ) -> list[Activity]:
        """Activities up to ``newest``. Duplicate recordings are excluded by default."""
        stmt = select(Activity).where(
            Activity.athlete_id == athlete_id, Activity.start_date_local <= newest
        )
        if not include_duplicates:
            stmt = stmt.where(Activity.duplicate_of_activity_id.is_(None))
        result = await self._session.execute(stmt.order_by(Activity.start_time, Activity.id))
        return list(result.scalars())

    async def list_between(
        self, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> list[Activity]:
        """All activities (duplicates included) with a local date in [oldest, newest]."""
        result = await self._session.execute(
            select(Activity)
            .where(
                Activity.athlete_id == athlete_id,
                Activity.start_date_local.between(oldest, newest),
            )
            .order_by(Activity.start_time, Activity.id)
        )
        return list(result.scalars())

    async def apply_duplicates(
        self, activities: Sequence[Activity], duplicate_of: dict[int, int]
    ) -> int:
        """Set or clear ``duplicate_of_activity_id``; returns the number of rows changed."""
        changed = 0
        for activity in activities:
            target = duplicate_of.get(activity.id)
            if activity.duplicate_of_activity_id != target:
                await self._session.execute(
                    update(Activity)
                    .where(Activity.id == activity.id)
                    .values(duplicate_of_activity_id=target)
                )
                activity.duplicate_of_activity_id = target
                changed += 1
        return changed

    async def count(self, athlete_id: int) -> int:
        return int(
            await self._session.scalar(
                select(func.count()).select_from(Activity).where(Activity.athlete_id == athlete_id)
            )
            or 0
        )


class DailyRecoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_between(
        self, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> list[DailyRecovery]:
        result = await self._session.execute(
            select(DailyRecovery)
            .where(
                DailyRecovery.athlete_id == athlete_id,
                DailyRecovery.recovery_date.between(oldest, newest),
            )
            .order_by(DailyRecovery.recovery_date, DailyRecovery.source)
        )
        return list(result.scalars())

    async def upsert(self, athlete_id: int, values: dict[str, Any]) -> str | None:
        return await _upsert(
            self._session,
            DailyRecovery,
            {**values, "athlete_id": athlete_id},
            ("athlete_id", "recovery_date", "source"),
        )


class CalendarEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, athlete_id: int, values: dict[str, Any]) -> str | None:
        return await _upsert(
            self._session,
            CalendarEvent,
            {**values, "athlete_id": athlete_id},
            ("source", "source_event_id"),
        )

    async def list_live_between(
        self, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> list[CalendarEvent]:
        result = await self._session.execute(
            select(CalendarEvent)
            .where(
                CalendarEvent.athlete_id == athlete_id,
                CalendarEvent.deleted_at.is_(None),
                CalendarEvent.start_date_local.between(oldest, newest),
            )
            .order_by(CalendarEvent.start_date_local, CalendarEvent.id)
        )
        return list(result.scalars())

    async def mark_missing_deleted(
        self,
        athlete_id: int,
        source: str,
        oldest: dt.date,
        newest: dt.date,
        seen_ids: Iterable[str],
        now: dt.datetime,
    ) -> int:
        """Soft-delete live events in [oldest, newest] that the source no longer returns."""
        stmt = (
            update(CalendarEvent)
            .where(
                and_(
                    CalendarEvent.athlete_id == athlete_id,
                    CalendarEvent.source == source,
                    CalendarEvent.start_date_local.between(oldest, newest),
                    CalendarEvent.deleted_at.is_(None),
                    CalendarEvent.source_event_id.not_in(list(seen_ids)),
                )
            )
            .values(deleted_at=now)
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


class PlannedWorkoutRepository:
    """One active planned workout per athlete and day (enforced by a partial unique index)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _active(self, athlete_id: int) -> Any:
        return select(PlannedWorkout).where(
            PlannedWorkout.athlete_id == athlete_id,
            PlannedWorkout.status.not_in(INACTIVE_WORKOUT_STATUSES),
        )

    async def get_for_day(
        self, athlete_id: int, day: dt.date, *, for_update: bool = False
    ) -> PlannedWorkout | None:
        stmt = self._active(athlete_id).where(PlannedWorkout.workout_date == day)
        if for_update:
            stmt = stmt.with_for_update()
        return cast(PlannedWorkout | None, await self._session.scalar(stmt))

    async def get_by_external_id(self, external_id: str) -> PlannedWorkout | None:
        return cast(
            PlannedWorkout | None,
            await self._session.scalar(
                select(PlannedWorkout).where(PlannedWorkout.external_id == external_id)
            ),
        )

    async def get_by_event_id(self, athlete_id: int, event_id: str) -> PlannedWorkout | None:
        return cast(
            PlannedWorkout | None,
            await self._session.scalar(
                self._active(athlete_id).where(PlannedWorkout.intervals_event_id == event_id)
            ),
        )

    async def list_due_before(self, athlete_id: int, day: dt.date) -> Sequence[PlannedWorkout]:
        """Training days before ``day`` that are still open (validated or published)."""
        result = await self._session.execute(
            self._active(athlete_id)
            .where(
                PlannedWorkout.workout_date < day,
                PlannedWorkout.status.in_(("validated", "published")),
                PlannedWorkout.workout_type != "rest",
            )
            .order_by(PlannedWorkout.workout_date)
        )
        return result.scalars().all()

    async def list_between(
        self, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> Sequence[PlannedWorkout]:
        result = await self._session.execute(
            self._active(athlete_id)
            .where(PlannedWorkout.workout_date.between(oldest, newest))
            .order_by(PlannedWorkout.workout_date)
        )
        return result.scalars().all()


class PublishingAuditRepository:
    """Append-only (UPDATE/DELETE are blocked by a trigger)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, **values: Any) -> PublishingAudit:
        row = PublishingAudit(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_for_workout(self, planned_workout_id: int) -> Sequence[PublishingAudit]:
        result = await self._session.execute(
            select(PublishingAudit)
            .where(PublishingAudit.planned_workout_id == planned_workout_id)
            .order_by(PublishingAudit.id)
        )
        return result.scalars().all()


class TrainingPlanRepository:
    """One ``active`` plan per athlete (partial unique index)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def activate(self, athlete_id: int, **values: Any) -> TrainingPlan:
        """Archive the current active plan (if any) and insert a new active one."""
        await self._session.execute(
            update(TrainingPlan)
            .where(TrainingPlan.athlete_id == athlete_id, TrainingPlan.status == "active")
            .values(status="archived")
        )
        await self._session.flush()
        plan = TrainingPlan(athlete_id=athlete_id, status="active", **values)
        self._session.add(plan)
        await self._session.flush()
        return plan

    async def get_active(self, athlete_id: int) -> TrainingPlan | None:
        result = await self._session.execute(
            select(TrainingPlan).where(
                TrainingPlan.athlete_id == athlete_id, TrainingPlan.status == "active"
            )
        )
        return result.scalar_one_or_none()


_METRIC_VALUE_COLUMNS = (
    "daily_tss",
    "ctl",
    "atl",
    "tsb",
    "rolling_7d_tss",
    "data_completeness",
    "details",
)


class TrainingMetricRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, values: dict[str, Any]) -> str | None:
        """Insert or update one day; unchanged values are left untouched."""
        table = cast(Table, TrainingMetric.__table__)
        stmt = insert(table).values(**values)
        upsert: Executable = stmt.on_conflict_do_update(
            index_elements=["athlete_id", "metric_date", "calculation_version"],
            set_={name: stmt.excluded[name] for name in _METRIC_VALUE_COLUMNS},
            where=tuple_(*(table.c[n] for n in _METRIC_VALUE_COLUMNS)).is_distinct_from(
                tuple_(*(stmt.excluded[n] for n in _METRIC_VALUE_COLUMNS))
            ),
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        row = (await self._session.execute(upsert)).first()
        if row is None:
            return None
        return "inserted" if row.inserted else "updated"

    async def list_between(
        self, athlete_id: int, version: str, oldest: dt.date, newest: dt.date
    ) -> list[TrainingMetric]:
        result = await self._session.execute(
            select(TrainingMetric)
            .where(
                TrainingMetric.athlete_id == athlete_id,
                TrainingMetric.calculation_version == version,
                TrainingMetric.metric_date.between(oldest, newest),
            )
            .order_by(TrainingMetric.metric_date)
        )
        return list(result.scalars())


class SynchronizationStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(
        self, athlete_id: int, source: str, resource: str
    ) -> SynchronizationState:
        """Return the state row (created if missing), locked for this transaction."""
        await self._session.execute(
            insert(SynchronizationState)
            .values(athlete_id=athlete_id, source=source, resource=resource)
            .on_conflict_do_nothing(index_elements=["athlete_id", "source", "resource"])
        )
        result = await self._session.execute(
            select(SynchronizationState)
            .where(
                SynchronizationState.athlete_id == athlete_id,
                SynchronizationState.source == source,
                SynchronizationState.resource == resource,
            )
            .with_for_update()
        )
        return result.scalar_one()

    async def get(self, athlete_id: int, source: str, resource: str) -> SynchronizationState | None:
        result = await self._session.execute(
            select(SynchronizationState).where(
                SynchronizationState.athlete_id == athlete_id,
                SynchronizationState.source == source,
                SynchronizationState.resource == resource,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_athlete(self, athlete_id: int) -> list[SynchronizationState]:
        result = await self._session.execute(
            select(SynchronizationState)
            .where(SynchronizationState.athlete_id == athlete_id)
            .order_by(SynchronizationState.source, SynchronizationState.resource)
        )
        return list(result.scalars())


async def try_advisory_xact_lock(session: AsyncSession, namespace: str, key: int) -> bool:
    """Non-blocking transaction-scoped advisory lock (released at commit/rollback)."""
    return bool(
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:ns), CAST(:key AS integer))"),
            {"ns": namespace, "key": key},
        )
    )


class WorkoutPerformanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_activity(self, activity_id: int) -> WorkoutPerformance | None:
        return cast(
            WorkoutPerformance | None,
            await self._session.scalar(
                select(WorkoutPerformance).where(WorkoutPerformance.activity_id == activity_id)
            ),
        )

    async def revisions(self, activity_ids: Iterable[int]) -> dict[int, str | None]:
        ids = list(activity_ids)
        if not ids:
            return {}
        result = await self._session.execute(
            select(WorkoutPerformance.activity_id, WorkoutPerformance.activity_revision).where(
                WorkoutPerformance.activity_id.in_(ids)
            )
        )
        return {row[0]: row[1] for row in result}

    async def save(self, values: dict[str, Any]) -> str:
        """Insert or update the evaluation of one activity (unique ``activity_id``)."""
        stmt = insert(WorkoutPerformance).values(**values)
        upsert: Executable = stmt.on_conflict_do_update(
            index_elements=["activity_id"],
            set_={k: stmt.excluded[k] for k in values if k not in ("activity_id", "athlete_id")},
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        row = (await self._session.execute(upsert)).one()
        return "inserted" if row.inserted else "updated"

    async def for_planned(self, planned_workout_id: int) -> WorkoutPerformance | None:
        return cast(
            WorkoutPerformance | None,
            await self._session.scalar(
                select(WorkoutPerformance)
                .where(WorkoutPerformance.planned_workout_id == planned_workout_id)
                .order_by(WorkoutPerformance.id)
                .limit(1)
            ),
        )

    async def list_between(
        self, athlete_id: int, oldest: dt.date, newest: dt.date
    ) -> list[WorkoutPerformance]:
        result = await self._session.execute(
            select(WorkoutPerformance)
            .where(
                WorkoutPerformance.athlete_id == athlete_id,
                WorkoutPerformance.session_date.between(oldest, newest),
            )
            .order_by(WorkoutPerformance.session_date, WorkoutPerformance.id)
        )
        return list(result.scalars())


class EventInboxRepository:
    """``webhook_events`` used as an inbound event inbox, deduplicated by (source, dedup_key)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self, source: str, dedup_key: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        """True if the event is new; a duplicate delivery is ignored."""
        stmt = (
            insert(WebhookEvent)
            .values(source=source, dedup_key=dedup_key, event_type=event_type, payload=payload)
            .on_conflict_do_nothing(index_elements=["source", "dedup_key"])
            .returning(WebhookEvent.id)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def claim(
        self,
        source: str,
        event_type: str,
        athlete_id: int,
        max_attempts: int,
        exclude: Iterable[int] = (),
    ) -> WebhookEvent | None:
        """Lock the oldest pending event (SKIP LOCKED) and mark it processing.

        ``exclude``: events already tried in this run (a failure is retried
        on the next run, not immediately).
        """
        event = await self._session.scalar(
            select(WebhookEvent)
            .where(
                WebhookEvent.source == source,
                WebhookEvent.event_type == event_type,
                WebhookEvent.status.in_(("received", "failed")),
                WebhookEvent.attempts < max_attempts,
                WebhookEvent.payload["athlete_id"].as_integer() == athlete_id,
                WebhookEvent.id.not_in(list(exclude) or [-1]),
            )
            .order_by(WebhookEvent.received_at, WebhookEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is not None:
            event.status = "processing"
            event.attempts += 1
        return event

    async def finish(self, event_id: int, status: str, error: str | None = None) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(status=status, last_error=error, processed_at=func.now())
        )

    async def reset_stale(self, source: str, event_type: str, athlete_id: int) -> int:
        """Events left 'processing' by a crashed run become retryable."""
        result = await self._session.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.source == source,
                WebhookEvent.event_type == event_type,
                WebhookEvent.status == "processing",
                WebhookEvent.payload["athlete_id"].as_integer() == athlete_id,
            )
            .values(status="failed", last_error="interrupted; will retry")
        )
        return int(getattr(result, "rowcount", 0) or 0)
