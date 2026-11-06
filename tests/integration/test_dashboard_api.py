"""Dashboard API against a migrated database (Phase 8)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.connection import create_session_factory
from app.database.models import (
    Activity,
    AthleteProfile,
    CalendarEvent,
    CoachDecision,
    PlannedWorkout,
    SynchronizationState,
    TrainingMetric,
    TrainingPlan,
    WorkoutPerformance,
)
from app.main import create_app
from app.training import CALCULATION_VERSION
from app.training.plan_builder import build_plan
from tests.unit.test_auth import PASSWORD, FakeUserStore, rider

pytestmark = pytest.mark.integration

TZ = ZoneInfo("Asia/Kolkata")
TODAY = dt.datetime.now(TZ).date()
MONDAY = TODAY - dt.timedelta(days=TODAY.weekday())
YESTERDAY = TODAY - dt.timedelta(days=1)
TOMORROW = TODAY + dt.timedelta(days=1)
STEPS = [
    {"index": 1, "kind": "warmup", "section": "warmup", "duration_s": 600, "low_pct": 55,
     "high_pct": 55, "low_watts": 96, "high_watts": 96, "zone": 2, "note": "easy"},
    {"index": 2, "kind": "steady", "section": "main", "duration_s": 3000, "low_pct": 70,
     "high_pct": 70, "low_watts": 122, "high_watts": 122, "zone": 2, "note": None},
]  # fmt: skip


def _ride(athlete_id: int, day: dt.date, sid: str, **extra: Any) -> Activity:
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "source": "intervals_icu",
        "source_activity_id": sid,
        "start_time": dt.datetime.combine(day, dt.time(7), TZ),
        "start_date_local": day,
        "activity_type": "VirtualRide",
        "name": f"Ride {sid}",
        "moving_time_s": 3600,
        "distance_m": 30000.0,
        "normalized_power_watts": 131.0,
        "ftp_at_activity_watts": 175,
    }
    values.update(extra)
    return Activity(**values)


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    phases = build_plan(start=MONDAY - dt.timedelta(weeks=1), weekly_hours=(6, 9))
    async with factory() as session, session.begin():
        athlete = AthleteProfile(
            slug="primary",
            display_name="Test Rider",
            ftp_watts=175,
            weight_kg=86.0,
            experience_level="intermediate",
            timezone="Asia/Kolkata",
            primary_objective="1200 km brevet",
            intervals_snapshot={"sportSettings": [{"types": ["Ride"], "ftp": 180}]},
        )
        session.add(athlete)
        await session.flush()
        aid = athlete.id
        plan = TrainingPlan(
            athlete_id=aid,
            name="Test plan",
            objective="1200 km brevet",
            start_date=phases.phases[0].start_date,
            end_date=phases.phases[-1].end_date,
            status="active",
            phases=phases.model_dump(mode="json"),
        )
        session.add(plan)
        await session.flush()
        done = PlannedWorkout(
            athlete_id=aid, training_plan_id=plan.id, workout_date=YESTERDAY,
            external_id="cd-done", status="completed", workout_type="endurance",
            name="Steady endurance", planned_duration_s=3600, planned_training_load=45,
            ftp_reference_watts=175, intervals_event_id="ev-done",
        )  # fmt: skip
        missed = PlannedWorkout(
            athlete_id=aid, training_plan_id=plan.id,
            workout_date=TODAY - dt.timedelta(days=3), external_id="cd-missed",
            status="missed", workout_type="tempo", name="Tempo", planned_duration_s=3600,
        )  # fmt: skip
        upcoming = PlannedWorkout(
            athlete_id=aid, training_plan_id=plan.id, workout_date=TOMORROW,
            external_id="cd-next", status="published", workout_type="endurance",
            name="Tomorrow's ride", planned_duration_s=3600, planned_training_load=44.5,
            ftp_reference_watts=175, intervals_event_id="ev-next",
            published_at=dt.datetime.now(dt.UTC),
            structure={"workout": {"steps": STEPS, "estimate": {"intensity_factor": 0.66},
                                   "objective": {"rationale": "aerobic base"}}},
        )  # fmt: skip
        session.add_all([done, missed, upcoming])
        await session.flush()

        evaluated = _ride(aid, YESTERDAY, "a1", rpe=4.0, paired_event_id="ev-done")
        icu_only = _ride(
            aid,
            TODAY - dt.timedelta(days=5),
            "a2",
            raw_payload={"icu_zone_times": [{"id": "Z2", "secs": 1800}, {"id": "SS", "secs": 60}]},
        )
        session.add_all([evaluated, icu_only])
        await session.flush()
        duplicate = _ride(aid, YESTERDAY, "a3", duplicate_of_activity_id=evaluated.id)
        session.add(duplicate)
        session.add(
            WorkoutPerformance(
                athlete_id=aid,
                activity_id=evaluated.id,
                planned_workout_id=done.id,
                completion_pct=100.0,
                power_deviation_pct=-1.5,
                outcome="as_planned",
                decoupling_pct=3.2,
                session_date=YESTERDAY,
                evaluation={
                    "zones": {"seconds_by_zone": {"1": 300, "2": 3300}, "missing_seconds": 0},
                    "power": {"method": "stream"},
                    "review": {"recommendation": "keep", "summary": "Well paced."},
                    "notes": [],
                },
            )
        )
        session.add(
            CalendarEvent(
                athlete_id=aid, source="intervals_icu", source_event_id="ev-next",
                category="WORKOUT", start_date_local=TOMORROW, name="Tomorrow's ride",
                external_id="cd-next",
            )
        )  # fmt: skip
        session.add(
            CalendarEvent(
                athlete_id=aid, source="intervals_icu", source_event_id="ev-other",
                category="NOTE", start_date_local=TOMORROW, name="Dentist",
            )
        )  # fmt: skip
        for i in range(60):
            day = TODAY - dt.timedelta(days=i)
            session.add(
                TrainingMetric(
                    athlete_id=aid,
                    metric_date=day,
                    calculation_version=CALCULATION_VERSION,
                    daily_tss=45.0 if day in (YESTERDAY, TODAY - dt.timedelta(days=5)) else 0.0,
                    ctl=20.0 - i * 0.1,
                    atl=15.0,
                    tsb=5.0 - i * 0.1,
                    rolling_7d_tss=90.0,
                    data_completeness=1.0,
                    details={"ctl_authoritative": i < 30, "tsb_authoritative": i < 30,
                             "atl_authoritative": True, "reasons": []},
                )
            )  # fmt: skip
        session.add_all(
            [
                CoachDecision(
                    athlete_id=aid,
                    decision_date=TOMORROW,
                    decision_type="daily_plan",
                    model_name="test-model",
                    input_snapshot={
                        "readiness": {
                            "status": "green",
                            "confidence": "high",
                            "max_intensity": "threshold",
                            "caps": [],
                            "factors": [{"name": "hrv", "status": "normal"}],
                        }
                    },
                    output={
                        "outcome": "planned",
                        "effective_cap": "endurance",
                        "objective": {"session_type": "endurance", "primary_goal": "base"},
                        "recovery_assessment": {"summary": "fine"},
                        "prompt_versions": {"x": "x/v1"},
                    },
                ),
                CoachDecision(
                    athlete_id=aid,
                    decision_date=YESTERDAY,
                    decision_type="performance_review",
                    output={
                        "review": {
                            "recommendation": "keep",
                            "summary": "Well paced.",
                            "went_well": ["pacing"],
                            "concerns": [],
                        },
                        "accepted": True,
                    },
                ),
                CoachDecision(
                    athlete_id=aid, decision_date=TOMORROW, decision_type="llm:workout_planning"
                ),
            ]
        )
        session.add(
            SynchronizationState(
                athlete_id=aid,
                source="intervals_icu",
                resource="activities",
                last_success_at=dt.datetime.now(dt.UTC),
            )
        )
    return {"athlete": aid, "evaluated": evaluated.id}


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def client(integration_settings: Settings, test_database_url: str) -> Iterator[TestClient]:
    db = test_database_url.rsplit("/", 1)[1]
    settings = integration_settings.model_copy(update={"postgres_db": db})
    with TestClient(create_app(settings)) as c:
        # Login itself is covered by test_login.py; here only the data matters.
        c.app.state.users = FakeUserStore(rider(athlete_slug="primary"))  # type: ignore[attr-defined]
        response = c.post("/login", data={"username": "rider", "password": PASSWORD})
        assert response.status_code == 200, response.text  # followed to /dashboard
        yield c


async def _row_counts(engine: AsyncEngine) -> dict[str, int]:
    out = {}
    async with engine.connect() as conn:
        for table in ("activities", "planned_workouts", "coach_decisions", "training_metrics",
                      "workout_performance", "training_plans", "publishing_audit"):  # fmt: skip
            out[table] = int(await conn.scalar(text(f"SELECT count(*) FROM coach.{table}")) or 0)
    return out


async def test_dashboard_sections(
    engine: AsyncEngine, factory: async_sessionmaker[AsyncSession], client: TestClient
) -> None:
    ids = await _seed(factory)
    before = await _row_counts(engine)

    summary = client.get("/api/dashboard/summary")
    assert summary.status_code == 200, summary.text
    assert summary.headers["cache-control"] == "no-store"
    s = summary.json()
    assert s["today"] == TODAY.isoformat()
    assert s["athlete"]["name"] == "Test Rider"
    assert s["ftp"]["watts"] == 175 and s["ftp"]["w_per_kg"] == 2.03
    # Differing FTP on Intervals.icu is surfaced (publishing would be blocked).
    assert s["ftp"]["intervals_icu_watts"] == 180 and s["ftp"]["matches_intervals_icu"] is False
    assert len(s["ftp"]["zones"]) == 7
    assert s["fitness"]["date"] == TODAY.isoformat() and s["fitness"]["ctl"] == 20.0
    assert s["readiness"]["status"] == "green" and s["readiness"]["effective_cap"] == "endurance"
    nxt = s["next_workout"]
    assert nxt["date"] == TOMORROW.isoformat() and nxt["published"] is True
    assert [st["duration_s"] for st in nxt["steps"]] == [600, 3000]
    assert s["plan"]["name"] == "Test plan" and s["plan"]["phase"] == "Block 1 week 2"
    assert s["sync"][0]["resource"] == "activities"

    fitness = client.get("/api/dashboard/fitness", params={"days": 45}).json()
    assert len(fitness["points"]) == 45
    assert fitness["points"][-1]["date"] == TODAY.isoformat()
    # Older points keep their "not authoritative" flag.
    assert fitness["points"][0]["ctl_authoritative"] is False
    assert fitness["points"][-1]["ctl_authoritative"] is True

    weekly = client.get("/api/dashboard/weekly", params={"weeks": 2}).json()["weeks"]
    assert [w["week_start"] for w in weekly] == [
        (MONDAY - dt.timedelta(weeks=1)).isoformat(),
        MONDAY.isoformat(),
    ]
    assert sum(w["rides"] for w in weekly) == 2  # the duplicate recording is not counted
    assert weekly[0]["target_tss"] == 300.0 and weekly[1]["target_tss"] == 324.0
    assert sum(w["tss"] for w in weekly) == 90.0

    zones = client.get("/api/dashboard/zones", params={"days": 28}).json()
    assert (zones["rides"], zones["rides_coach"], zones["rides_intervals_icu"]) == (2, 1, 1)
    z2 = next(z for z in zones["zones"] if z["zone"] == 2)
    assert z2["seconds"] == 3300 + 1800

    workouts = client.get("/api/dashboard/workouts", params={"days": 28}).json()
    assert workouts["duplicates_hidden"] == 1
    assert workouts["workouts"][0]["id"] == ids["evaluated"]  # newest first
    first = workouts["workouts"][0]
    assert first["outcome"] == "as_planned" and first["planned_name"] == "Steady endurance"
    assert first["recommendation"] == "keep" and first["method"] == "stream"
    assert first["tss"] == pytest.approx(56.0, abs=0.1)  # 1 h at IF 0.749
    assert workouts["workouts"][1]["evaluated"] is False

    cal = client.get("/api/dashboard/calendar").json()
    days = {d["date"]: d for d in cal["days"]}
    assert len(cal["days"]) == 35 and cal["start"] == (MONDAY - dt.timedelta(weeks=2)).isoformat()
    assert days[YESTERDAY.isoformat()]["state"] == "completed"
    assert len(days[YESTERDAY.isoformat()]["rides"]) == 1  # duplicate hidden
    assert days[(TODAY - dt.timedelta(days=3)).isoformat()]["state"] == "missed"
    assert days[TOMORROW.isoformat()]["state"] == "planned"
    # The coach's own published event isn't listed twice; other events are.
    assert [e["name"] for e in days[TOMORROW.isoformat()]["events"]] == ["Dentist"]

    found = client.get("/api/dashboard/assessments", params={"limit": 10}).json()["assessments"]
    assert [a["type"] for a in found] == ["daily_plan", "performance_review"]
    assert found[0]["outcome"] == "planned" and found[1]["recommendation"] == "keep"

    plan = client.get("/api/dashboard/plan").json()["plan"]
    assert plan["open_ended"] is True and plan["weekly_hours"] == [6.0, 9.0]
    assert plan["current_index"] == 1 and plan["weeks_done"] == 1
    assert plan["weeks"][0]["state"] == "past" and plan["weeks"][1]["state"] == "current"
    assert plan["ctl_now"] == 20.0

    # Reading the dashboard never writes anything.
    assert await _row_counts(engine) == before


async def test_dashboard_without_data_and_errors(
    factory: async_sessionmaker[AsyncSession], client: TestClient
) -> None:
    assert client.get("/api/dashboard/summary").status_code == 404  # athlete not seeded
    async with factory() as session, session.begin():
        session.add(
            AthleteProfile(
                slug="primary",
                display_name="New",
                experience_level="beginner",
                timezone="Asia/Kolkata",
            )
        )
    s = client.get("/api/dashboard/summary").json()
    assert s["ftp"]["watts"] is None and s["ftp"]["zones"] == []
    assert s["fitness"] is None and s["readiness"] is None and s["next_workout"] is None
    assert s["plan"] is None
    assert client.get("/api/dashboard/plan").json()["plan"] is None
    weekly = client.get("/api/dashboard/weekly", params={"weeks": 1}).json()["weeks"][0]
    assert weekly["tss"] is None  # no metrics: unknown, not zero
    assert client.get("/api/dashboard/zones").json()["total_seconds"] == 0
    assert client.get("/api/dashboard/fitness", params={"days": 0}).status_code == 422
    assert client.get("/api/dashboard/weekly", params={"weeks": 53}).status_code == 422
    bad = client.get(
        "/api/dashboard/calendar",
        params={"start": TODAY.isoformat(), "end": (TODAY + dt.timedelta(days=70)).isoformat()},
    )
    assert bad.status_code == 422 and "62" in bad.json()["detail"]
    reversed_range = client.get(
        "/api/dashboard/calendar",
        params={"start": TODAY.isoformat(), "end": YESTERDAY.isoformat()},
    )
    assert reversed_range.status_code == 422
