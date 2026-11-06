"""Incremental synchronization from Intervals.icu into PostgreSQL.

Resources, in run order:

``athlete``     ``GET /athlete/{id}`` → links ``athlete_profile.intervals_athlete_id``,
                stores a redacted snapshot, reports FTP/weight differences.
                Our profile is never overwritten automatically.
``activities``  ``GET /athlete/{id}/activities`` → ``coach.activities``.
``wellness``    ``GET /athlete/{id}/wellness``  → ``coach.daily_recovery``.
``calendar``    ``GET /athlete/{id}/events``    → ``coach.calendar_events``
                (planned workouts are the ``WORKOUT`` category).

Incremental strategy. The list endpoints only filter by *local date*; there
is no "changed since" parameter. So each resource keeps a cursor (the last
successfully synced window) in ``coach.synchronization_state`` and the next run
re-fetches from ``cursor.newest - overlap_days``. The overlap picks up late
uploads (wearable sync delays) and edits (RPE, feel, wellness entered later).
The first run, or ``full=True``, fetches ``history_days`` back.

Idempotency. Rows are upserted on their natural keys (``ON CONFLICT``) and
only rewritten when the source payload changed. Each resource runs in one
transaction holding a per-athlete advisory lock, so concurrent runs cannot
interleave; a run that cannot get the lock skips the resource.

Failures are recorded on the state row (``last_error``,
``consecutive_failures``) and logged; other resources still run.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import AthleteProfile
from app.database.repositories import (
    ActivityRepository,
    AthleteProfileRepository,
    CalendarEventRepository,
    DailyRecoveryRepository,
    SynchronizationStateRepository,
    UpsertCounts,
    try_advisory_xact_lock,
)
from app.integrations import intervals_mappers as mappers
from app.integrations.intervals_client import IntervalsClient
from app.training.activity_dedup import Recording, find_duplicates

logger = logging.getLogger(__name__)

Resource = Literal["athlete", "activities", "wellness", "calendar"]
ALL_RESOURCES: tuple[Resource, ...] = ("athlete", "activities", "wellness", "calendar")
LOCK_NAMESPACE = "intervals_sync"
_MAX_ERROR_LEN = 2000


class SyncError(Exception):
    """Unrecoverable sync problem (e.g. athlete id mismatch)."""


@dataclass
class ResourceResult:
    resource: str
    status: Literal["ok", "failed", "locked"] = "ok"
    oldest: str | None = None
    newest: str | None = None
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    deleted: int = 0
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    # Local day stored as the incremental cursor (calendar fetches beyond it).
    cursor_newest: str | None = None

    def apply(self, counts: UpsertCounts) -> None:
        self.inserted, self.updated, self.unchanged = (
            counts.inserted,
            counts.updated,
            counts.unchanged,
        )


@dataclass
class SyncReport:
    athlete_slug: str
    started_at: str
    full: bool
    results: list[ResourceResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status != "failed" for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


class IntervalsSyncService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: IntervalsClient,
        settings: Settings,
        *,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._sessions = session_factory
        self._client = client
        self._settings = settings
        self._now = now

    async def run(
        self,
        athlete_slug: str,
        *,
        resources: Sequence[Resource] = ALL_RESOURCES,
        full: bool = False,
    ) -> SyncReport:
        report = SyncReport(athlete_slug, self._now().isoformat(), full)
        for resource in ALL_RESOURCES:  # fixed order: athlete link first
            if resource not in resources:
                continue
            result = await self._run_resource(athlete_slug, resource, full)
            report.results.append(result)
            if resource == "athlete" and result.status == "failed":
                # Without a verified athlete link the other resources could
                # attach data to the wrong profile.
                break
        logger.info("intervals_sync_finished", extra={"report": report.as_dict()})
        return report

    # ------------------------------------------------------------------

    async def _run_resource(self, slug: str, resource: Resource, full: bool) -> ResourceResult:
        result = ResourceResult(resource)
        started = self._now()
        try:
            async with self._sessions() as session, session.begin():
                athlete = await AthleteProfileRepository(session).get_by_slug(slug)
                if athlete is None:
                    raise SyncError(
                        f"athlete profile {slug!r} not found; run scripts.seed_athlete_profile"
                    )
                if not await try_advisory_xact_lock(session, LOCK_NAMESPACE, athlete.id):
                    result.status = "locked"
                    result.notes.append("another sync for this athlete is running; skipped")
                    logger.warning("intervals_sync_locked", extra={"resource": resource})
                    return result
                state = await SynchronizationStateRepository(session).get_for_update(
                    athlete.id, mappers.SOURCE, resource
                )
                state.last_attempt_at = started
                today = started.astimezone(ZoneInfo(athlete.timezone)).date()
                cursor = dict(state.cursor or {})

                if resource == "athlete":
                    await self._sync_athlete(session, athlete, result)
                elif resource == "activities":
                    await self._sync_activities(session, athlete, cursor, today, full, result)
                elif resource == "wellness":
                    await self._sync_wellness(session, athlete, cursor, today, full, result)
                else:
                    await self._sync_calendar(session, athlete, cursor, today, full, result)

                # (Cursors written before Phase 3 lack coverage_start; their
                # previous "oldest" is a safe, never-overstated substitute.)
                previous = cursor.get("coverage_start") or cursor.get("oldest")
                coverage = [c for c in (previous, result.oldest) if c]
                state.cursor = {
                    **cursor,
                    # Windows always overlap the previous one, so history is
                    # contiguous from the earliest window start ever synced.
                    "coverage_start": min(coverage) if coverage else None,
                    "oldest": result.oldest,
                    "newest": result.cursor_newest or result.newest,
                    "last_full_sync": started.isoformat()
                    if full or not cursor
                    else cursor.get("last_full_sync"),
                }
                state.last_success_at = started
                state.last_error = None
                state.consecutive_failures = 0
        except Exception as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
            logger.exception(
                "intervals_sync_resource_failed",
                extra={"resource": resource, "athlete_slug": slug},
            )
            await self._record_failure(slug, resource, started, result.error)
            return result

        logger.info("intervals_sync_resource_ok", extra={"result": asdict(result)})
        return result

    async def _record_failure(
        self, slug: str, resource: str, started: dt.datetime, error: str
    ) -> None:
        try:
            async with self._sessions() as session, session.begin():
                athlete = await AthleteProfileRepository(session).get_by_slug(slug)
                if athlete is None:
                    return
                state = await SynchronizationStateRepository(session).get_for_update(
                    athlete.id, mappers.SOURCE, resource
                )
                state.last_attempt_at = started
                state.last_error = error
                state.consecutive_failures += 1
        except Exception:
            logger.exception("intervals_sync_state_update_failed", extra={"resource": resource})

    def _window_start(self, cursor: dict[str, Any], today: dt.date, full: bool) -> dt.date:
        history_start = today - dt.timedelta(days=self._settings.intervals_history_days)
        previous = cursor.get("newest")
        if full or not previous:
            return history_start
        start = dt.date.fromisoformat(previous) - dt.timedelta(
            days=self._settings.intervals_sync_overlap_days
        )
        return min(start, today)

    # --- resources -----------------------------------------------------

    async def _sync_athlete(
        self, session: AsyncSession, athlete: AthleteProfile, result: ResourceResult
    ) -> None:
        payload = await self._client.get_athlete()
        result.fetched = 1
        summary = mappers.athlete_summary(payload)
        remote_id = summary["intervals_athlete_id"]
        if remote_id is None:
            raise SyncError("Intervals.icu athlete response has no id")
        if athlete.intervals_athlete_id and athlete.intervals_athlete_id != remote_id:
            raise SyncError(
                f"profile is linked to Intervals.icu athlete {athlete.intervals_athlete_id}, "
                f"but the API key returned {remote_id}; refusing to mix data"
            )
        if athlete.intervals_athlete_id is None:
            athlete.intervals_athlete_id = remote_id
            result.notes.append(f"linked profile to Intervals.icu athlete {remote_id}")
        snapshot = mappers.athlete_snapshot(payload)
        if snapshot != athlete.intervals_snapshot:
            athlete.intervals_snapshot = snapshot
            result.updated = 1
        else:
            result.unchanged = 1
        athlete.intervals_synced_at = self._now()

        # Differences are reported, not applied: the coach's profile is authoritative
        # and FTP changes must be deliberate (they change every power target).
        remote_ftp = summary["ride_ftp_watts"]
        if remote_ftp is not None and remote_ftp != athlete.ftp_watts:
            result.notes.append(
                f"FTP differs: profile {athlete.ftp_watts} W, Intervals.icu Ride {remote_ftp} W"
            )
        remote_weight = summary["weight_kg"]
        if (
            remote_weight is not None
            and athlete.weight_kg is not None
            and abs(remote_weight - athlete.weight_kg) >= 0.5
        ):
            result.notes.append(
                f"weight differs: profile {athlete.weight_kg} kg, Intervals.icu {remote_weight} kg"
            )
        if summary["timezone"] and summary["timezone"] != athlete.timezone:
            result.notes.append(
                f"timezone differs: profile {athlete.timezone}, Intervals.icu {summary['timezone']}"
            )
        for note in result.notes:
            if "differs" in note:
                logger.warning("intervals_athlete_difference", extra={"note": note})

    async def _sync_activities(
        self,
        session: AsyncSession,
        athlete: AthleteProfile,
        cursor: dict[str, Any],
        today: dt.date,
        full: bool,
        result: ResourceResult,
    ) -> None:
        oldest = self._window_start(cursor, today, full)
        result.oldest, result.newest = oldest.isoformat(), today.isoformat()
        # 'newest' is omitted on purpose: it defaults to "now", whereas a bare
        # date would be read as midnight and drop today's activities.
        payloads = await self._client.list_activities(oldest)
        result.fetched = len(payloads)
        repo = ActivityRepository(session)
        counts = UpsertCounts()
        hidden = 0
        for payload in payloads:
            if mappers.is_hidden_activity(payload):
                hidden += 1
                continue
            try:
                values = mappers.activity_values(payload)
            except mappers.MappingError as exc:
                result.skipped += 1
                logger.warning(
                    "intervals_activity_skipped",
                    extra={"activity_id": payload.get("id"), "reason": str(exc)},
                )
                continue
            counts.add(await repo.upsert(athlete.id, values))
        result.apply(counts)
        await self._mark_duplicates(repo, athlete, oldest, today, result)
        result.skipped += hidden
        if hidden:
            result.notes.append(
                f"{hidden} activities are hidden from the API (e.g. Strava imports) "
                "and were not stored"
            )

    @staticmethod
    async def _mark_duplicates(
        repo: ActivityRepository,
        athlete: AthleteProfile,
        oldest: dt.date,
        today: dt.date,
        result: ResourceResult,
    ) -> None:
        """Link recordings of the same ride (e.g. icTrainer + COROS watch); see activity_dedup."""
        # One day of margin: a ride crossing midnight can have different local dates.
        activities = await repo.list_between(athlete.id, oldest - dt.timedelta(days=1), today)
        duplicates = find_duplicates(
            Recording(
                id=a.id,
                start=a.start_time,
                duration_s=a.elapsed_time_s or a.moving_time_s or 0,
                activity_type=a.activity_type,
                has_power=a.avg_power_watts is not None,
                paired=a.paired_event_id is not None,
                moving_s=a.moving_time_s or 0,
            )
            for a in activities
        )
        changed = await repo.apply_duplicates(activities, duplicates)
        if duplicates:
            result.notes.append(
                f"{len(duplicates)} duplicate recordings linked to their primary activity "
                "and excluded from training load"
            )
        if changed:
            logger.info(
                "intervals_duplicates_updated",
                extra={"changed": changed, "duplicates": len(duplicates)},
            )

    async def _sync_wellness(
        self,
        session: AsyncSession,
        athlete: AthleteProfile,
        cursor: dict[str, Any],
        today: dt.date,
        full: bool,
        result: ResourceResult,
    ) -> None:
        oldest = self._window_start(cursor, today, full)
        result.oldest, result.newest = oldest.isoformat(), today.isoformat()
        payloads = await self._client.list_wellness(oldest, today)
        result.fetched = len(payloads)
        repo = DailyRecoveryRepository(session)
        counts = UpsertCounts()
        for payload in payloads:
            if not mappers.wellness_has_data(payload):
                result.skipped += 1  # only computed CTL/ATL, no recorded wellness
                continue
            try:
                values = mappers.wellness_values(payload)
            except mappers.MappingError as exc:
                result.skipped += 1
                logger.warning("intervals_wellness_skipped", extra={"reason": str(exc)})
                continue
            counts.add(await repo.upsert(athlete.id, values))
        result.apply(counts)

    async def _sync_calendar(
        self,
        session: AsyncSession,
        athlete: AthleteProfile,
        cursor: dict[str, Any],
        today: dt.date,
        full: bool,
        result: ResourceResult,
    ) -> None:
        oldest = self._window_start(cursor, today, full)
        newest = today + dt.timedelta(days=self._settings.intervals_calendar_future_days)
        result.oldest, result.newest = oldest.isoformat(), newest.isoformat()
        payloads = await self._client.list_events(oldest, newest)
        result.fetched = len(payloads)
        repo = CalendarEventRepository(session)
        counts = UpsertCounts()
        seen: list[str] = []
        planned = 0
        for payload in payloads:
            try:
                values = mappers.event_values(payload)
            except mappers.MappingError as exc:
                result.skipped += 1
                logger.warning(
                    "intervals_event_skipped",
                    extra={"event_id": payload.get("id"), "reason": str(exc)},
                )
                continue
            seen.append(values["source_event_id"])
            planned += values["category"] == "WORKOUT"
            counts.add(await repo.upsert(athlete.id, values))
        result.apply(counts)
        result.deleted = await repo.mark_missing_deleted(
            athlete.id, mappers.SOURCE, oldest, newest, seen, self._now()
        )
        result.notes.append(f"{planned} planned workouts (category WORKOUT) in window")
        # The incremental cursor is today, not the future horizon: future events
        # keep changing and are re-fetched on every run.
        result.cursor_newest = today.isoformat()
