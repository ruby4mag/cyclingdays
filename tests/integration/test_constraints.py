"""Database-level integrity guarantees (idempotency, safety, audit)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.database.connection import create_session_factory
from app.database.models import (
    Activity,
    AthleteProfile,
    PlannedWorkout,
    PublishingAudit,
    WebhookEvent,
)

pytestmark = pytest.mark.integration

DAY = dt.date(2026, 11, 5)


async def _athlete(session: AsyncSession, **overrides: Any) -> AthleteProfile:
    values: dict[str, Any] = {
        "slug": "primary",
        "display_name": "Test",
        "ftp_watts": 175,
        "experience_level": "intermediate",
        "timezone": "Asia/Kolkata",
    }
    values.update(overrides)
    athlete = AthleteProfile(**values)
    session.add(athlete)
    await session.flush()
    return athlete


def _activity(athlete_id: int, source_id: str = "i123") -> Activity:
    return Activity(
        athlete_id=athlete_id,
        source="intervals_icu",
        source_activity_id=source_id,
        start_time=dt.datetime(2026, 11, 4, 1, 0, tzinfo=dt.UTC),
        start_date_local=dt.date(2026, 11, 4),
    )


def _workout(athlete_id: int, external_id: str, status: str = "validated") -> PlannedWorkout:
    return PlannedWorkout(
        athlete_id=athlete_id,
        workout_date=DAY,
        external_id=external_id,
        status=status,
        workout_type="endurance",
        name="Z2 endurance",
        ftp_reference_watts=175,
    )


async def test_duplicate_activity_import_rejected(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        session.add(_activity(athlete.id))
        await session.commit()
        session.add(_activity(athlete.id))
        with pytest.raises(IntegrityError, match="uq_activities_source_source_activity_id"):
            await session.commit()


async def test_only_one_active_workout_per_day(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        session.add(_workout(athlete.id, "coach-1-2026-11-05-a"))
        await session.commit()
        session.add(_workout(athlete.id, "coach-1-2026-11-05-b"))
        with pytest.raises(IntegrityError, match="uq_planned_workouts_one_active_per_day"):
            await session.commit()


async def test_superseded_workout_allows_replacement(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        first = _workout(athlete.id, "coach-1-2026-11-05-a")
        session.add(first)
        await session.commit()
        first.status = "superseded"
        session.add(_workout(athlete.id, "coach-1-2026-11-05-b"))
        await session.commit()
        rows = (await session.execute(select(PlannedWorkout))).scalars().all()
        assert sorted(r.status for r in rows) == ["superseded", "validated"]


async def test_external_id_is_unique(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        session.add(_workout(athlete.id, "same-id", status="cancelled"))
        await session.commit()
        session.add(_workout(athlete.id, "same-id", status="cancelled"))
        with pytest.raises(IntegrityError, match="uq_planned_workouts_external_id"):
            await session.commit()


@pytest.mark.parametrize("ftp", [0, -10, 900])
async def test_invalid_ftp_rejected(engine: AsyncEngine, ftp: int) -> None:
    async with create_session_factory(engine)() as session:
        with pytest.raises(IntegrityError, match="ck_athlete_profile_ftp"):
            await _athlete(session, ftp_watts=ftp)


async def test_invalid_workout_status_rejected(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        session.add(_workout(athlete.id, "x", status="maybe"))
        with pytest.raises(IntegrityError, match="ck_planned_workouts_status"):
            await session.commit()


async def test_webhook_events_are_deduplicated(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        session.add(WebhookEvent(source="intervals_icu", dedup_key="evt-1"))
        await session.commit()
        session.add(WebhookEvent(source="intervals_icu", dedup_key="evt-1"))
        with pytest.raises(IntegrityError, match="uq_webhook_events_source_dedup_key"):
            await session.commit()


async def test_publishing_audit_is_append_only(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        session.add(
            PublishingAudit(
                athlete_id=athlete.id,
                action="skip",
                target="intervals_icu",
                actor="test",
                success=False,
                error="validation failed",
            )
        )
        await session.commit()
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("UPDATE coach.publishing_audit SET success = true"))
        await conn.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("DELETE FROM coach.publishing_audit"))


async def test_updated_at_trigger_fires_on_raw_sql(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        athlete = await _athlete(session)
        await session.commit()
        before = athlete.updated_at
    async with engine.begin() as conn:
        # now() is the transaction start time, so use a separate transaction.
        await conn.execute(text("UPDATE coach.athlete_profile SET ftp_watts = 180"))
    async with engine.connect() as conn:
        after = (await conn.execute(text("SELECT updated_at FROM coach.athlete_profile"))).scalar()
    assert after is not None and after > before
