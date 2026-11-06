"""End-to-end sync into PostgreSQL with Intervals.icu mocked at the HTTP layer."""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.database.connection import create_session_factory
from app.database.models import (
    Activity,
    AthleteProfile,
    CalendarEvent,
    DailyRecovery,
    SynchronizationState,
)
from app.integrations.intervals_client import IntervalsClient
from app.logging_config import REDACTED
from app.services.intervals_sync import IntervalsSyncService
from tests.conftest import make_unit_settings

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 11, 5, 4, 0, tzinfo=dt.UTC)  # 09:30 in Asia/Kolkata
TODAY = dt.date(2026, 11, 5)


def _activity(i: int, day: dt.date) -> dict[str, Any]:
    return {
        "id": f"i{1000 + i}",
        "start_date_local": f"{day.isoformat()}T06:30:00",
        "start_date": f"{day.isoformat()}T01:00:00Z",
        "type": "VirtualRide",
        "name": f"Ride {i}",
        "trainer": True,
        "moving_time": 3600,
        "icu_weighted_avg_watts": 140,
        "icu_training_load": 55,
        "icu_intensity": 80.0,
    }


class FakeIntervals:
    """Serves documented endpoints from in-memory data; records requests."""

    def __init__(self) -> None:
        self.athlete: dict[str, Any] = {
            "id": "i42",
            "name": "Test Rider",
            "timezone": "Asia/Kolkata",
            "icu_weight": 86.0,
            "icu_api_key": "must-not-be-stored",
            "sportSettings": [{"types": ["Ride", "VirtualRide"], "ftp": 180}],
        }
        self.activities = [_activity(i, TODAY - dt.timedelta(days=i * 3)) for i in range(35)]
        self.activities.append(
            {
                "id": "999",
                "icu_athlete_id": "i42",
                "start_date_local": "2026-11-01T07:00:00",
                "source": "STRAVA",
                "_note": "STRAVA activities are not available via the API",
            }
        )
        self.wellness: list[dict[str, Any]] = [
            {
                "id": (TODAY - dt.timedelta(days=d)).isoformat(),
                "ctl": 40.0,
                "atl": 42.0,
                "restingHR": 50 + d % 3,
                "hrv": 45.0,
            }
            for d in range(10)
        ] + [{"id": (TODAY - dt.timedelta(days=11)).isoformat(), "ctl": 39.0, "atl": 40.0}]
        self.events: list[dict[str, Any]] = [
            {
                "id": 1,
                "category": "WORKOUT",
                "type": "Ride",
                "name": "Endurance",
                "start_date_local": "2026-11-06T00:00:00",
                "moving_time": 5400,
            },
            {
                "id": 2,
                "category": "NOTE",
                "name": "Travel",
                "start_date_local": "2026-11-10T00:00:00",
            },
        ]
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, content=b'{"error":"x"}')
        path, params = request.url.path, request.url.params
        if path == "/api/v1/athlete/i42":
            body: Any = self.athlete
        elif path == "/api/v1/athlete/i42/activities":
            oldest = params["oldest"]
            body = [a for a in self.activities if a["start_date_local"][:10] >= oldest]
        elif path == "/api/v1/athlete/i42/wellness":
            body = [w for w in self.wellness if params["oldest"] <= w["id"] <= params["newest"]]
        elif path == "/api/v1/athlete/i42/events":
            body = [
                e
                for e in self.events
                if params["oldest"] <= e["start_date_local"][:10] <= params["newest"]
            ]
        else:
            return httpx.Response(404, content=b"{}")
        return httpx.Response(200, content=json.dumps(body).encode())


async def _sleep(_: float) -> None:
    return None


def _service(
    engine: AsyncEngine, fake: FakeIntervals, now: dt.datetime = NOW
) -> tuple[IntervalsSyncService, async_sessionmaker[AsyncSession]]:
    settings = make_unit_settings(
        intervals_history_days=90, intervals_sync_overlap_days=7, intervals_calendar_future_days=28
    )
    client = IntervalsClient(
        api_key="k",
        athlete_id="i42",
        max_retries=1,
        transport=httpx.MockTransport(fake.handler),
        sleep=_sleep,
    )
    factory = create_session_factory(engine)
    return IntervalsSyncService(factory, client, settings, now=lambda: now), factory


async def _seed(factory: async_sessionmaker[AsyncSession], **overrides: Any) -> None:
    async with factory() as session, session.begin():
        values: dict[str, Any] = {
            "slug": "primary",
            "display_name": "T",
            "ftp_watts": 175,
            "weight_kg": 86.0,
            "experience_level": "intermediate",
            "timezone": "Asia/Kolkata",
        }
        values.update(overrides)
        session.add(AthleteProfile(**values))


async def _count(factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_first_sync_stores_history_and_links_athlete(engine: AsyncEngine) -> None:
    fake = FakeIntervals()
    service, factory = _service(engine, fake)
    await _seed(factory)

    report = await service.run("primary")

    assert report.ok, report.as_dict()
    by = {r.resource: r for r in report.results}
    # 90 days of history: activities every 3 days -> days 0..90 -> 31 activities.
    assert by["activities"].oldest == "2026-08-07"
    assert by["activities"].inserted == 31
    assert by["activities"].skipped == 1  # the hidden Strava stub
    assert by["wellness"].inserted == 10 and by["wellness"].skipped == 1
    assert by["calendar"].inserted == 2
    assert any("FTP differs" in n for n in by["athlete"].notes)

    async with factory() as session:
        athlete = (await session.execute(select(AthleteProfile))).scalar_one()
        assert athlete.intervals_athlete_id == "i42"
        assert athlete.ftp_watts == 175  # never overwritten from the source
        assert athlete.intervals_snapshot["icu_api_key"] == REDACTED
        span = await session.execute(
            select(func.min(Activity.start_date_local), func.max(Activity.start_date_local))
        )
        oldest, newest = span.one()
        assert (newest - oldest).days >= 30
        states = (await session.execute(select(SynchronizationState))).scalars().all()
        assert {s.resource for s in states} == {"athlete", "activities", "wellness", "calendar"}
        assert all(s.last_success_at == NOW and s.consecutive_failures == 0 for s in states)


async def test_repeated_sync_creates_no_duplicates(engine: AsyncEngine) -> None:
    fake = FakeIntervals()
    service, factory = _service(engine, fake)
    await _seed(factory)
    await service.run("primary")
    counts = [await _count(factory, m) for m in (Activity, DailyRecovery, CalendarEvent)]

    fake.requests.clear()
    second, _ = _service(engine, fake, NOW + dt.timedelta(hours=6))
    report = await second.run("primary")

    assert report.ok
    assert [await _count(factory, m) for m in (Activity, DailyRecovery, CalendarEvent)] == counts
    by = {r.resource: r for r in report.results}
    assert by["activities"].inserted == 0 and by["activities"].updated == 0
    assert by["activities"].unchanged == by["activities"].fetched - 1
    # Incremental window: previous cursor (today) minus the 7-day overlap.
    acts = next(r for r in fake.requests if r.url.path.endswith("/activities"))
    assert acts.url.params["oldest"] == "2026-10-29"
    # Coverage keeps the earliest synced day across incremental windows.
    async with factory() as session:
        state = (
            await session.execute(
                select(SynchronizationState).where(SynchronizationState.resource == "activities")
            )
        ).scalar_one()
        assert state.cursor["coverage_start"] == "2026-08-07"
        assert state.cursor["oldest"] == "2026-10-29"

    # --full ignores the cursor but still deduplicates.
    full = await second.run("primary", full=True)
    assert full.ok
    assert await _count(factory, Activity) == counts[0]


async def test_changed_and_new_records_are_applied(engine: AsyncEngine) -> None:
    fake = FakeIntervals()
    service, factory = _service(engine, fake)
    await _seed(factory)
    await service.run("primary")

    fake.activities[0] = {**copy.deepcopy(fake.activities[0]), "icu_rpe": 7}  # edited later
    fake.activities.append(_activity(99, TODAY))  # late upload
    fake.events = [e for e in fake.events if e["id"] != 2]  # note deleted in Intervals
    report = await service.run("primary", resources=["activities", "calendar"])

    by = {r.resource: r for r in report.results}
    assert by["activities"].updated == 1 and by["activities"].inserted == 1
    assert by["calendar"].deleted == 1
    async with factory() as session:
        edited = (
            await session.execute(select(Activity).where(Activity.source_activity_id == "i1000"))
        ).scalar_one()
        assert edited.rpe == 7.0
        note = (
            await session.execute(select(CalendarEvent).where(CalendarEvent.source_event_id == "2"))
        ).scalar_one()
        assert note.deleted_at is not None

    # The event reappears -> undeleted.
    fake.events.append(
        {"id": 2, "category": "NOTE", "name": "Travel", "start_date_local": "2026-11-10T00:00:00"}
    )
    await service.run("primary", resources=["calendar"])
    async with factory() as session:
        note = (
            await session.execute(select(CalendarEvent).where(CalendarEvent.source_event_id == "2"))
        ).scalar_one()
        assert note.deleted_at is None


async def test_api_failure_is_recorded_and_other_resources_continue(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeIntervals()
    service, factory = _service(engine, fake)
    await _seed(factory, intervals_athlete_id="i42")
    fake.fail_with = 500

    report = await service.run("primary", resources=["activities", "wellness"])

    assert not report.ok
    assert [r.status for r in report.results] == ["failed", "failed"]
    assert "IntervalsServerError" in (report.results[0].error or "")
    assert "intervals_sync_resource_failed" in caplog.text
    async with factory() as session:
        state = (
            await session.execute(
                select(SynchronizationState).where(SynchronizationState.resource == "activities")
            )
        ).scalar_one()
        assert state.consecutive_failures == 1
        assert state.last_success_at is None
        assert "HTTP 500" in (state.last_error or "")

    fake.fail_with = None
    assert (await service.run("primary", resources=["activities"])).ok
    async with factory() as session:
        state = (
            await session.execute(
                select(SynchronizationState).where(SynchronizationState.resource == "activities")
            )
        ).scalar_one()
        assert state.consecutive_failures == 0 and state.last_error is None


async def test_athlete_mismatch_stops_sync(engine: AsyncEngine) -> None:
    fake = FakeIntervals()
    service, factory = _service(engine, fake)
    await _seed(factory, intervals_athlete_id="i7")

    report = await service.run("primary")

    assert not report.ok
    assert [r.resource for r in report.results] == ["athlete"]  # nothing else ran
    assert "refusing to mix data" in (report.results[0].error or "")
    assert await _count(factory, Activity) == 0


async def test_unknown_athlete_slug_fails_cleanly(engine: AsyncEngine) -> None:
    service, _ = _service(engine, FakeIntervals())
    report = await service.run("nobody", resources=["activities"])
    assert not report.ok
    assert "not found" in (report.results[0].error or "")


async def test_concurrent_run_is_skipped_while_locked(engine: AsyncEngine) -> None:
    from app.database.repositories import try_advisory_xact_lock
    from app.services.intervals_sync import LOCK_NAMESPACE

    fake = FakeIntervals()
    service, factory = _service(engine, fake)
    await _seed(factory)
    async with factory() as session:
        athlete_id = (await session.execute(select(AthleteProfile.id))).scalar_one()

    async with factory() as holder, holder.begin():
        assert await try_advisory_xact_lock(holder, LOCK_NAMESPACE, athlete_id)
        report = await service.run("primary", resources=["activities"])

    assert report.results[0].status == "locked"
    assert report.ok  # locked is a skip, not a failure
    assert await _count(factory, Activity) == 0
