"""The LangGraph workflow end to end: PostgreSQL data, Postgres checkpointer, scripted LLM."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.llm import LLMUnavailableError
from app.agents.schemas import TrainingObjective, WorkoutDraft
from app.config import Settings
from app.database.connection import create_session_factory
from app.database.models import (
    Activity,
    AthleteProfile,
    CoachDecision,
    DailyRecovery,
    PlannedWorkout,
    PublishingAudit,
    SynchronizationState,
)
from app.graph.guards import workout_violations
from app.graph.workflow import DailyPlanRunner
from app.integrations.intervals_client import IntervalsClient
from app.services.workout_publisher import WorkoutPublisher
from app.training.intensity import Intensity
from tests.conftest import make_unit_settings
from tests.fakes import (
    ANALYSIS,
    FakeCalendar,
    ScriptedLLM,
    endurance_workout,
    objective,
    recovery,
    step,
    sweet_spot_workout,
)

pytestmark = pytest.mark.integration

PLAN_DAY = dt.date(2026, 11, 6)
NOW = dt.datetime(2026, 11, 5, 14, 0, tzinfo=dt.UTC)


def _conninfo(database_url: str) -> str:
    base = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    return f"{base}?options=-c%20search_path%3Dlanggraph"


@pytest.fixture
async def saver(test_database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    conninfo = _conninfo(test_database_url)
    async with AsyncPostgresSaver.from_conn_string(conninfo) as cp:
        await cp.setup()
        yield cp
    # Checkpoints live outside schema coach; clear them so threads don't leak
    # between tests.
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await conn.execute(f"DELETE FROM langgraph.{table}")


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, ftp: int | None = 175, **today: Any
) -> None:
    """Healthy athlete: 120 days of endurance riding and a stable 40-day baseline."""
    async with factory() as session, session.begin():
        athlete = AthleteProfile(
            slug="primary",
            display_name="T",
            ftp_watts=ftp,
            experience_level="intermediate",
            timezone="Asia/Kolkata",
            primary_objective="1200 km brevet",
        )
        session.add(athlete)
        await session.flush()
        session.add(
            SynchronizationState(
                athlete_id=athlete.id,
                source="intervals_icu",
                resource="activities",
                cursor={"coverage_start": (PLAN_DAY - dt.timedelta(days=130)).isoformat()},
                last_success_at=NOW,
            )
        )
        for i in range(3, 125, 2):
            day = PLAN_DAY - dt.timedelta(days=i)
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
                    activity_type="VirtualRide",
                )
            )
        for i in range(1, 41):
            values = {
                "hrv_rmssd_ms": 55 + (0 if i == 1 else 4 if i % 2 else -4),
                "resting_hr_bpm": 52 + (0 if i == 1 else 2 if i % 2 else -2),
                "sleep_duration_s": 27000 + (0 if i == 1 else 900 if i % 2 else -900),
            }
            if i == 1:
                values.update(today)
            session.add(
                DailyRecovery(
                    athlete_id=athlete.id,
                    source="intervals_icu",
                    recovery_date=PLAN_DAY - dt.timedelta(days=i),
                    **values,
                )
            )


def _settings() -> Settings:
    return make_unit_settings(llm_max_corrections=2)


async def _decisions(factory: async_sessionmaker[AsyncSession]) -> list[CoachDecision]:
    async with factory() as session:
        rows = await session.execute(select(CoachDecision).order_by(CoachDecision.id))
        return list(rows.scalars())


def _happy_script() -> dict[str, list[Any]]:
    return {
        "training_analysis": [ANALYSIS],
        "recovery_assessment": [recovery("vo2max")],
        "objective_selection": [objective("sweet_spot", 75)],
        "workout_planning": [sweet_spot_workout()],
    }


async def test_happy_path_plans_and_persists(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    llm = ScriptedLLM(_happy_script())

    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert result["status"] == "completed" and result["outcome"] == "planned"
    assert result["deterministic_cap"] == "vo2max" and result["readiness_status"] == "green"
    assert result["objective"]["session_type"] == "sweet_spot"
    assert result["workout_minutes"] == 75
    rows = await _decisions(factory)
    assert [r.decision_type for r in rows] == [
        "llm:training_analysis",
        "llm:recovery_assessment",
        "llm:objective_selection",
        "llm:workout_planning",
        "daily_plan",
    ]
    plan = rows[-1]
    assert plan.id == result["decision_id"]
    assert plan.model_name == "test-model-1-2026-01-01"  # model reported by the API
    assert plan.prompt_version == "daily_plan/v3"
    assert plan.output["prompt_versions"]["workout_planning"] == "workout_planning/v3"
    # Phase 5: the validated workout is stored as the day's planned workout.
    async with factory() as session:
        pw = await session.scalar(select(PlannedWorkout))
    assert pw is not None and pw.id == result["planned_workout_id"]
    assert pw.status == "validated" and pw.workout_type == "sweet_spot"
    assert pw.external_id == "cycledays:primary:2026-11-06"
    assert pw.coach_decision_id == plan.id and pw.ftp_reference_watts == 175
    assert pw.planned_duration_s == 75 * 60 and pw.planned_training_load
    assert pw.validation_result["valid"] is True
    assert pw.validation_result["checks"]["weekly"] == "passed"
    assert pw.structure["intervals_description"].rstrip().endswith("- Cooldown 9m 45-55%")
    assert result["validation"]["valid"] is True
    assert plan.input_snapshot["readiness"]["max_intensity"] == "vo2max"  # full context stored
    assert plan.graph_thread_id == "daily-plan:primary:2026-11-06"
    llm_row = rows[0]
    assert llm_row.prompt_version == "training_analysis/v1"
    assert len(llm_row.input_snapshot["prompt_sha256"]) == 64
    assert llm_row.output["accepted"] is True and llm_row.output["usage"]["input_tokens"] == 100


async def test_state_persists_and_rerun_does_not_call_llm(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    llm = ScriptedLLM(_happy_script())
    first = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)
    calls = llm.total_calls

    # A new runner (new process) with the same checkpointer database.
    second = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert second["status"] == "already_planned"
    assert second["decision_id"] == first["decision_id"]
    assert second["workout"] == first["workout"]
    assert llm.total_calls == calls
    assert len(await _decisions(factory)) == 5
    tables = await _langgraph_tables(saver)
    assert {"checkpoints", "checkpoint_writes", "checkpoint_blobs"} <= tables

    forced = await DailyPlanRunner(_settings(), factory, saver, llm).run(
        "primary", PLAN_DAY, force=True
    )
    assert forced["status"] == "completed" and forced["thread_id"] != first["thread_id"]


async def _langgraph_tables(saver: AsyncPostgresSaver) -> set[str]:
    async with saver.conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'langgraph'"
        )
        return {row["table_name"] for row in await cur.fetchall()}


async def test_crashed_run_resumes_from_checkpoint(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    script = _happy_script()
    script["workout_planning"] = [RuntimeError("process killed"), sweet_spot_workout()]
    llm = ScriptedLLM(script)
    runner = DailyPlanRunner(_settings(), factory, saver, llm)

    with pytest.raises(RuntimeError, match="process killed"):
        await runner.run("primary", PLAN_DAY)
    assert await _decisions(factory) == []

    resumed = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert resumed["status"] == "resumed" and resumed["outcome"] == "planned"
    # Completed nodes were not re-run: one call each, workout planning twice.
    assert dict(llm.calls) == {
        "training_analysis": 1,
        "recovery_assessment": 1,
        "objective_selection": 1,
        "workout_planning": 2,
    }


async def test_llm_cannot_exceed_cap_correction_then_accept(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory, sleep_duration_s=23000)  # 85 % of usual sleep -> caution -> tempo cap
    llm = ScriptedLLM(
        {
            "training_analysis": [ANALYSIS],
            "recovery_assessment": [recovery("vo2max")],  # tries to raise the cap: clamped
            "objective_selection": [objective("vo2max", 60), objective("endurance", 60)],
            "workout_planning": [endurance_workout(60)],
        }
    )

    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert result["deterministic_cap"] == "tempo" and result["effective_cap"] == "tempo"
    assert result["outcome"] == "planned" and result["objective"]["session_type"] == "endurance"
    rejected = [c for c in result["llm_calls"] if not c["accepted"]]
    assert len(rejected) == 1 and "exceeds" in rejected[0]["violations"][0]
    rows = await _decisions(factory)
    assert sum(r.decision_type == "llm:objective_selection" for r in rows) == 2  # audit both


async def test_persistent_violation_falls_back_within_cap(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory, sleep_duration_s=23000)
    llm = ScriptedLLM(
        {
            "training_analysis": [ANALYSIS],
            "recovery_assessment": [recovery("tempo")],
            "objective_selection": [objective("endurance", 60)],
            # Always sneaks a threshold block into an endurance ride.
            "workout_planning": [
                {
                    **endurance_workout(60),
                    "steps": [
                        *endurance_workout(60)["steps"][:2],
                        {
                            "kind": "work",
                            "duration_seconds": 300,
                            "power_low_pct_ftp": 100,
                            "power_high_pct_ftp": 105,
                            "cadence_rpm": None,
                            "note": "",
                        },
                        endurance_workout(60)["steps"][2],
                    ],
                }
            ],
        }
    )

    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert result["outcome"] == "fallback"
    assert llm.calls["workout_planning"] == 3  # 1 + 2 corrections
    assert "rejected after corrections" in result["fallback_reason"]
    obj = TrainingObjective.model_validate(result["objective"])
    draft = WorkoutDraft.model_validate(result["workout"])
    assert workout_violations(draft, obj, Intensity.TEMPO, 175) == []


async def test_api_outage_uses_deterministic_fallback(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    llm = ScriptedLLM({"training_analysis": [LLMUnavailableError("503 after 3 attempts")]})

    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert result["outcome"] == "fallback"
    assert "LLM unavailable" in result["fallback_reason"]
    assert result["objective"]["session_type"] == "endurance"  # conservative even when green
    rows = await _decisions(factory)
    assert rows[-1].decision_type == "daily_plan" and rows[-1].model_name is None
    assert rows[0].output["error"] == "503 after 3 attempts"


async def test_no_llm_configured_falls_back(engine: AsyncEngine, saver: AsyncPostgresSaver) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    result = await DailyPlanRunner(_settings(), factory, saver, None).run("primary", PLAN_DAY)
    assert result["outcome"] == "fallback" and "OPENAI_API_KEY" in result["fallback_reason"]


async def test_illness_routes_to_rest_without_llm(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory, illness_reported=True)
    llm = ScriptedLLM(_happy_script())

    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert result["outcome"] == "rest" and result["deterministic_cap"] == "rest"
    assert llm.total_calls == 0
    assert "illness reported" in result["objective"]["rationale"]
    assert result["workout"] is None


async def test_llm_may_choose_rest(engine: AsyncEngine, saver: AsyncPostgresSaver) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    script = _happy_script()
    script["objective_selection"] = [objective("rest", 0)]
    llm = ScriptedLLM(script)
    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)
    assert result["outcome"] == "rest" and llm.calls["workout_planning"] == 0


async def test_missing_ftp_aborts(engine: AsyncEngine, saver: AsyncPostgresSaver) -> None:
    factory = create_session_factory(engine)
    await _seed(factory, ftp=None)
    llm = ScriptedLLM(_happy_script())
    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)
    assert result["outcome"] == "aborted" and "FTP" in result["abort_reason"]
    assert llm.total_calls == 0
    rows = await _decisions(factory)
    assert rows[-1].output["outcome"] == "aborted"


async def test_unknown_athlete_aborts_without_decision(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    result = await DailyPlanRunner(_settings(), factory, saver, None).run("ghost", PLAN_DAY)
    assert result["outcome"] == "aborted" and result["decision_id"] is None
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CoachDecision)) == 0


# --- Phase 5: structure corrections and publishing end to end -------------------------


async def test_llm_is_corrected_on_interval_structure(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    no_recovery = sweet_spot_workout()
    no_recovery["steps"] = [s for s in no_recovery["steps"] if s["kind"] != "recovery"]
    no_recovery["steps"].insert(-1, step("steady", 15, 60, 65))  # keep 75 min
    script = {**_happy_script(), "workout_planning": [no_recovery, sweet_spot_workout()]}
    llm = ScriptedLLM(script)

    result = await DailyPlanRunner(_settings(), factory, saver, llm).run("primary", PLAN_DAY)

    assert result["outcome"] == "planned"
    first, second = [c for c in result["llm_calls"] if c["node"] == "workout_planning"]
    assert not first["accepted"] and second["accepted"]
    assert any("need a recovery step between them" in v for v in first["violations"])
    assert "rejected by deterministic validation" in llm.prompts[-1][1]


async def test_plan_publish_replan_updates_the_same_event(
    engine: AsyncEngine, saver: AsyncPostgresSaver
) -> None:
    factory = create_session_factory(engine)
    await _seed(factory)
    cal = FakeCalendar()
    client = IntervalsClient(
        api_key="k", athlete_id=cal.athlete_id, transport=httpx.MockTransport(cal.handler)
    )
    publisher = WorkoutPublisher(_settings(), factory, client)

    plan = await DailyPlanRunner(_settings(), factory, saver, ScriptedLLM(_happy_script())).run(
        "primary", PLAN_DAY
    )
    first = await publisher.publish("primary", PLAN_DAY)
    assert plan["outcome"] == "planned" and first.status == "published", first.errors
    (event,) = cal.events.values()
    assert event["name"] == "Sweet spot 3x12"

    script = {
        **_happy_script(),
        "objective_selection": [objective("endurance", 60)],
        "workout_planning": [endurance_workout()],
    }
    replanned = await DailyPlanRunner(_settings(), factory, saver, ScriptedLLM(script)).run(
        "primary", PLAN_DAY, force=True
    )
    second = await publisher.publish("primary", PLAN_DAY)
    await client.aclose()

    assert replanned["planned_workout_id"] == plan["planned_workout_id"]
    assert second.status == "published" and second.intervals_event_id == event["id"]
    assert list(cal.events) == [event["id"]] and cal.events[event["id"]]["name"] == "Endurance 60'"
    async with factory() as session:
        rows = (await session.execute(select(PlannedWorkout))).scalars().all()
        audit = (await session.execute(select(PublishingAudit.action))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "published"
    assert rows[0].coach_decision_id == replanned["decision_id"]
    assert audit == ["create", "verify", "update", "verify"]
