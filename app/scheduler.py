"""Scheduler process (Phase 7, multi-athlete in Phase 10): ``python -m app.scheduler``.

APScheduler 3 (``AsyncIOScheduler``) coaches every athlete with an active
Intervals.icu connection (``coach.intervals_connections``):

* ``poll`` every ``SCHEDULER_POLL_MINUTES`` (default 30): for each athlete,
  sync, evaluate new rides, mark missed days, run the sync checks.
* ``daily_plan`` checks every minute. Once an athlete's local time (their
  own timezone) passes ``SCHEDULER_PLAN_TIME`` (default 20:00), it polls,
  then plans and publishes tomorrow for that athlete, once per local day.
  Athletes without a long-term plan (goal) are skipped.
* ``onboarding`` every 2 minutes: a newly connected athlete gets their
  first sync, and once they've set a goal, their first plan, without
  waiting for the next poll or 20:00.

Safety:

* Each athlete's job runs under a **PostgreSQL session advisory lock**
  (``pg_try_advisory_lock``) named for the job and athlete, so two scheduler
  processes never run the same athlete's job at once. A run that can't get
  the lock is skipped and logged. One athlete's failure never affects
  another's.
* At most ``SCHEDULER_MAX_PARALLEL`` athlete runs at once (LLM and
  Intervals.icu load).
* **Catch-up at start:** an athlete whose plan time has passed today is
  planned once at start. It's idempotent: an already planned day returns
  ``already_planned`` without calling the LLM.
* Every job catches and logs its own errors. One failing run never stops
  the scheduler.
* **Notifications (Phase 9/10):** problems go to the operator
  (``NOTIFY_EMAIL_TO``), per athlete. The daily summary goes to the
  athlete's own email (``app_users.email``), if they want it.
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
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import alerts
from app.alerts import AlertTracker
from app.config import Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.logging_config import configure_logging
from app.notifications import LogNotifier, Notifier, make_notifier
from app.services.daily_planner import DailyPlanner

logger = logging.getLogger("app.scheduler")

LOCK_NAMESPACE = "scheduler"
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


RecipientLookup = Callable[[str], Awaitable[list[str]]]


async def _no_recipients(_slug: str) -> list[str]:
    return []


class JobNotifier:
    """Turns job runs into emails (``app/alerts.py``); never raises.

    Problems go to the operator, prefixed with the athlete. The daily summary
    goes to the athlete's own address (``recipients(slug)``).
    """

    def __init__(
        self,
        settings: Settings,
        notifier: Notifier,
        now: Callable[[], dt.datetime] | None = None,
        recipients: RecipientLookup | None = None,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._recipients = recipients or _no_recipients
        self.tracker = AlertTracker(
            repeat=dt.timedelta(hours=settings.notify_repeat_hours),
            thresholds={"poll": settings.notify_poll_failure_threshold},
        )

    async def after_poll(self, slug: str, run: JobRun) -> None:
        if run.status == "locked":
            return
        update = self.tracker.update(
            f"poll:{slug}", alerts.poll_alerts(run.result, run.error), self._now()
        )
        message = alerts.poll_message(update)
        if message is not None:
            await self._notifier.send(f"{slug}: {message[0]}", message[1])

    async def after_daily_plan(self, slug: str, run: JobRun) -> None:
        if run.status == "locked":
            return
        found = alerts.daily_plan_alerts(
            run.result, run.error, publish_enabled=self._settings.intervals_publish_enabled
        )
        update = self.tracker.update(f"daily_plan:{slug}", found, self._now())
        problem = alerts.daily_plan_message(run.result, update, daily_summary=False)
        if problem is not None:
            await self._notifier.send(f"{slug}: {problem[0]}", problem[1])
        if not self._settings.notify_daily_summary:
            return
        summary = alerts.athlete_summary_message(run.result)
        if summary is None:
            return
        to = await self._recipients(slug)
        if to:
            await self._notifier.send(*summary, to=to)


# --------------------------------------------------------------------------
# Athletes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoachedAthlete:
    """An athlete with an active Intervals.icu connection."""

    slug: str
    timezone: str
    never_synced: bool
    has_plan: bool  # an active long-term plan (the goal is set)
    ever_planned: bool  # at least one daily_plan decision


_ATHLETES_SQL = text(
    """
    SELECT a.slug, a.timezone, a.intervals_synced_at IS NULL AS never_synced,
           EXISTS (SELECT 1 FROM coach.training_plans p
                   WHERE p.athlete_id = a.id AND p.status = 'active') AS has_plan,
           EXISTS (SELECT 1 FROM coach.coach_decisions d
                   WHERE d.athlete_id = a.id AND d.decision_type = 'daily_plan') AS ever_planned
    FROM coach.athlete_profile a
    JOIN coach.intervals_connections c ON c.athlete_id = a.id AND c.status = 'active'
    ORDER BY a.id
    """
)

_RECIPIENTS_SQL = text(
    """
    SELECT u.email FROM coach.app_users u
    JOIN coach.athlete_profile a ON a.id = u.athlete_id
    WHERE a.slug = :slug AND u.is_active AND u.notify_daily_summary AND u.email IS NOT NULL
    ORDER BY u.id
    """
)


async def coached_athletes(engine: AsyncEngine) -> list[CoachedAthlete]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_ATHLETES_SQL)).all()
    return [CoachedAthlete(*row) for row in rows]


async def summary_recipients(engine: AsyncEngine, slug: str) -> list[str]:
    async with engine.connect() as conn:
        return [str(e) for e in (await conn.scalars(_RECIPIENTS_SQL, {"slug": slug})).all()]


def plan_due(
    now: dt.datetime, timezone: str, plan_at: tuple[int, int], last_attempt: dt.date | None
) -> dt.date | None:
    """The athlete's local date if their daily plan is due now, else None.

    Due once per local day, any time after the plan time (so a scheduler that
    starts late still plans that evening).
    """
    try:
        local = now.astimezone(ZoneInfo(timezone))
    except (ValueError, KeyError):
        return None
    if (local.hour, local.minute) < plan_at or last_attempt == local.date():
        return None
    return local.date()


ONBOARDING_RETRY = dt.timedelta(hours=1)


class CoachScheduler:
    """The per-athlete work behind the APScheduler jobs."""

    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        planner: DailyPlanner,
        notifier: Notifier | None = None,
        *,
        athletes: Callable[[], Awaitable[list[CoachedAthlete]]] | None = None,
        recipients: RecipientLookup | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._planner = planner
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._athletes = athletes or (lambda: coached_athletes(engine))
        self.jobs = JobNotifier(
            settings,
            notifier or LogNotifier("no notifier configured"),
            now=self._now,
            recipients=recipients or (lambda slug: summary_recipients(engine, slug)),
        )
        self._slots = asyncio.Semaphore(settings.scheduler_max_parallel)
        self._plan_at = plan_time(settings)
        self.last_plan_attempt: dict[str, dt.date] = {}
        self.onboarding_attempts: dict[str, dt.datetime] = {}

    async def _list(self) -> list[CoachedAthlete]:
        try:
            return await self._athletes()
        except Exception:
            logger.exception("scheduler_athletes_failed")
            return []

    async def _each(self, work: list[tuple[str, Callable[[str], Awaitable[None]]]]) -> None:
        """Run ``job(slug)`` for each pair, at most SCHEDULER_MAX_PARALLEL at once."""

        async def one(slug: str, job: Callable[[str], Awaitable[None]]) -> None:
            async with self._slots:
                try:
                    await job(slug)
                except Exception:
                    logger.exception("scheduler_athlete_failed", extra={"athlete": slug})

        await asyncio.gather(*(one(slug, job) for slug, job in work))

    async def poll_one(self, slug: str) -> None:
        run = await run_job(self._engine, f"poll:{slug}", lambda: self._planner.poll(slug))
        await self.jobs.after_poll(slug, run)

    async def plan_one(self, slug: str) -> None:
        run = await run_job(
            self._engine,
            f"daily_plan:{slug}",
            lambda: self._planner.plan_day(slug, publish=self._settings.intervals_publish_enabled),
        )
        await self.jobs.after_daily_plan(slug, run)

    async def poll_all(self) -> None:
        await self._each([(a.slug, self.poll_one) for a in await self._list()])

    async def plan_tick(self) -> None:
        """Plan tomorrow for every athlete whose plan time has passed today."""
        now = self._now()
        due = []
        for athlete in await self._list():
            if not athlete.has_plan:
                continue
            day = plan_due(
                now, athlete.timezone, self._plan_at, self.last_plan_attempt.get(athlete.slug)
            )
            if day is not None:
                self.last_plan_attempt[athlete.slug] = day  # once a day, even if it fails
                due.append(athlete)
        if due:
            logger.info("daily_plan_due", extra={"athletes": [a.slug for a in due]})
            await self._each([(a.slug, self.plan_one) for a in due])

    async def onboard(self) -> None:
        """First sync and first plan for new athletes (retried hourly on failure)."""
        now = self._now()
        todo: list[tuple[str, Callable[[str], Awaitable[None]]]] = []
        for athlete in await self._list():
            last = self.onboarding_attempts.get(athlete.slug)
            if last is not None and now - last < ONBOARDING_RETRY:
                continue
            if athlete.has_plan and not athlete.ever_planned:
                step, job = "plan", self.plan_one  # syncs first, then plans tomorrow
            elif athlete.never_synced:
                step, job = "sync", self.poll_one
            else:
                continue
            self.onboarding_attempts[athlete.slug] = now
            logger.info("athlete_onboarding", extra={"athlete": athlete.slug, "step": step})
            todo.append((athlete.slug, job))
        await self._each(todo)


def build_scheduler(
    settings: Settings,
    engine: AsyncEngine,
    planner: DailyPlanner,
    notifier: Notifier | None = None,
    *,
    coach: CoachScheduler | None = None,
) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.app_timezone)
    scheduler = AsyncIOScheduler(timezone=tz)
    coach = coach or CoachScheduler(settings, engine, planner, notifier)
    scheduler.coach = coach  # for tests and introspection

    async def heartbeat() -> None:
        write_heartbeat()

    now = dt.datetime.now(tz)
    scheduler.add_job(
        coach.poll_all,
        IntervalTrigger(minutes=settings.scheduler_poll_minutes, timezone=tz),
        id="poll",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.scheduler_poll_minutes * 60,
        next_run_time=now + dt.timedelta(seconds=30),
    )
    scheduler.add_job(
        coach.plan_tick,
        IntervalTrigger(minutes=1, timezone=tz),
        id="daily_plan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        # The first check doubles as the catch-up for a late start.
        next_run_time=now + dt.timedelta(seconds=90),
    )
    scheduler.add_job(
        coach.onboard,
        IntervalTrigger(minutes=2, timezone=tz),
        id="onboarding",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=now + dt.timedelta(seconds=60),
    )
    scheduler.add_job(
        heartbeat,
        IntervalTrigger(seconds=HEARTBEAT_SECONDS, timezone=tz),
        id="heartbeat",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
    )
    return scheduler


async def main() -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    # "Running job …" / "executed successfully" for every run, including the
    # minutely heartbeat; scheduler_job_finished/_failed already cover the jobs.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    engine = create_db_engine(settings)
    planner = DailyPlanner(settings, create_session_factory(engine))
    notifier = make_notifier(settings)
    if isinstance(notifier, LogNotifier):
        logger.error("notifications_disabled", extra={"reason": notifier.reason})
    scheduler = build_scheduler(settings, engine, planner, notifier)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    try:
        athletes: list[str] | str = [a.slug for a in await coached_athletes(engine)]
    except Exception as exc:  # the jobs retry; don't die over a log line
        athletes = f"unavailable: {type(exc).__name__}"
    logger.info(
        "scheduler_started",
        extra={
            "athletes": athletes,
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
