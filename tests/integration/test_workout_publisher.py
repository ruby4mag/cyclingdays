"""Publishing to Intervals.icu (fake calendar): idempotency, gates, verification, audit."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.database.connection import create_session_factory
from app.database.models import AthleteProfile, CoachDecision, PlannedWorkout, PublishingAudit
from app.graph.nodes import CoachNodes
from app.integrations.intervals_client import IntervalsClient
from app.services.workout_publisher import WorkoutPublisher
from tests.conftest import make_unit_settings
from tests.fakes import FakeCalendar, endurance_workout, objective, sweet_spot_workout

pytestmark = pytest.mark.integration

DAY = dt.date(2026, 11, 6)
EXTERNAL_ID = "cycledays:primary:2026-11-06"


async def _noop(_: float) -> None:
    return None


def _client(cal: FakeCalendar) -> IntervalsClient:
    return IntervalsClient(
        api_key="k",
        athlete_id=cal.athlete_id,
        transport=httpx.MockTransport(cal.handler),
        max_retries=0,
        sleep=_noop,
    )


async def _athlete(factory: async_sessionmaker[AsyncSession], ftp: int | None = 175) -> int:
    async with factory() as session, session.begin():
        athlete = AthleteProfile(
            slug="primary",
            display_name="T",
            ftp_watts=ftp,
            timezone="Asia/Kolkata",
            experience_level="intermediate",
        )
        session.add(athlete)
        await session.flush()
        return athlete.id


def _context(ftp: int = 175, cap: str = "vo2max") -> dict[str, Any]:
    return {
        "plan_date": DAY.isoformat(),
        "today": (DAY - dt.timedelta(days=1)).isoformat(),
        "athlete": {"slug": "primary", "ftp_watts": ftp},
        "readiness": {"status": "green", "max_intensity": cap},
        "load": {"latest": {"ctl": 60.0, "ctl_authoritative": True}},
        "recent_activities": [],
        "recent_planned": [],
    }


async def _plan(
    factory: async_sessionmaker[AsyncSession],
    athlete_id: int,
    *,
    session_type: str = "sweet_spot",
    minutes: int = 75,
    workout: dict[str, Any] | None = None,
    cap: str = "vo2max",
) -> PlannedWorkout:
    """Store the day's plan through the real finalize code path."""
    async with factory() as session, session.begin():
        decision = CoachDecision(
            athlete_id=athlete_id, decision_date=DAY, decision_type="daily_plan", output={}
        )
        session.add(decision)
        await session.flush()
        state: Any = {
            "athlete_slug": "primary",
            "plan_date": DAY.isoformat(),
            "context": _context(cap=cap),
            "deterministic_cap": cap,
            "effective_cap": cap,
            "objective": objective(session_type, minutes),
            "workout": workout,
            "outcome": "rest" if workout is None else "planned",
        }
        nodes = CoachNodes(factory, None, 0)
        planned_id, _ = await nodes._store_planned_workout(session, state, athlete_id, decision.id)
    return await _row(factory, planned_id)


async def _row(factory: async_sessionmaker[AsyncSession], row_id: int | None) -> PlannedWorkout:
    async with factory() as session:
        row = await session.get(PlannedWorkout, row_id)
    assert row is not None
    return row


async def _audit(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, bool]]:
    async with factory() as session:
        rows = await session.execute(select(PublishingAudit).order_by(PublishingAudit.id))
        return [(a.action, a.success) for a in rows.scalars()]


async def _publish(factory: async_sessionmaker[AsyncSession], cal: FakeCalendar, **kw: Any) -> Any:
    async with _client(cal) as client:
        publisher = WorkoutPublisher(make_unit_settings(), factory, client)
        return await publisher.publish("primary", DAY, **kw)


async def test_publish_is_verified_and_idempotent(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    planned = await _plan(factory, athlete_id, workout=sweet_spot_workout())
    assert planned.status == "validated"
    cal = FakeCalendar()

    first = await _publish(factory, cal)
    assert first.status == "published" and first.ok, first.errors
    assert len(cal.events) == 1
    event = next(iter(cal.events.values()))
    assert (
        event["external_id"] == EXTERNAL_ID and event["start_date_local"] == "2026-11-06T00:00:00"
    )
    assert len(event["workout_doc"]["steps"]) == 8
    row = await _row(factory, planned.id)
    assert row.status == "published" and row.intervals_event_id == str(event["id"])
    assert row.published_at is not None
    assert await _audit(factory) == [("create", True), ("verify", True)]

    writes = len(cal.writes)
    second = await _publish(factory, cal)
    assert second.status == "unchanged" and second.intervals_event_id == event["id"]
    assert len(cal.events) == 1 and len(cal.writes) == writes  # no write at all
    assert (await _audit(factory))[-1] == ("verify", True)

    # A re-planned day updates the same event (stable external_id), never adds one.
    await _plan(
        factory, athlete_id, session_type="endurance", minutes=60, workout=endurance_workout()
    )
    third = await _publish(factory, cal)
    assert third.status == "published" and third.intervals_event_id == event["id"]
    assert len(cal.events) == 1 and cal.events[event["id"]]["name"] == "Endurance 60'"
    assert (await _audit(factory))[-2:] == [("update", True), ("verify", True)]


async def test_duplicates_with_our_external_id_are_removed(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar()
    for _ in range(2):  # e.g. two plain POSTs from an earlier crash
        cal.add(start_date_local="2026-11-06T00:00:00", external_id=EXTERNAL_ID, name="old")

    result = await _publish(factory, cal)

    assert result.status == "published"
    assert len(cal.events) == 1
    assert [a for a in await _audit(factory)] == [
        ("delete", True),
        ("update", True),
        ("verify", True),
    ]


async def test_competing_workout_blocks_publishing(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar()
    cal.add(start_date_local="2026-11-06T00:00:00", name="Coach Bob's VO2", external_id=None)

    result = await _publish(factory, cal)

    assert result.status == "blocked" and not result.ok
    assert "another workout" in result.errors[0]
    assert cal.writes == [] and len(cal.events) == 1
    assert await _audit(factory) == [("skip", False)]


@pytest.mark.parametrize(("profile_ftp", "intervals_ftp"), [(175, 200), (175, None)])
async def test_ftp_mismatch_or_missing_blocks_publishing(
    engine: AsyncEngine, profile_ftp: int, intervals_ftp: int | None
) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory, ftp=profile_ftp)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar(ftp=intervals_ftp)

    result = await _publish(factory, cal)

    assert result.status == "blocked" and "Intervals.icu FTP" in result.errors[0]
    assert cal.writes == []


async def test_profile_ftp_changed_since_planning_blocks(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    async with factory() as session, session.begin():
        athlete = await session.get(AthleteProfile, athlete_id)
        assert athlete is not None
        athlete.ftp_watts = 190

    result = await _publish(factory, FakeCalendar(ftp=190))

    assert result.status == "blocked" and "re-plan" in result.errors[0]


async def test_tampered_workout_fails_the_gate(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    planned = await _plan(
        factory, athlete_id, workout=endurance_workout(), session_type="endurance", minutes=60
    )
    async with factory() as session, session.begin():
        row = await session.get(PlannedWorkout, planned.id)
        assert row is not None
        structure = dict(row.structure)
        workout = dict(structure["workout"])
        steps = [dict(s) for s in workout["steps"]]
        steps[1].update(low_pct=118, high_pct=120, low_watts=206, high_watts=210, zone=6)
        workout["steps"] = steps
        row.structure = {**structure, "workout": workout}
    cal = FakeCalendar()

    result = await _publish(factory, cal)

    assert result.status == "blocked"
    assert any("above the allowed endurance" in e for e in result.errors)
    assert cal.writes == [] and await _audit(factory) == [("skip", False)]


async def test_misread_workout_is_deleted(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    planned = await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar()
    cal.corrupt_parse = True

    result = await _publish(factory, cal)

    assert result.status == "failed" and "step 1: duration" in result.errors[0]
    assert cal.events == {}
    row = await _row(factory, planned.id)
    assert row.status == "validated" and row.intervals_event_id is None
    assert await _audit(factory) == [("create", True), ("verify", False), ("delete", True)]


async def test_rest_day_removes_our_event_only(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar()
    assert (await _publish(factory, cal)).status == "published"
    note = cal.add(start_date_local="2026-11-06T00:00:00", category="NOTE", name="note")

    # Re-planned as a rest day (e.g. illness reported): the workout must go.
    await _plan(factory, athlete_id, session_type="rest", minutes=0, workout=None)
    result = await _publish(factory, cal)

    assert result.status == "removed"
    assert list(cal.events) == [note["id"]]
    again = await _publish(factory, cal)
    assert again.status == "rest_day" and again.ok


async def test_dry_run_writes_nothing(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar()

    result = await _publish(factory, cal, dry_run=True)

    assert result.status == "dry_run" and result.event is not None
    assert result.event["external_id"] == EXTERNAL_ID
    assert "- Work 12m 88-92%" in result.event["description"]
    assert cal.writes == [] and await _audit(factory) == []


async def test_nothing_planned_and_final_rows(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    cal = FakeCalendar()
    assert (await _publish(factory, cal)).status == "nothing_planned"

    planned = await _plan(factory, athlete_id, workout=sweet_spot_workout())
    async with factory() as session, session.begin():
        row = await session.get(PlannedWorkout, planned.id)
        assert row is not None
        row.status = "completed"
    result = await _publish(factory, cal)
    assert result.status == "skipped" and cal.requests == []

    # A completed day is never overwritten by a new plan either.
    again = await _plan(
        factory, athlete_id, session_type="endurance", minutes=60, workout=endurance_workout()
    )
    assert again.status == "completed" and again.workout_type == "sweet_spot"


async def test_verify_retrieves_and_checks_published_event(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    athlete_id = await _athlete(factory)
    await _plan(factory, athlete_id, workout=sweet_spot_workout())
    cal = FakeCalendar()
    await _publish(factory, cal)

    async with _client(cal) as client:
        publisher = WorkoutPublisher(make_unit_settings(), factory, client)
        ok = await publisher.verify("primary", DAY)
        event = next(iter(cal.events.values()))
        event["workout_doc"]["steps"][3]["power"] = {"units": "%ftp", "value": 120}
        bad = await publisher.verify("primary", DAY)

    assert ok.status == "unchanged" and ok.ok
    assert bad.status == "failed" and "step 4: power" in bad.errors[0]
