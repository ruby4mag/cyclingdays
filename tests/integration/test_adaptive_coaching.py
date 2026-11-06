"""Phase 7 against PostgreSQL: activity evaluation loop, plan guidance, adaptive planning."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx2 as httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.llm import LLMUnavailableError
from app.database.connection import create_session_factory
from app.database.models import (
    Activity,
    AthleteProfile,
    CoachDecision,
    PlannedWorkout,
    TrainingPlan,
    WebhookEvent,
    WorkoutPerformance,
)
from app.database.repositories import EventInboxRepository
from app.graph.context import build_context
from app.graph.workflow import DailyPlanRunner
from app.integrations.intervals_client import IntervalsClient
from app.scheduler import run_locked
from app.services.activity_processor import ActivityProcessor, activity_revision
from app.training.plan_builder import build_plan
from app.training.workout_generator import StepInput, WorkoutObjective, build_workout
from tests.conftest import make_unit_settings
from tests.fakes import ANALYSIS, ScriptedLLM, objective, recovery, step
from tests.integration.test_coach_graph import PLAN_DAY, _seed, saver  # noqa: F401

pytestmark = pytest.mark.integration

RIDE_DAY = PLAN_DAY - dt.timedelta(days=1)  # Thursday 2026-11-05
NOW = dt.datetime(2026, 11, 5, 14, 0, tzinfo=dt.UTC)  # 19:30 in Kolkata


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _structured(day: dt.date, session_type: str = "sweet_spot") -> dict[str, Any]:
    w = build_workout(
        athlete_slug="primary",
        workout_date=day,
        name="2x10 sweet spot",
        description="",
        ftp_watts=175,
        objective=WorkoutObjective(
            session_type=session_type,
            target_duration_minutes=45,
            primary_goal="",
            rationale="",
        ),
        steps=[
            StepInput(kind="warmup", duration_s=600, low_pct=50, high_pct=60),
            StepInput(kind="work", duration_s=600, low_pct=88, high_pct=92),
            StepInput(kind="recovery", duration_s=300, low_pct=50, high_pct=55),
            StepInput(kind="work", duration_s=600, low_pct=88, high_pct=92),
            StepInput(kind="cooldown", duration_s=600, low_pct=45, high_pct=55),
        ],
    )
    return w.model_dump(mode="json")


async def _athlete_id(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(await session.scalar(select(AthleteProfile.id)) or 0)


async def _planned(
    factory: async_sessionmaker[AsyncSession],
    day: dt.date,
    *,
    event_id: str | None = "500",
    session_type: str = "sweet_spot",
    status: str = "published",
) -> int:
    workout = _structured(day, session_type)
    async with factory() as session, session.begin():
        row = PlannedWorkout(
            athlete_id=await _athlete_id(factory),
            workout_date=day,
            external_id=f"cycledays:primary:{day}",
            intervals_event_id=event_id,
            status=status,
            workout_type=session_type,
            name=workout["name"],
            planned_duration_s=workout["total_s"],
            planned_training_load=workout["estimate"]["tss"],
            ftp_reference_watts=175,
            structure={"workout": workout},
            validation_result={"metrics": {"hard": False}},
        )
        session.add(row)
        await session.flush()
        return row.id


async def _ride(
    factory: async_sessionmaker[AsyncSession],
    day: dt.date,
    *,
    sid: str = "i900",
    paired: str | None = "500",
    rpe: float | None = 6,
    **kw: Any,
) -> int:
    async with factory() as session, session.begin():
        a = Activity(
            athlete_id=await _athlete_id(factory),
            source="intervals_icu",
            source_activity_id=sid,
            start_time=dt.datetime.combine(day, dt.time(12, 30), dt.UTC),
            start_date_local=day,
            activity_type="VirtualRide",
            name="2x10 sweet spot",
            is_indoor=True,
            moving_time_s=2700,
            avg_power_watts=125.0,
            normalized_power_watts=140.0,
            avg_hr_bpm=135.0,
            rpe=rpe,
            paired_event_id=paired,
            origin="OAUTH_CLIENT",
            **kw,
        )
        session.add(a)
        await session.flush()
        return a.id


class FakeStreams:
    """Serves ``/activity/{id}/streams`` from a planned workout (ERG-perfect power)."""

    def __init__(self, workout: dict[str, Any], scale: float = 1.0, fail: bool = False) -> None:
        power: list[float] = []
        for s in workout["steps"]:
            power += [(s["low_watts"] + s["high_watts"]) / 2 * scale] * s["duration_s"]
        self.power = power
        self.fail = fail
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.fail:
            return httpx.Response(404, content=b'{"error":"not found"}')
        if request.url.path.endswith("/streams"):
            n = len(self.power)
            body = [
                {"type": "time", "data": list(range(n))},
                {"type": "watts", "data": self.power},
                {"type": "heartrate", "data": [135] * n},
            ]
            return httpx.Response(200, content=json.dumps(body).encode())
        return httpx.Response(404, content=b"{}")


async def _sleep(_: float) -> None:
    return None


def _processor(
    factory: async_sessionmaker[AsyncSession],
    streams: FakeStreams | None,
    llm: Any = None,
    now: dt.datetime = NOW,
) -> ActivityProcessor:
    client = (
        None
        if streams is None
        else IntervalsClient(
            api_key="k",
            athlete_id="i42",
            max_retries=0,
            transport=httpx.MockTransport(streams.handler),
            sleep=_sleep,
        )
    )
    return ActivityProcessor(
        make_unit_settings(llm_max_corrections=1), factory, client, llm, now=lambda: now
    )


async def _count(factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _active_plan(
    factory: async_sessionmaker[AsyncSession], start: dt.date, **template: str
) -> None:
    phases = build_plan(start=start, weekly_hours=(6, 9))
    if template:
        assert phases.week_template is not None
        phases = phases.model_copy(update={"week_template": {**phases.week_template, **template}})
    async with factory() as session, session.begin():
        session.add(
            TrainingPlan(
                athlete_id=await _athlete_id(factory),
                name="Test plan",
                objective="1200 km brevet",
                start_date=phases.phases[0].start_date,
                end_date=phases.phases[-1].end_date,
                status="active",
                phases=phases.model_dump(mode="json"),
            )
        )


REVIEW = {
    "summary": "Both sweet-spot intervals on target.",
    "went_well": ["power on target"],
    "concerns": [],
    "recommendation": "keep",
    "next_session_note": "Continue as planned.",
}


# --------------------------------------------------------------------------
# Activity evaluation loop
# --------------------------------------------------------------------------


async def test_completed_ride_is_evaluated_once_and_marks_the_plan_completed(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    planned_id = await _planned(factory, RIDE_DAY)
    activity_id = await _ride(factory, RIDE_DAY)
    async with factory() as session:
        row = await session.get(PlannedWorkout, planned_id)
        assert row is not None
        streams = FakeStreams(row.structure["workout"])
    llm = ScriptedLLM({"performance_review": [REVIEW]})

    report = await _processor(factory, streams, llm).run("primary", lookback_days=1)

    assert report.ok, report.as_dict()
    assert report.processed == 1 and report.failed == 0
    async with factory() as session:
        perf = (
            await session.execute(
                select(WorkoutPerformance).where(WorkoutPerformance.activity_id == activity_id)
            )
        ).scalar_one()
        assert perf.planned_workout_id == planned_id
        assert perf.outcome == "as_planned"
        assert perf.completion_pct == 100.0 and perf.power_deviation_pct == 0.0
        assert perf.evaluation["power"]["method"] == "stream"
        assert perf.evaluation["review"]["recommendation"] == "keep"
        assert perf.session_date == RIDE_DAY
        planned = await session.get(PlannedWorkout, planned_id)
        assert planned is not None and planned.status == "completed"
        decision = (
            await session.execute(
                select(CoachDecision).where(CoachDecision.decision_type == "performance_review")
            )
        ).scalar_one()
        assert decision.prompt_version == "performance_review/v1"

    # Detecting the same ride again triggers nothing: no event, no LLM call.
    again = await _processor(factory, streams, llm).run("primary", lookback_days=1)
    assert again.detected == 0 and again.processed == 0
    assert llm.total_calls == 1
    assert await _count(factory, WebhookEvent) == 1
    assert await _count(factory, WorkoutPerformance) == 1


async def test_duplicate_event_delivery_is_ignored(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _planned(factory, RIDE_DAY)
    activity_id = await _ride(factory, RIDE_DAY)
    processor = _processor(factory, None)
    async with factory() as session:
        activity = await session.get(Activity, activity_id)
        assert activity is not None
        rev = activity_revision(activity)
    # The same event "delivered" twice before processing: one inbox row.
    await processor.run("primary", lookback_days=1)
    async with factory() as session, session.begin():
        inbox = EventInboxRepository(session)
        assert not await inbox.enqueue(
            "intervals_icu",
            f"activity:i900:{rev}",
            "activity.evaluate",
            {"athlete_id": 1, "activity_id": activity_id, "revision": rev},
        )
    assert await _count(factory, WebhookEvent) == 1
    assert await _count(factory, WorkoutPerformance) == 1


async def test_edited_ride_is_re_evaluated_in_place(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _planned(factory, RIDE_DAY)
    activity_id = await _ride(factory, RIDE_DAY, rpe=None)
    await _processor(factory, None).run("primary", lookback_days=1)
    # The athlete enters RPE later: a new revision, evaluated again, same row.
    async with factory() as session, session.begin():
        await session.execute(update(Activity).where(Activity.id == activity_id).values(rpe=9))
    report = await _processor(factory, None).run("primary", lookback_days=1)
    assert report.detected == 1 and report.processed == 1
    assert await _count(factory, WorkoutPerformance) == 1
    async with factory() as session:
        perf = (await session.execute(select(WorkoutPerformance))).scalar_one()
        assert perf.rpe == 9
        assert perf.evaluation["subjective"]["rpe_delta"] == 3.0
        assert perf.evaluation["power"]["method"] == "summary"  # no client: no streams


async def test_streams_failure_falls_back_to_totals(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    planned_id = await _planned(factory, RIDE_DAY)
    await _ride(factory, RIDE_DAY)
    async with factory() as session:
        row = await session.get(PlannedWorkout, planned_id)
        assert row is not None
        streams = FakeStreams(row.structure["workout"], fail=True)
    report = await _processor(factory, streams).run("primary", lookback_days=1)
    assert report.processed == 1
    async with factory() as session:
        perf = (await session.execute(select(WorkoutPerformance))).scalar_one()
        assert perf.evaluation["power"]["method"] == "summary"
        assert any("streams unavailable" in n for n in perf.evaluation["notes"])


async def test_missed_day_is_marked_and_a_late_upload_revives_it(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    planned_id = await _planned(factory, RIDE_DAY - dt.timedelta(days=1), event_id=None)
    report = await _processor(factory, None).run("primary", lookback_days=1)
    assert report.missed == [(RIDE_DAY - dt.timedelta(days=1)).isoformat()]
    async with factory() as session:
        row = await session.get(PlannedWorkout, planned_id)
        assert row is not None and row.status == "missed"
    # COROS/icTrainer upload arrives a day late: matched by date, day completed.
    await _ride(factory, RIDE_DAY - dt.timedelta(days=1), paired=None)
    late = await _processor(factory, None).run("primary", lookback_days=1)
    assert late.processed == 1 and late.missed == []
    async with factory() as session:
        row = await session.get(PlannedWorkout, planned_id)
        assert row is not None and row.status == "completed"


async def test_second_ride_of_the_day_is_unplanned(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _planned(factory, RIDE_DAY)
    await _ride(factory, RIDE_DAY)
    async with factory() as session, session.begin():
        session.add(
            Activity(
                athlete_id=await _athlete_id(factory),
                source="intervals_icu",
                source_activity_id="i901",
                start_time=dt.datetime.combine(RIDE_DAY, dt.time(3), dt.UTC),
                start_date_local=RIDE_DAY,
                activity_type="Ride",
                moving_time_s=1800,
                rpe=3,
            )
        )
    report = await _processor(factory, None).run("primary", lookback_days=1)
    assert report.processed == 2
    outcomes = sorted(e["outcome"] for e in report.evaluations)
    assert "unplanned" in outcomes


async def test_failing_evaluation_is_retried_then_recorded(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _planned(factory, RIDE_DAY)
    await _ride(factory, RIDE_DAY)

    class Boom:
        configured_model = "x"

        async def generate(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("unexpected")

    report = await _processor(factory, None, Boom()).run("primary", lookback_days=1)
    assert report.failed == 1 and not report.ok
    async with factory() as session:
        event = (await session.execute(select(WebhookEvent))).scalar_one()
        assert event.status == "failed" and "unexpected" in (event.last_error or "")
        assert event.attempts == 1  # retried on the next run, not in a tight loop
    # The LLM recovers: the next run retries the same event (no new one).
    llm = ScriptedLLM({"performance_review": [REVIEW]})
    report = await _processor(factory, None, llm).run("primary", lookback_days=1)
    assert report.processed == 1
    assert await _count(factory, WebhookEvent) == 1


async def test_llm_unavailable_keeps_deterministic_evaluation(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _planned(factory, RIDE_DAY)
    await _ride(factory, RIDE_DAY)
    llm = ScriptedLLM({"performance_review": [LLMUnavailableError("down")]})
    report = await _processor(factory, None, llm).run("primary", lookback_days=1)
    assert report.processed == 1
    async with factory() as session:
        perf = (await session.execute(select(WorkoutPerformance))).scalar_one()
        assert perf.outcome is not None and perf.evaluation["review"] is None


# --------------------------------------------------------------------------
# Guidance in the planning context
# --------------------------------------------------------------------------


async def test_context_guidance_follows_the_plan_and_recent_performance(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    # Plan started 8 weeks ago: the week of PLAN_DAY is in block 3 (threshold key).
    await _active_plan(factory, PLAN_DAY - dt.timedelta(weeks=8), fri="key")
    context = await build_context(factory, "primary", PLAN_DAY, now=NOW)
    g = context["guidance"]
    assert g["plan"] == "Test plan"
    assert g["prescription"]["role"] == "key"
    assert g["max_session_type"] == "threshold"

    # A struggled threshold session yesterday lowers today's key session.
    await _planned(factory, RIDE_DAY, session_type="threshold")
    await _ride(factory, RIDE_DAY, rpe=9)
    await _processor(factory, None).run("primary", lookback_days=1)
    context = await build_context(factory, "primary", PLAN_DAY, now=NOW)
    g = context["guidance"]
    assert g["max_session_type"] == "sweet_spot", g
    assert any("key session" in r for r in g["adaptation"]["reasons"])
    assert context["recent_evaluations"][0]["rpe"] == 9


async def test_missed_sessions_do_not_raise_the_next_days_budget(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _active_plan(factory, PLAN_DAY - dt.timedelta(weeks=8))
    sat = PLAN_DAY + dt.timedelta(days=1)
    before = (await build_context(factory, "primary", sat, now=NOW))["guidance"]
    for d in (3, 2, 1):  # Tue, Wed, Thu planned and missed
        await _planned(factory, PLAN_DAY - dt.timedelta(days=d), event_id=None)
    await _processor(factory, None).run("primary", lookback_days=1)
    after = (await build_context(factory, "primary", sat, now=NOW))["guidance"]
    assert after["max_tss"] <= before["max_tss"]
    assert after["max_minutes"] <= before["max_minutes"]
    assert after["max_session_type"] == before["max_session_type"]


# --------------------------------------------------------------------------
# Planning with the plan
# --------------------------------------------------------------------------


async def test_rest_role_plans_rest_without_llm(
    engine: AsyncEngine,
    saver: AsyncPostgresSaver,  # noqa: F811
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    await _active_plan(factory, PLAN_DAY - dt.timedelta(weeks=1), fri="rest")
    llm = ScriptedLLM({})
    runner = DailyPlanRunner(make_unit_settings(), factory, saver, llm)
    result = await runner.run("primary", PLAN_DAY)
    assert result["outcome"] == "rest"
    assert llm.total_calls == 0
    assert "long-term plan" in result["objective"]["rationale"]


async def test_llm_is_corrected_to_the_plans_session_type(
    engine: AsyncEngine,
    saver: AsyncPostgresSaver,  # noqa: F811
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    # Block 1: key sessions up to tempo.
    await _active_plan(factory, PLAN_DAY - dt.timedelta(days=4), fri="key")
    tempo = {
        "name": "Tempo 2x15",
        "description": "Tempo",
        "steps": [
            step("warmup", 10, 50, 60),
            step("work", 15, 78, 84),
            step("recovery", 5, 50, 55),
            step("work", 15, 78, 84),
            step("cooldown", 10, 45, 55),
        ],
    }
    llm = ScriptedLLM(
        {
            "training_analysis": [ANALYSIS],
            "recovery_assessment": [recovery("vo2max")],
            "objective_selection": [objective("sweet_spot", 60), objective("tempo", 55)],
            "workout_planning": [tempo],
        }
    )
    runner = DailyPlanRunner(make_unit_settings(llm_max_corrections=2), factory, saver, llm)
    result = await runner.run("primary", PLAN_DAY)
    assert result["outcome"] == "planned", result
    assert result["objective"]["session_type"] == "tempo"
    assert llm.calls["objective_selection"] == 2
    async with factory() as session:
        rejected = (
            await session.execute(
                select(CoachDecision).where(
                    CoachDecision.decision_type == "llm:objective_selection",
                    CoachDecision.output["accepted"].as_boolean().is_(False),
                )
            )
        ).scalar_one()
        assert any("long-term plan" in v for v in rejected.output["violations"])
        row = (await session.execute(select(PlannedWorkout))).scalar_one()
        assert row.validation_result["checks"]["plan"] == "passed"


# --------------------------------------------------------------------------
# Scheduler locking
# --------------------------------------------------------------------------


async def test_scheduler_job_lock_skips_a_concurrent_run(engine: AsyncEngine) -> None:
    calls: list[str] = []

    async def job() -> dict[str, Any]:
        calls.append("ran")
        return {}

    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT pg_advisory_lock(hashtext('scheduler'), hashtext('poll'))")
        )
        assert await run_locked(engine, "poll", job) is None
        await holder.execute(
            text("SELECT pg_advisory_unlock(hashtext('scheduler'), hashtext('poll'))")
        )
    assert await run_locked(engine, "poll", job) == {}
    assert calls == ["ran"]

    async def broken() -> dict[str, Any]:
        raise RuntimeError("boom")

    assert await run_locked(engine, "poll", broken) is None  # logged, not raised
    assert await run_locked(engine, "poll", job) == {}  # and the lock was released
