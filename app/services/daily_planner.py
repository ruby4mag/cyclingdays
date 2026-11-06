"""The daily coaching loop (Phase 7): poll, evaluate, plan, publish.

Two entry points, used by the CLIs and the scheduler (``app/scheduler.py``):

``poll(athlete)``: new data in, evaluations out.
    1. Incremental Intervals.icu sync (activities, wellness, calendar).
    2. Activity processing: detect new or changed rides, evaluate them
       against the plan, mark missed days (``activity_processor.py``).
    3. Synchronization checks (Phase 6).

``plan_day(athlete, day)``: the next workout. Daily planning steps 1-8:
    1. Fetch the latest data: ``poll``, unless ``sync=False``.
    2-3. Training load and recovery: the deterministic metrics and readiness
       in the graph context.
    4. Review the longer-term plan. Its horizon is extended if needed;
       ``guidance`` (prescription plus adaptation from recent evaluations)
       sets the day's limits.
    5-7. Select, generate and validate the workout: the LangGraph workflow.
    8. Publish automatically (``workout_publisher.py``), with the Phase 5
       safety gates.

Every step is idempotent, so the scheduler can repeat any of them safely.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.llm import OpenAIStructuredLLM, StructuredLLM
from app.config import Settings
from app.database.repositories import AthleteProfileRepository, TrainingPlanRepository
from app.graph.workflow import make_llm, run_daily_plan
from app.integrations.intervals_client import IntervalsClient, IntervalsError
from app.services.activity_processor import ActivityProcessor
from app.services.intervals_sync import IntervalsSyncService
from app.services.sync_verification import SyncVerifier
from app.services.workout_publisher import WorkoutPublisher
from app.training.periodization import PlanPhases
from app.training.plan_builder import extend_plan

logger = logging.getLogger(__name__)


class DailyPlanner:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        *,
        client_factory: Callable[[], IntervalsClient] | None = None,
        llm: StructuredLLM | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._client_factory = client_factory or (lambda: IntervalsClient.from_settings(settings))
        self._llm = llm
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    async def _today(self, slug: str) -> dt.date:
        async with self._sessions() as session:
            athlete = await AthleteProfileRepository(session).get_by_slug(slug)
        tz = ZoneInfo(athlete.timezone if athlete else self._settings.app_timezone)
        return self._now().astimezone(tz).date()

    # --- poll ----------------------------------------------------------------

    async def poll(self, slug: str) -> dict[str, Any]:
        out: dict[str, Any] = {"athlete_slug": slug}
        llm = self._llm if self._llm is not None else make_llm(self._settings)
        try:
            async with self._client_factory() as client:
                sync = await IntervalsSyncService(
                    self._sessions, client, self._settings, now=self._now
                ).run(slug)
                out["sync"] = {
                    "ok": sync.ok,
                    "results": {
                        r.resource: {"status": r.status, "error": r.error} for r in sync.results
                    },
                }
                processed = await ActivityProcessor(
                    self._settings, self._sessions, client, llm, now=self._now
                ).run(slug, lookback_days=self._settings.activity_lookback_days)
                out["activities"] = processed.as_dict()
        except IntervalsError as exc:
            logger.error("poll_failed", extra={"error": str(exc)})
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        finally:
            if self._llm is None and isinstance(llm, OpenAIStructuredLLM):
                await llm.aclose()
        checks = await SyncVerifier(self._sessions, now=self._now).run(slug)
        out["sync_checks"] = {c.check_name: c.status for c in checks.checks}
        return out

    # --- long-term plan ------------------------------------------------------

    async def ensure_plan_horizon(self, slug: str, until: dt.date) -> dict[str, Any] | None:
        """Extend an open-ended active plan so it covers ``until``."""
        async with self._sessions() as session, session.begin():
            athlete = await AthleteProfileRepository(session).get_by_slug(slug)
            if athlete is None:
                return None
            row = await TrainingPlanRepository(session).get_active(athlete.id)
            if row is None:
                return None
            try:
                plan = PlanPhases.from_json(row.phases)
            except ValidationError:
                return {"plan": row.name, "extended": False, "error": "invalid plan"}
            extended = extend_plan(plan, until=until)
            if extended is None:
                return {"plan": row.name, "extended": False}
            row.phases = extended.model_dump(mode="json")
            row.end_date = max(p.end_date for p in extended.phases)
            logger.info(
                "training_plan_extended",
                extra={"plan_id": row.id, "end_date": row.end_date.isoformat()},
            )
            return {"plan": row.name, "extended": True, "end_date": row.end_date.isoformat()}

    # --- plan ----------------------------------------------------------------

    async def plan_day(
        self,
        slug: str,
        day: dt.date | None = None,
        *,
        force: bool = False,
        publish: bool = True,
        sync: bool = True,
    ) -> dict[str, Any]:
        poll = await self.poll(slug) if sync else None
        today = await self._today(slug)
        day = day or today + dt.timedelta(days=1)
        horizon = await self.ensure_plan_horizon(
            slug, day + dt.timedelta(days=self._settings.plan_horizon_days)
        )
        result = await run_daily_plan(
            self._settings, self._sessions, slug, day, force=force, llm=self._llm
        )
        result["poll"] = poll
        result["plan_horizon"] = horizon
        result["publish"] = None
        if publish and result.get("outcome") != "aborted" and result.get("plan_date"):
            try:
                async with self._client_factory() as client:
                    published = await WorkoutPublisher(
                        self._settings, self._sessions, client
                    ).publish(slug, dt.date.fromisoformat(result["plan_date"]))
                result["publish"] = published.as_dict()
            except IntervalsError as exc:
                result["publish"] = {"ok": False, "status": "failed", "error": str(exc)}
        return result
