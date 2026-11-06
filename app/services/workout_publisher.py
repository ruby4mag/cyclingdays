"""Publish validated workouts to the Intervals.icu calendar.

``WorkoutPublisher.publish(slug, day)`` makes the calendar match the planned
workout row for that day, idempotently. Steps:

1. **Lock.** A per-athlete advisory lock; a concurrent run is reported as
   ``locked``.
2. **Gate.** Re-run the deterministic validator on the stored structured
   workout with the stored validation context; check the FTP reference against
   the athlete profile **and** the Intervals.icu FTP (targets are % FTP, so the
   trainer's watts come from the Intervals.icu FTP).
3. **No competing plans.** A ``WORKOUT`` on that date not created by us (a
   different ``external_id``) blocks publishing: the athlete or another tool
   already planned the day.
4. **Upsert.** ``POST /events/bulk?upsert=true`` keyed by the stable
   ``external_id`` (``cycledays:<athlete>:<date>``): repeating it never creates
   a second event. Extra copies with our ``external_id`` (from a crash or a
   manual copy) are deleted. An event that already matches is left alone.
5. **Verify.** Read the event back and compare the parsed ``workout_doc``
   step by step. A mismatch deletes the event: a misread workout must not
   reach the trainer.

Rest days and rejected workouts remove our event for that day, if any.

Every external action, and every decision not to act, is written to the
append-only ``publishing_audit`` table in its own transaction, so the audit
survives failures of the surrounding run.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import PlannedWorkout
from app.database.repositories import (
    AthleteProfileRepository,
    PlannedWorkoutRepository,
    PublishingAuditRepository,
    try_advisory_xact_lock,
)
from app.integrations import intervals_workouts as fmt
from app.integrations.intervals_client import IntervalsClient, IntervalsError
from app.integrations.intervals_mappers import cycling_ftp
from app.training.workout_generator import StructuredWorkout
from app.training.workout_validator import ValidationContext, validate_workout

logger = logging.getLogger(__name__)

LOCK_NAMESPACE = "workout_publish"
AUDIT_TARGET = "intervals.icu"
AUDIT_ACTOR = "cycledays.publisher"
_FINAL_STATUSES = ("completed", "missed", "skipped")

Status = Literal[
    "published",
    "unchanged",
    "rest_day",
    "removed",
    "nothing_planned",
    "skipped",
    "blocked",
    "failed",
    "locked",
    "dry_run",
]


class PublishError(Exception):
    """Configuration problem that prevents publishing (e.g. unknown athlete)."""


@dataclass
class PublishResult:
    athlete_slug: str
    workout_date: str
    external_id: str
    status: Status = "failed"
    planned_workout_id: int | None = None
    intervals_event_id: int | None = None
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    event: dict[str, Any] | None = None  # payload (dry run) or verified event summary

    @property
    def ok(self) -> bool:
        return self.status in (
            "published",
            "unchanged",
            "rest_day",
            "removed",
            "nothing_planned",
            "skipped",
            "dry_run",
        )

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def _event_id(event: dict[str, Any]) -> int:
    return int(event["id"])


def _summary(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("workout_doc")
    doc: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "start_date_local": event.get("start_date_local"),
        "external_id": event.get("external_id"),
        "moving_time": event.get("moving_time"),
        "icu_training_load": event.get("icu_training_load"),
        "icu_intensity": event.get("icu_intensity"),
        "steps": len(doc.get("steps") or []),
    }


class WorkoutPublisher:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        client: IntervalsClient,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._client = client
        self._now = now

    def _clock(self) -> dt.datetime:
        return self._now or dt.datetime.now(dt.UTC)

    # --- audit -----------------------------------------------------------------

    async def _audit(
        self,
        *,
        athlete_id: int,
        row: PlannedWorkout | None,
        action: str,
        success: bool,
        request: dict[str, Any] | None = None,
        response: Any = None,
        validation: dict[str, Any] | None = None,
        error: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Own transaction: the audit row is committed even if the run fails later."""
        async with self._sessions() as session, session.begin():
            await PublishingAuditRepository(session).add(
                athlete_id=athlete_id,
                planned_workout_id=row.id if row is not None else None,
                action=action,
                target=AUDIT_TARGET,
                external_id=row.external_id if row is not None else None,
                actor=AUDIT_ACTOR,
                success=success,
                http_status=http_status,
                request_payload=request or {},
                response_payload=response
                if isinstance(response, dict)
                else {"items": response}
                if response is not None
                else {},
                validation_snapshot=validation or {},
                error=error,
            )

    # --- public API ------------------------------------------------------------

    async def publish(
        self, athlete_slug: str, day: dt.date, *, dry_run: bool = False
    ) -> PublishResult:
        external_id = fmt.external_id_for(athlete_slug, day.isoformat())
        result = PublishResult(athlete_slug, day.isoformat(), external_id)
        async with self._sessions() as session, session.begin():
            athlete = await AthleteProfileRepository(session).get_by_slug(athlete_slug)
            if athlete is None:
                raise PublishError(f"athlete profile {athlete_slug!r} not found")
            if not await try_advisory_xact_lock(session, LOCK_NAMESPACE, athlete.id):
                result.status = "locked"
                result.errors.append("another publish run holds the lock for this athlete")
                return result
            row = await PlannedWorkoutRepository(session).get_by_external_id(external_id)
            if row is None:
                result.status = "nothing_planned"
                return result
            result.planned_workout_id = row.id
            try:
                await self._publish_row(result, athlete.id, athlete.ftp_watts, row, dry_run=dry_run)
            except IntervalsError as exc:
                result.status = "failed"
                result.errors.append(str(exc))
                if not dry_run:
                    await self._audit(
                        athlete_id=athlete.id,
                        row=row,
                        action="skip",
                        success=False,
                        error=f"Intervals.icu error: {exc}",
                        http_status=exc.status_code,
                    )
        log = logger.info if result.ok else logger.error
        log("workout_publish", extra={"result": result.as_dict()})
        return result

    # --- steps -----------------------------------------------------------------

    async def _publish_row(
        self,
        result: PublishResult,
        athlete_id: int,
        profile_ftp: int | None,
        row: PlannedWorkout,
        *,
        dry_run: bool,
    ) -> None:
        if row.status in _FINAL_STATUSES:
            result.status = "skipped"
            result.warnings.append(f"planned workout is {row.status}; calendar left unchanged")
            return

        events = await self._client.list_events(
            row.workout_date, row.workout_date, categories=[fmt.EVENT_CATEGORY]
        )
        ours = [e for e in events if e.get("external_id") == row.external_id]
        others = [e for e in events if e.get("external_id") != row.external_id]

        if row.workout_type == "rest" or row.status == "rejected":
            await self._remove(result, athlete_id, row, ours, dry_run=dry_run)
            if row.status == "rejected":
                result.status = "blocked"
                result.errors.append("planned workout failed validation; not published")
            else:
                result.status = "removed" if ours else "rest_day"
            return

        workout, validation, problems = self._gate(row, profile_ftp)
        if not problems:
            intervals_ftp = cycling_ftp(await self._client.get_athlete())
            if intervals_ftp != workout.ftp_watts:
                problems.append(
                    f"Intervals.icu FTP is {intervals_ftp!r} W but the workout was planned with "
                    f"{workout.ftp_watts} W; update one of them and re-plan"
                )
        if not problems and others:
            names = ", ".join(repr(e.get("name")) for e in others)
            problems.append(
                f"the calendar already has another workout on {row.workout_date}: {names}. "
                "Not publishing a competing workout"
            )
        if problems:
            result.status = "blocked"
            result.errors.extend(problems)
            if not dry_run:
                await self._audit(
                    athlete_id=athlete_id,
                    row=row,
                    action="skip",
                    success=False,
                    validation=validation,
                    error="; ".join(problems),
                )
            return

        payload = fmt.to_event(workout)
        if dry_run:
            result.status = "dry_run"
            result.event = payload
            return

        # Duplicates with our external_id: keep one, delete the rest.
        keep = None
        if ours:
            preferred = [e for e in ours if str(e.get("id")) == row.intervals_event_id]
            keep = (preferred or sorted(ours, key=_event_id))[0]
            extras = [e for e in ours if e is not keep]
            await self._remove(result, athlete_id, row, extras, dry_run=False)

        if keep is not None and fmt.event_matches(keep, workout):
            event_id = _event_id(keep)
            result.actions.append(f"unchanged event {event_id}")
            changed = False
        else:
            action = "update" if keep is not None else "create"
            try:
                response = await self._client.upsert_events([payload])
            except IntervalsError as exc:
                await self._audit(
                    athlete_id=athlete_id,
                    row=row,
                    action=action,
                    success=False,
                    request=payload,
                    validation=validation,
                    error=str(exc),
                    http_status=exc.status_code,
                )
                raise
            if len(response) != 1:
                raise IntervalsError(f"events/bulk returned {len(response)} events, expected 1")
            event_id = _event_id(response[0])
            await self._audit(
                athlete_id=athlete_id,
                row=row,
                action=action,
                success=True,
                request=payload,
                response=_summary(response[0]),
                validation=validation,
            )
            result.actions.append(f"{action} event {event_id}")
            changed = True

        verified = await self._verify(result, athlete_id, row, workout, event_id)
        if not verified:
            return
        row.intervals_event_id = str(event_id)
        if changed or row.status != "published" or row.published_at is None:
            row.published_at = self._clock()
        row.status = "published"
        result.intervals_event_id = event_id
        result.status = "published" if changed else "unchanged"

    def _gate(
        self, row: PlannedWorkout, profile_ftp: int | None
    ) -> tuple[StructuredWorkout, dict[str, Any], list[str]]:
        """Deterministic re-validation right before publishing."""
        workout = StructuredWorkout.model_validate(row.structure["workout"])
        stored = row.validation_result or {}
        ctx = ValidationContext.from_dict(stored["context"])
        check = validate_workout(workout, ctx)
        validation = {**check.as_dict(), "context": ctx.as_dict()}
        problems = list(check.errors)
        if profile_ftp is None:
            problems.append("athlete FTP is missing; never publish without a valid FTP")
        elif profile_ftp != workout.ftp_watts or row.ftp_reference_watts != workout.ftp_watts:
            problems.append(
                f"profile FTP is {profile_ftp} W but the workout was planned with "
                f"{workout.ftp_watts} W; re-plan the day"
            )
        if row.workout_date != workout.workout_date:
            problems.append("stored workout date does not match the planned day")
        if row.structure.get("intervals_description") != fmt.to_description(workout):
            problems.append("stored Intervals.icu description does not match the workout")
        return workout, validation, problems

    async def _remove(
        self,
        result: PublishResult,
        athlete_id: int,
        row: PlannedWorkout,
        events: list[dict[str, Any]],
        *,
        dry_run: bool,
    ) -> None:
        for event in events:
            event_id = _event_id(event)
            if dry_run:
                result.actions.append(f"would delete event {event_id}")
                continue
            try:
                await self._client.delete_event(event_id)
            except IntervalsError as exc:
                await self._audit(
                    athlete_id=athlete_id,
                    row=row,
                    action="delete",
                    success=False,
                    request={"event_id": event_id},
                    error=str(exc),
                    http_status=exc.status_code,
                )
                raise
            await self._audit(
                athlete_id=athlete_id,
                row=row,
                action="delete",
                success=True,
                request={"event_id": event_id},
                response=_summary(event),
            )
            result.actions.append(f"delete event {event_id}")
            if row.intervals_event_id == str(event_id):
                row.intervals_event_id = None
        if events and not dry_run and row.status == "published":
            row.status = "validated"

    async def _verify(
        self,
        result: PublishResult,
        athlete_id: int,
        row: PlannedWorkout,
        workout: StructuredWorkout,
        event_id: int,
    ) -> bool:
        event = await self._client.get_event(event_id)
        problems = fmt.verify_event(event, workout)
        await self._audit(
            athlete_id=athlete_id,
            row=row,
            action="verify",
            success=not problems,
            request={"event_id": event_id},
            response=_summary(event),
            error="; ".join(problems) or None,
        )
        result.event = _summary(event)
        if not problems:
            result.actions.append(f"verified event {event_id}")
            return True
        # A workout Intervals.icu reads differently must not reach the trainer.
        result.status = "failed"
        result.errors.extend(problems)
        await self._remove(result, athlete_id, row, [event], dry_run=False)
        row.status = "validated"
        return False

    async def verify(self, athlete_slug: str, day: dt.date) -> PublishResult:
        """Retrieve the published event and verify it (read-only apart from the audit)."""
        external_id = fmt.external_id_for(athlete_slug, day.isoformat())
        result = PublishResult(athlete_slug, day.isoformat(), external_id)
        async with self._sessions() as session:
            athlete = await AthleteProfileRepository(session).get_by_slug(athlete_slug)
            if athlete is None:
                raise PublishError(f"athlete profile {athlete_slug!r} not found")
            row = await PlannedWorkoutRepository(session).get_by_external_id(external_id)
        if row is None or row.workout_type == "rest" or not row.structure:
            result.status = "nothing_planned" if row is None else "rest_day"
            return result
        result.planned_workout_id = row.id
        workout = StructuredWorkout.model_validate(row.structure["workout"])
        events = await self._client.list_events(day, day, categories=[fmt.EVENT_CATEGORY])
        ours = [e for e in events if e.get("external_id") == external_id]
        if len(ours) != 1:
            result.status = "failed"
            result.errors.append(
                f"expected 1 event with external_id {external_id}, found {len(ours)}"
            )
            return result
        event = await self._client.get_event(_event_id(ours[0]))
        problems = fmt.verify_event(event, workout)
        await self._audit(
            athlete_id=athlete.id,
            row=row,
            action="verify",
            success=not problems,
            request={"event_id": event["id"]},
            response=_summary(event),
            error="; ".join(problems) or None,
        )
        result.event = _summary(event)
        result.intervals_event_id = _event_id(event)
        result.errors.extend(problems)
        result.status = "unchanged" if not problems else "failed"
        return result
