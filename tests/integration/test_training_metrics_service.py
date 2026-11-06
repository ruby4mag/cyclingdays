"""TrainingMetricsService against PostgreSQL: persistence, reproducibility, readiness."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.database.connection import create_session_factory
from app.database.models import (
    Activity,
    AthleteProfile,
    DailyRecovery,
    SynchronizationState,
    TrainingMetric,
    TrainingPlan,
)
from app.services.training_metrics import TrainingMetricsError, TrainingMetricsService

pytestmark = pytest.mark.integration

DAY = dt.date(2026, 11, 5)
NOW = dt.datetime(2026, 11, 5, 4, 0, tzinfo=dt.UTC)
COVERAGE = DAY - dt.timedelta(days=120)


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, coverage: dt.date | None = COVERAGE
) -> int:
    async with factory() as session, session.begin():
        athlete = AthleteProfile(
            slug="primary",
            display_name="T",
            ftp_watts=175,
            experience_level="intermediate",
            timezone="Asia/Kolkata",
        )
        session.add(athlete)
        await session.flush()
        if coverage is not None:
            session.add(
                SynchronizationState(
                    athlete_id=athlete.id,
                    source="intervals_icu",
                    resource="activities",
                    cursor={
                        "coverage_start": coverage.isoformat(),
                        "oldest": "2026-10-29",
                        "newest": DAY.isoformat(),
                    },
                    last_success_at=NOW,
                )
            )
        # An endurance ride every other day: 1.5 h at NP 131 W (IF ~0.75, TSS ~84).
        for i in range(2, 121, 2):
            day = DAY - dt.timedelta(days=i)
            session.add(
                Activity(
                    athlete_id=athlete.id,
                    source="intervals_icu",
                    source_activity_id=f"i{i}",
                    start_time=dt.datetime.combine(day, dt.time(1), dt.UTC),
                    start_date_local=day,
                    moving_time_s=5400,
                    normalized_power_watts=131.0,
                    ftp_at_activity_watts=175,
                )
            )
        # Personal baseline: 40 days of wellness.
        for i in range(0, 40):
            session.add(
                DailyRecovery(
                    athlete_id=athlete.id,
                    source="intervals_icu",
                    recovery_date=DAY - dt.timedelta(days=i),
                    # alternating around the mean; today sits on the mean
                    hrv_rmssd_ms=55 + (0 if i == 0 else 4 if i % 2 else -4),
                    resting_hr_bpm=52 + (0 if i == 0 else 2 if i % 2 else -2),
                    sleep_duration_s=27000 + (0 if i == 0 else 900 if i % 2 else -900),
                )
            )
        return athlete.id


def _service(
    engine: AsyncEngine,
) -> tuple[TrainingMetricsService, async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    return TrainingMetricsService(factory, now=lambda: NOW), factory


async def test_metrics_persisted_and_reproducible(engine: AsyncEngine) -> None:
    service, factory = _service(engine)
    await _seed(factory)

    first = await service.run("primary")

    assert first.day == DAY.isoformat()
    assert first.series_start == COVERAGE.isoformat()
    assert first.metrics_written == {"inserted": 121, "updated": 0, "unchanged": 0}
    assert first.load_methods == {"power": 60}
    latest: dict[str, Any] = first.latest  # type: ignore[assignment]
    assert latest["ctl_authoritative"] and latest["atl_authoritative"]
    assert 25 < latest["ctl"] < 45  # ~42 TSS/day average
    assert first.weeks and first.weeks[-1]["week_start"] == "2026-11-02"

    second = await service.run("primary")
    assert second.metrics_written == {"inserted": 0, "updated": 0, "unchanged": 121}
    assert second.latest == first.latest
    assert second.readiness == first.readiness

    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(TrainingMetric))
        row = (
            await session.execute(
                select(TrainingMetric).where(TrainingMetric.metric_date == DAY - dt.timedelta(2))
            )
        ).scalar_one()
    assert count == 121
    assert row.calculation_version == "v2"
    assert row.daily_tss == pytest.approx(5400 * 131 * (131 / 175) / (175 * 3600) * 100, abs=1e-3)
    assert row.details["load_methods"] == {"power": 1}
    assert row.details["ctl_authoritative"] is True


async def test_readiness_from_stored_data(engine: AsyncEngine) -> None:
    service, factory = _service(engine)
    athlete_id = await _seed(factory)

    report = await service.run("primary")
    readiness = report.readiness
    assert readiness["status"] == "green"
    assert readiness["max_intensity"] == "vo2max"
    assert readiness["confidence"] == "high"

    # A recovery phase in the active plan caps intensity; status stays green.
    async with factory() as session, session.begin():
        session.add(
            TrainingPlan(
                athlete_id=athlete_id,
                name="Brevet 2027",
                objective="1200 km",
                start_date=DAY - dt.timedelta(days=30),
                status="active",
                phases={
                    "phases": [
                        {
                            "name": "Recovery week",
                            "type": "recovery",
                            "start_date": "2026-11-03",
                            "end_date": "2026-11-09",
                        }
                    ]
                },
            )
        )
    capped = (await service.run("primary", store=False)).readiness
    assert capped["max_intensity"] == "endurance" and capped["status"] == "green"
    assert any("recovery phase" in c["reason"] for c in capped["caps"])


async def test_illness_and_injury_from_wellness_rows(engine: AsyncEngine) -> None:
    service, factory = _service(engine)
    await _seed(factory)
    async with factory() as session, session.begin():
        today = (
            await session.execute(select(DailyRecovery).where(DailyRecovery.recovery_date == DAY))
        ).scalar_one()
        today.subjective_injury = 3
    readiness = (await service.run("primary", store=False)).readiness
    assert readiness["max_intensity"] == "rest"
    assert readiness["status"] == "red"


async def test_no_sync_coverage_means_unknown_load(engine: AsyncEngine) -> None:
    service, factory = _service(engine)
    await _seed(factory, coverage=None)

    report = await service.run("primary", store=False)

    assert report.coverage_start is None
    assert report.metrics_written == {"inserted": 0, "updated": 0, "unchanged": 0}
    latest: dict[str, Any] = report.latest  # type: ignore[assignment]
    assert not latest["ctl_authoritative"] and latest["data_completeness"] == 0.0
    assert report.readiness["max_intensity"] == "endurance"
    assert any("no synchronized activity history" in n for n in report.notes)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(TrainingMetric)) == 0


async def test_unknown_athlete(engine: AsyncEngine) -> None:
    service, _ = _service(engine)
    with pytest.raises(TrainingMetricsError):
        await service.run("nobody")
