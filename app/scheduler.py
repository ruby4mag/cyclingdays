"""Scheduler process (Phase 7): ``python -m app.scheduler``.

APScheduler 3 (``AsyncIOScheduler``) runs two jobs for the athlete:

* ``poll`` every ``SCHEDULER_POLL_MINUTES`` (default 30): sync, evaluate new
  rides, mark missed days, run the sync checks.
* ``daily_plan`` every day at ``SCHEDULER_PLAN_TIME`` (default 20:00, athlete
  timezone): poll, then plan and publish tomorrow.

Safety:

* Each job runs under a **PostgreSQL session advisory lock**
  (``pg_try_advisory_lock``), so even two scheduler processes never run the
  same job at once. A run that can't get the lock is skipped and logged.
* ``max_instances=1`` and ``coalesce=True``: missed runs collapse into one.
  A late daily plan still runs within ``misfire_grace_time`` (6 h).
* **Catch-up at start:** if the process starts after today's plan time, the
  daily plan runs once. It's idempotent: an already planned day returns
  ``already_planned`` without calling the LLM.
* Every job catches and logs its own errors. One failing run never stops
  the scheduler.
* **Notifications (Phase 9):** after each run, ``JobNotifier`` emails new,
  repeated and resolved problems (``app/alerts.py``) and, after the daily
  plan, a summary of what was published (``app/notifications.py``).
* **Heartbeat:** a ``heartbeat`` job writes ``HEARTBEAT_FILE`` every minute.
  The container healthcheck fails if it's older than 5 minutes, which the
  host watchdog (``deploy/watchdog``) reports.

Runs as the ``scheduler`` service in docker-compose. The API container
doesn't schedule anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import alerts
from app.alerts import AlertTracker
from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.logging_config import configure_logging
from app.notifications import LogNotifier, Notifier, make_notifier
from app.services.daily_planner import DailyPlanner

logger = logging.getLogger("app.scheduler")

LOCK_NAMESPACE = "scheduler"
DAILY_PLAN_MISFIRE_S = 6 * 3600
# The container healthcheck fails when this file is older than 5 minutes.
HEARTBEAT_FILE = Path("/tmp/cycledays-scheduler.heartbeat")  # noqa: S108 (container-private /tmp)
HEARTBEAT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class JobRun:
    """How one locked job run ended: ``ok``, ``locked`` (skipped) or ``failed``."""

    status: Literal["ok", "locked", "failed"]
    result: dict[str, Any] | None = None
    error: str | None = None


async def run_job(
    engine: AsyncEngine, job: str, func: Callable[[], Awaitable[dict[str, Any]]]
) -> JobRun:
    """Run ``func`` while holding a session-level advisory lock for ``job``."""
    async with engine.connect() as conn:
        got = await conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:ns), hashtext(:job))"),
            {"ns": LOCK_NAMESPACE, "job": job},
        )
        await conn.commit()
        if not got:
            logger.warning("scheduler_job_locked", extra={"job": job})
            return JobRun("locked")
        started = dt.datetime.now(dt.UTC)
        try:
            result = await func()
            logger.info(
                "scheduler_job_finished",
                extra={
                    "job": job,
                    "seconds": round((dt.datetime.now(dt.UTC) - started).total_seconds(), 1),
                    "summary": _summary(job, result),
                },
            )
            return JobRun("ok", result=result)
        except Exception as exc:
            logger.exception("scheduler_job_failed", extra={"job": job})
            return JobRun("failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:ns), hashtext(:job))"),
                {"ns": LOCK_NAMESPACE, "job": job},
            )
            await conn.commit()


async def run_locked(
    engine: AsyncEngine, job: str, func: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Like :func:`run_job`; returns the result, or None if skipped or failed."""
    return (await run_job(engine, job, func)).result


def _summary(job: str, result: dict[str, Any]) -> dict[str, Any]:
    if job == "daily_plan":
        publish = result.get("publish") or {}
        return {
            "plan_date": result.get("plan_date"),
            "status": result.get("status"),
            "outcome": result.get("outcome"),
            "publish": publish.get("status"),
        }
    activities = result.get("activities") or {}
    return {
        "sync_ok": (result.get("sync") or {}).get("ok"),
        "detected": activities.get("detected"),
        "processed": activities.get("processed"),
        "failed": activities.get("failed"),
        "missed": activities.get("missed"),
        "error": result.get("error"),
    }


def plan_time(settings: Settings) -> tuple[int, int]:
    hour, minute = settings.scheduler_plan_time.split(":")
    return int(hour), int(minute)


def write_heartbeat(path: Path = HEARTBEAT_FILE) -> None:
    """Touch the heartbeat file the container healthcheck reads."""
    try:
        path.write_text(dt.datetime.now(dt.UTC).isoformat())
    except OSError as exc:
        logger.warning("scheduler_heartbeat_failed", extra={"error": str(exc)})


class JobNotifier:
    """Turns job runs into emails (``app/alerts.py``); never raises."""

    def __init__(
        self,
        settings: Settings,
        notifier: Notifier,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self.tracker = AlertTracker(
            repeat=dt.timedelta(hours=settings.notify_repeat_hours),
            thresholds={"poll": settings.notify_poll_failure_threshold},
        )

    async def after_poll(self, run: JobRun) -> None:
        if run.status == "locked":
            return
        update = self.tracker.update("poll", alerts.poll_alerts(run.result, run.error), self._now())
        message = alerts.poll_message(update)
        if message is not None:
            await self._notifier.send(*message)

    async def after_daily_plan(self, run: JobRun) -> None:
        if run.status == "locked":
            return
        found = alerts.daily_plan_alerts(
            run.result, run.error, publish_enabled=self._settings.intervals_publish_enabled
        )
        update = self.tracker.update("daily_plan", found, self._now())
        message = alerts.daily_plan_message(
            run.result, update, daily_summary=self._settings.notify_daily_summary
        )
        if message is not None:
            await self._notifier.send(*message)


def build_scheduler(
    settings: Settings,
    engine: AsyncEngine,
    slug: str,
    planner: DailyPlanner,
    notifier: Notifier | None = None,
) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.app_timezone)
    hour, minute = plan_time(settings)
    scheduler = AsyncIOScheduler(timezone=tz)
    jobs = JobNotifier(settings, notifier or LogNotifier("no notifier configured"))

    async def poll() -> None:
        try:
            await jobs.after_poll(await run_job(engine, "poll", lambda: planner.poll(slug)))
        except Exception:
            logger.exception("scheduler_notify_failed", extra={"job": "poll"})

    async def daily_plan() -> None:
        try:
            run = await run_job(
                engine,
                "daily_plan",
                lambda: planner.plan_day(slug, publish=settings.intervals_publish_enabled),
            )
            await jobs.after_daily_plan(run)
        except Exception:
            logger.exception("scheduler_notify_failed", extra={"job": "daily_plan"})

    async def heartbeat() -> None:
        write_heartbeat()

    scheduler.add_job(
        poll,
        IntervalTrigger(minutes=settings.scheduler_poll_minutes, timezone=tz),
        id="poll",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.scheduler_poll_minutes * 60,
        next_run_time=dt.datetime.now(tz) + dt.timedelta(seconds=30),
    )
    scheduler.add_job(
        heartbeat,
        IntervalTrigger(seconds=HEARTBEAT_SECONDS, timezone=tz),
        id="heartbeat",
        max_instances=1,
        coalesce=True,
        next_run_time=dt.datetime.now(tz),
    )
    scheduler.add_job(
        daily_plan,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_plan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=DAILY_PLAN_MISFIRE_S,
    )
    now = dt.datetime.now(tz)
    if (now.hour, now.minute) >= (hour, minute):
        # Started after today's plan time: plan tomorrow once (idempotent).
        scheduler.add_job(
            daily_plan,
            id="daily_plan_catch_up",
            next_run_time=now + dt.timedelta(seconds=90),
            max_instances=1,
        )
    return scheduler


async def main() -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    # "Running job …" / "executed successfully" for every run, including the
    # minutely heartbeat; scheduler_job_finished/_failed already cover the jobs.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    slug = AthleteSeedSettings().slug
    engine = create_db_engine(settings)
    planner = DailyPlanner(settings, create_session_factory(engine))
    notifier = make_notifier(settings)
    if isinstance(notifier, LogNotifier):
        logger.error("notifications_disabled", extra={"reason": notifier.reason})
    scheduler = build_scheduler(settings, engine, slug, planner, notifier)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    logger.info(
        "scheduler_started",
        extra={
            "athlete": slug,
            "jobs": [{"id": j.id, "next_run": str(j.next_run_time)} for j in scheduler.get_jobs()],
        },
    )
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()
        logger.info("scheduler_stopped")


if __name__ == "__main__":
    asyncio.run(main())
