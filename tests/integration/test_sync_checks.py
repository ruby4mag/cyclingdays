"""Phase 6 against PostgreSQL: provenance, duplicate recordings, stored sync checks."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.database.models import Activity, AthleteProfile, PlannedWorkout, SyncCheck
from app.database.repositories import ActivityRepository
from app.services.sync_verification import SyncVerifier, UnknownCheckError
from tests.integration.test_intervals_sync import NOW, TODAY, FakeIntervals, _seed, _service

pytestmark = pytest.mark.integration

RIDE_DAY = TODAY - dt.timedelta(days=1)


def _ride(id_: str, **kw: Any) -> dict[str, Any]:
    ride: dict[str, Any] = {
        "id": id_,
        "start_date_local": f"{RIDE_DAY.isoformat()}T18:00:00",
        "start_date": f"{RIDE_DAY.isoformat()}T12:30:00Z",
        "type": "VirtualRide",
        "name": "Gentle Z1 Recovery Spin",
        "trainer": True,
        "moving_time": 1800,
        "elapsed_time": 1830,
        "icu_average_watts": 74,
        "icu_weighted_avg_watts": 75,
        "icu_training_load": 9,
    }
    ride.update(kw)
    return ride


def _fake() -> FakeIntervals:
    fake = FakeIntervals()
    fake.athlete.update(
        {
            "coros_user_id": "u1",
            "coros_sync_activities": True,
            "coros_download_wellness": True,
            "coros_upload_workouts": True,
        }
    )
    fake.activities = [
        _ride("i1", source="OAUTH_CLIENT", oauth_client_name="icTrainer", paired_event_id=500),
        # The same ride recorded by the watch: no power, starts a minute earlier.
        _ride(
            "i2",
            source="COROS",
            type="Ride",
            name="Indoor Cycling",
            start_date=f"{RIDE_DAY.isoformat()}T12:29:00Z",
            icu_average_watts=None,
            icu_weighted_avg_watts=None,
            moving_time=1900,
            elapsed_time=1900,
        ),
        _ride("i3", source="COROS", type="Run", name="Run", start_date=f"{TODAY}T01:00:00Z"),
    ]
    return fake


async def _plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        athlete = (await session.execute(select(AthleteProfile))).scalar_one()
        session.add(
            PlannedWorkout(
                athlete_id=athlete.id,
                workout_date=RIDE_DAY,
                external_id=f"cycledays:primary:{RIDE_DAY}",
                intervals_event_id="500",
                status="published",
                workout_type="recovery",
                name="Gentle Z1 Recovery Spin",
                planned_duration_s=1800,
                structure={"workout": {"estimate": {"average_power_watts": 74.4}}},
            )
        )


async def test_sync_stores_provenance_and_links_duplicate_recordings(engine: AsyncEngine) -> None:
    fake = _fake()
    service, factory = _service(engine, fake)
    await _seed(factory)

    report = await service.run("primary")
    activities_result = next(r for r in report.results if r.resource == "activities")
    assert any("1 duplicate recordings" in n for n in activities_result.notes)

    async with factory() as session:
        rows = {
            a.source_activity_id: a for a in (await session.execute(select(Activity))).scalars()
        }
        assert rows["i1"].origin == "OAUTH_CLIENT" and rows["i1"].origin_client == "icTrainer"
        assert rows["i1"].paired_event_id == "500"
        assert rows["i2"].origin == "COROS"
        assert rows["i2"].duplicate_of_activity_id == rows["i1"].id  # power file kept
        assert rows["i1"].duplicate_of_activity_id is None
        assert rows["i3"].duplicate_of_activity_id is None  # a run is another sport

        repo = ActivityRepository(session)
        counted = {
            a.source_activity_id for a in await repo.list_until(rows["i1"].athlete_id, TODAY)
        }
        assert counted == {"i1", "i3"}  # the duplicate isn't counted toward load
        everything = await repo.list_until(rows["i1"].athlete_id, TODAY, include_duplicates=True)
        assert len(everything) == 3

    # Repeating the sync keeps the link; deleting the primary upstream releases it.
    await service.run("primary")
    fake.activities = [a for a in fake.activities if a["id"] != "i1"]
    async with factory() as session, session.begin():
        await session.execute(delete(Activity).where(Activity.source_activity_id == "i1"))
    await service.run("primary")
    async with factory() as session:
        i2 = (
            await session.execute(select(Activity).where(Activity.source_activity_id == "i2"))
        ).scalar_one()
        assert i2.duplicate_of_activity_id is None


async def test_sync_checks_are_evaluated_and_stored_only_when_changed(
    engine: AsyncEngine,
) -> None:
    fake = _fake()
    service, factory = _service(engine, fake)
    await _seed(factory)
    await service.run("primary")
    await _plan(factory)
    verifier = SyncVerifier(factory, now=lambda: NOW)

    report = await verifier.run("primary")
    by = {c.check_name: c for c in report.checks}
    assert by["coros.link"].status == "configured"
    assert by["intervals_to_ictrainer.planned"].status == "verified"
    assert by["ictrainer.erg_execution"].status == "verified", by["ictrainer.erg_execution"]
    assert by["ictrainer_to_intervals.activities"].status == "verified"
    assert by["coros_to_intervals.activities"].status == "verified"
    assert by["coros_to_intervals.wellness"].status == "verified"
    assert by["intervals_to_coros.workouts"].status == "configured"
    assert by["activities.duplicates"].status == "attention"
    assert report.stored == 8

    again = await verifier.run("primary")
    assert again.stored == 0

    await verifier.confirm("primary", "intervals_to_coros.workouts", "workout on the watch")
    confirmed = await verifier.run("primary")
    assert confirmed.stored == 1
    assert {c.check_name: c.status for c in confirmed.checks}[
        "intervals_to_coros.workouts"
    ] == "confirmed"

    async with factory() as session:
        rows = (await session.execute(select(SyncCheck).order_by(SyncCheck.id))).scalars().all()
        assert len(rows) == 10
        assert [r.actor for r in rows].count("athlete") == 1

    with pytest.raises(UnknownCheckError):
        await verifier.confirm("primary", "activities.duplicates", "x")
