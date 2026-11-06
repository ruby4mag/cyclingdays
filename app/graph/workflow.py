"""The daily coaching graph and its runner.

```
START -> gather_context --analyse--> training_analysis -> recovery_assessment
              |  rest -> rest_day -> finalize              |        |  rest -> rest_day
              |  abort -> abort -> finalize                |        v
              v                                            |  objective_selection
                                                           |   | rest -> finalize
         (any LLM node) --fallback--> fallback -> finalize |   v
                                                        workout_planning -> finalize -> END
```

Persistence: LangGraph's ``AsyncPostgresSaver`` writes a checkpoint after
every node, in schema ``langgraph`` (its own tables; never touched by
Alembic). The thread id is ``daily-plan:<athlete>:<date>``:

* Re-running a completed day returns the stored result; nothing is called
  or written again.
* A run that crashed (process killed, DB outage) resumes from the last
  checkpoint, so completed LLM calls are not repeated.
* ``force=True`` starts a new thread for the same day (suffix ``:run-<id>``).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.llm import LLMUnavailableError, OpenAIStructuredLLM, StructuredLLM
from app.config import Settings
from app.database.repositories import AthleteProfileRepository
from app.graph import routing
from app.graph.nodes import CoachNodes
from app.graph.state import CoachState

logger = logging.getLogger(__name__)


def build_graph(nodes: CoachNodes) -> StateGraph[CoachState]:
    g: StateGraph[CoachState] = StateGraph(CoachState)
    g.add_node("gather_context", nodes.gather_context)
    g.add_node("training_analysis", nodes.training_analysis)
    g.add_node("recovery_assessment", nodes.recovery_assessment)
    g.add_node("objective_selection", nodes.objective_selection)
    g.add_node("workout_planning", nodes.workout_planning)
    g.add_node("rest_day", nodes.rest_day)
    g.add_node("fallback", nodes.fallback)
    g.add_node("abort", nodes.abort)
    g.add_node("finalize", nodes.finalize)

    g.add_edge(START, "gather_context")
    g.add_conditional_edges("gather_context", routing.after_context, routing.AFTER_CONTEXT)
    g.add_conditional_edges("training_analysis", routing.after_analysis, routing.AFTER_ANALYSIS)
    g.add_conditional_edges("recovery_assessment", routing.after_recovery, routing.AFTER_RECOVERY)
    g.add_conditional_edges("objective_selection", routing.after_objective, routing.AFTER_OBJECTIVE)
    g.add_conditional_edges("workout_planning", routing.after_workout, routing.AFTER_WORKOUT)
    for terminal in ("rest_day", "fallback", "abort"):
        g.add_edge(terminal, "finalize")
    g.add_edge("finalize", END)
    return g


def thread_id_for(slug: str, plan_date: dt.date, *, force: bool = False) -> str:
    base = f"daily-plan:{slug}:{plan_date.isoformat()}"
    return f"{base}:run-{uuid.uuid4().hex[:8]}" if force else base


def summarize(state: dict[str, Any], *, status: str) -> dict[str, Any]:
    objective = state.get("objective") or {}
    workout = state.get("workout")
    return {
        "status": status,
        "thread_id": state.get("thread_id"),
        "plan_date": state.get("plan_date"),
        "outcome": state.get("outcome"),
        "decision_id": state.get("decision_id"),
        "planned_workout_id": state.get("planned_workout_id"),
        "deterministic_cap": state.get("deterministic_cap"),
        "effective_cap": state.get("effective_cap"),
        "readiness_status": (state.get("context") or {}).get("readiness", {}).get("status"),
        "objective": objective or None,
        "workout": workout,
        "workout_minutes": None
        if not workout
        else round(sum(s["duration_seconds"] for s in workout["steps"]) / 60),
        "fallback_reason": state.get("fallback_reason"),
        "abort_reason": state.get("abort_reason"),
        "validation": state.get("validation"),
        "llm_calls": [
            {
                "node": c["node"],
                "accepted": c["accepted"],
                "attempt": c["attempt"],
                "model": c["reported_model"],
                "violations": c["violations"],
                "error": c["error"],
                "usage": c["usage"],
            }
            for c in state.get("calls", [])
        ],
    }


class DailyPlanRunner:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer: BaseCheckpointSaver[Any],
        llm: StructuredLLM | None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        nodes = CoachNodes(sessions, llm, settings.llm_max_corrections)
        self.graph: CompiledStateGraph[CoachState, Any, Any, Any] = build_graph(nodes).compile(
            checkpointer=checkpointer
        )

    async def default_plan_date(self, slug: str) -> dt.date:
        """Tomorrow in the athlete's timezone (the coach plans the next day)."""
        async with self._sessions() as session:
            athlete = await AthleteProfileRepository(session).get_by_slug(slug)
        tz = ZoneInfo(athlete.timezone if athlete else self._settings.app_timezone)
        return dt.datetime.now(tz).date() + dt.timedelta(days=1)

    async def run(
        self, slug: str, plan_date: dt.date | None = None, *, force: bool = False
    ) -> dict[str, Any]:
        plan_date = plan_date or await self.default_plan_date(slug)
        thread_id = thread_id_for(slug, plan_date, force=force)
        config: Any = {"configurable": {"thread_id": thread_id}}
        snapshot = await self.graph.aget_state(config)
        if snapshot.values and not snapshot.next:
            logger.info("coach_plan_exists", extra={"thread_id": thread_id})
            return summarize(snapshot.values, status="already_planned")
        if snapshot.next:
            logger.warning(
                "coach_plan_resume", extra={"thread_id": thread_id, "next": list(snapshot.next)}
            )
            final = await self.graph.ainvoke(None, config)
            return summarize(final, status="resumed")
        initial: CoachState = {
            "athlete_slug": slug,
            "plan_date": plan_date.isoformat(),
            "thread_id": thread_id,
            "run_id": uuid.uuid4().hex,
            "calls": [],
        }
        final = await self.graph.ainvoke(initial, config)
        return summarize(final, status="completed")


def make_llm(settings: Settings) -> StructuredLLM | None:
    try:
        return OpenAIStructuredLLM(settings)
    except LLMUnavailableError as exc:
        logger.error("llm_not_configured", extra={"error": str(exc)})
        return None


async def run_daily_plan(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    slug: str,
    plan_date: dt.date | None = None,
    *,
    force: bool = False,
    llm: StructuredLLM | None = None,
    conninfo: str | None = None,
) -> dict[str, Any]:
    llm = llm if llm is not None else make_llm(settings)
    async with AsyncPostgresSaver.from_conn_string(
        conninfo or settings.langgraph_conninfo()
    ) as saver:
        await saver.setup()  # idempotent; creates/migrates tables in schema langgraph
        runner = DailyPlanRunner(settings, sessions, saver, llm)
        try:
            return await runner.run(slug, plan_date, force=force)
        finally:
            if isinstance(llm, OpenAIStructuredLLM):
                await llm.aclose()
