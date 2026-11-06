"""Activity evaluation loop (Phase 7).

Each ``run(athlete)``:

1. **Detect.** Synced rides from the last ``lookback_days`` days are checked;
   duplicates (Phase 6) are skipped. Each ride gets a *revision*: a hash of
   the fields the evaluation uses (duration, power, HR, RPE, feel, paired
   event). A ride without an evaluation for its current revision is put in
   the event inbox (``coach.webhook_events``). The dedup key is
   ``activity:<id>:<revision>``, so detecting the same ride twice is a no-op,
   while a later edit (e.g. RPE entered after the ride) is evaluated again.
2. **Process** pending events one at a time (``FOR UPDATE SKIP LOCKED``):
   1. Find the matching planned workout: first by Intervals.icu
      ``paired_event_id``; otherwise the coach's workout on that date, if
      no other ride claimed it already.
   2. Load the power and HR streams. Without them, the activity totals are
      used.
   3. Evaluate deterministically (``app/training/evaluation.py``). An
      optional LLM review adds a narrative and a lower-only recommendation.
   4. Upsert ``workout_performance`` (one row per activity).
   5. Mark the planned workout ``completed``.
   6. Mark the event ``processed``. Failures are retried up to
      ``MAX_ATTEMPTS`` times.
3. **Missed.** Coach workouts on earlier days without a ride become
   ``missed``. They are never moved to another day. If a late upload
   arrives, the evaluation turns the day back to ``completed``.

A per-athlete advisory lock held for the whole run keeps runs from
overlapping.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.llm import StructuredLLM
from app.agents.performance_evaluator import review_performance
from app.config import Settings
from app.database.models import Activity, AthleteProfile, CoachDecision, PlannedWorkout
from app.database.repositories import (
    ActivityRepository,
    AthleteProfileRepository,
    DailyRecoveryRepository,
    EventInboxRepository,
    PlannedWorkoutRepository,
    WorkoutPerformanceRepository,
    try_advisory_xact_lock,
)
from app.graph.prompts import PERFORMANCE_REVIEW
from app.integrations.intervals_client import IntervalsClient, IntervalsError
from app.training.activity_dedup import sport_family
from app.training.evaluation import (
    EVALUATION_VERSION,
    evaluate_ride,
    summary_numbers,
    to_seconds_series,
)

logger = logging.getLogger(__name__)

SOURCE = "intervals_icu"
EVENT_TYPE = "activity.evaluate"
LOCK_NAMESPACE = "activity_processing"
MAX_ATTEMPTS = 5
REVISION_FIELDS = (
    "start_time",
    "moving_time_s",
    "avg_power_watts",
    "normalized_power_watts",
    "avg_hr_bpm",
    "rpe",
    "feel",
    "paired_event_id",
)


def activity_revision(activity: Activity) -> str:
    data = {f: getattr(activity, f) for f in REVISION_FIELDS}
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ProcessReport:
    athlete_slug: str
    status: str = "ok"  # ok | locked | failed
    detected: int = 0
    processed: int = 0
    failed: int = 0
    missed: list[str] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "locked") and self.failed == 0


class ActivityProcessor:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        client: IntervalsClient | None,
        llm: StructuredLLM | None = None,
        *,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._client = client
        self._llm = llm
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    async def run(self, slug: str, *, lookback_days: int = 14) -> ProcessReport:
        report = ProcessReport(slug)
        async with self._sessions() as lock_session, lock_session.begin():
            athlete = await AthleteProfileRepository(lock_session).get_by_slug(slug)
            if athlete is None:
                report.status, report.error = "failed", f"athlete {slug!r} not found"
                return report
            if not await try_advisory_xact_lock(lock_session, LOCK_NAMESPACE, athlete.id):
                report.status = "locked"
                return report
            today = self._now().astimezone(ZoneInfo(athlete.timezone)).date()
            report.detected = await self._detect(athlete, today, lookback_days)
            await self._process_pending(athlete, report)
            report.missed = await self._mark_missed(athlete, today)
        logger.info("activity_processing_finished", extra={"report": report.as_dict()})
        return report

    # --- 1. detect -----------------------------------------------------------

    async def _detect(self, athlete: AthleteProfile, today: dt.date, lookback_days: int) -> int:
        async with self._sessions() as session, session.begin():
            activities = await ActivityRepository(session).list_between(
                athlete.id, today - dt.timedelta(days=lookback_days), today
            )
            candidates = [a for a in activities if a.duplicate_of_activity_id is None]
            done = await WorkoutPerformanceRepository(session).revisions(a.id for a in candidates)
            inbox = EventInboxRepository(session)
            new = 0
            for a in candidates:
                rev = activity_revision(a)
                if done.get(a.id) == rev:
                    continue
                if await inbox.enqueue(
                    SOURCE,
                    f"activity:{a.source_activity_id}:{rev}",
                    EVENT_TYPE,
                    {
                        "athlete_id": athlete.id,
                        "activity_id": a.id,
                        "source_activity_id": a.source_activity_id,
                        "revision": rev,
                    },
                ):
                    new += 1
            return new

    # --- 2. process ----------------------------------------------------------

    async def _process_pending(self, athlete: AthleteProfile, report: ProcessReport) -> None:
        async with self._sessions() as session, session.begin():
            await EventInboxRepository(session).reset_stale(SOURCE, EVENT_TYPE, athlete.id)
        tried: set[int] = set()
        while True:
            async with self._sessions() as session, session.begin():
                event = await EventInboxRepository(session).claim(
                    SOURCE, EVENT_TYPE, athlete.id, MAX_ATTEMPTS, exclude=tried
                )
                if event is None:
                    return
                event_id, payload = event.id, dict(event.payload)
            tried.add(event_id)
            try:
                summary = await self._evaluate(athlete, payload)
            except Exception as exc:  # recorded and retried; never stops the loop
                logger.exception("activity_evaluation_failed", extra={"event_id": event_id})
                async with self._sessions() as session, session.begin():
                    await EventInboxRepository(session).finish(event_id, "failed", str(exc)[:500])
                report.failed += 1
                continue
            async with self._sessions() as session, session.begin():
                await EventInboxRepository(session).finish(
                    event_id, "processed" if summary else "ignored"
                )
            if summary:
                report.processed += 1
                report.evaluations.append(summary)

    async def _evaluate(
        self, athlete: AthleteProfile, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._sessions() as session:
            activity = await session.get(Activity, payload["activity_id"])
            if activity is None or activity.duplicate_of_activity_id is not None:
                return None  # deleted, or turned out to be a duplicate recording
            if activity_revision(activity) != payload["revision"]:
                return None  # superseded by a newer revision (its own event)
            planned = await self._match_planned(session, athlete, activity)
            planned_id = None if planned is None else planned.id
            planned_workout = None if planned is None else planned.structure.get("workout")
            wellness_rows = await DailyRecoveryRepository(session).list_between(
                athlete.id, activity.start_date_local, activity.start_date_local
            )
        wellness = None
        if wellness_rows:
            w = wellness_rows[0]
            wellness = {"fatigue": w.subjective_fatigue, "soreness": w.subjective_soreness}

        power, heartrate, stream_note = await self._streams(activity)
        evaluation = evaluate_ride(
            planned=planned_workout,
            activity={
                "moving_time_s": activity.moving_time_s,
                "avg_power_watts": activity.avg_power_watts,
                "normalized_power_watts": activity.normalized_power_watts,
                "avg_hr_bpm": activity.avg_hr_bpm,
                "rpe": activity.rpe,
                "feel": activity.feel,
            },
            power=power,
            heartrate=heartrate,
            wellness=wellness,
            ftp_watts=activity.ftp_at_activity_watts or athlete.ftp_watts,
        )
        if stream_note:
            evaluation["notes"].append(stream_note)
        evaluation["activity"] = {
            "source_activity_id": activity.source_activity_id,
            "date": activity.start_date_local.isoformat(),
            "name": activity.name,
            "type": activity.activity_type,
            "source": activity.origin,
            "intervals_compliance": (activity.raw_payload or {}).get("compliance"),
            "intervals_decoupling": (activity.raw_payload or {}).get("decoupling"),
        }
        review, decision = await self._review(athlete, activity, evaluation)
        evaluation["review"] = review
        numbers = summary_numbers(evaluation)

        async with self._sessions() as session, session.begin():
            await WorkoutPerformanceRepository(session).save(
                {
                    "athlete_id": athlete.id,
                    "activity_id": activity.id,
                    "planned_workout_id": planned_id,
                    "session_date": activity.start_date_local,
                    "completion_pct": numbers["completion_pct"],
                    "power_deviation_pct": numbers["power_deviation_pct"],
                    "decoupling_pct": numbers["decoupling_pct"],
                    "rpe": numbers["rpe"],
                    "outcome": evaluation["outcome"],
                    "hr_response": evaluation.get("heart_rate") or {},
                    "step_compliance": {"steps": (evaluation.get("power") or {}).get("steps", [])},
                    "evaluation": evaluation,
                    "calculation_version": EVALUATION_VERSION,
                    "activity_revision": payload["revision"],
                }
            )
            if planned_id is not None:
                row = await session.get(PlannedWorkout, planned_id)
                if row is not None and row.status in ("validated", "published", "missed"):
                    row.status = "completed"
            if decision is not None:
                session.add(decision)
        return {
            "activity": activity.source_activity_id,
            "date": activity.start_date_local.isoformat(),
            "planned_workout_id": planned_id,
            "outcome": evaluation["outcome"],
            **numbers,
            "recommendation": None if review is None else review.get("recommendation"),
        }

    async def _match_planned(
        self, session: AsyncSession, athlete: AthleteProfile, activity: Activity
    ) -> PlannedWorkout | None:
        if sport_family(activity.activity_type) != "cycling":
            return None
        repo = PlannedWorkoutRepository(session)
        candidate = None
        if activity.paired_event_id:
            candidate = await repo.get_by_event_id(athlete.id, activity.paired_event_id)
        if candidate is None:
            candidate = await repo.get_for_day(athlete.id, activity.start_date_local)
        if candidate is None or candidate.workout_type == "rest":
            return None
        if candidate.status not in ("validated", "published", "missed", "completed"):
            return None
        other = await WorkoutPerformanceRepository(session).for_planned(candidate.id)
        if other is not None and other.activity_id != activity.id:
            return None  # a second ride that day: evaluated as unplanned
        return candidate

    async def _streams(
        self, activity: Activity
    ) -> tuple[list[float | None] | None, list[float | None] | None, str | None]:
        if self._client is None:
            return None, None, "streams not loaded (no Intervals.icu client)"
        if activity.avg_power_watts is None and activity.avg_hr_bpm is None:
            return None, None, None
        try:
            streams = await self._client.get_activity_streams(activity.source_activity_id)
        except IntervalsError as exc:
            logger.warning(
                "activity_streams_unavailable",
                extra={"activity": activity.source_activity_id, "error": str(exc)},
            )
            return None, None, f"streams unavailable: {type(exc).__name__}"
        time = streams.get("time")
        return (
            to_seconds_series(streams.get("watts"), time),
            to_seconds_series(streams.get("heartrate"), time),
            None,
        )

    async def _review(
        self, athlete: AthleteProfile, activity: Activity, evaluation: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, CoachDecision | None]:
        if self._llm is None:
            return None, None
        payload = {
            "athlete": {
                "ftp_watts": athlete.ftp_watts,
                "primary_objective": athlete.primary_objective,
                "experience_level": athlete.experience_level,
            },
            "evaluation": {k: v for k, v in evaluation.items() if k != "review"},
        }
        outcome = await review_performance(
            self._llm, payload, max_corrections=self._settings.llm_max_corrections
        )
        last = outcome.calls[-1] if outcome.calls else None
        decision = CoachDecision(
            athlete_id=athlete.id,
            decision_date=activity.start_date_local,
            decision_type="performance_review",
            model_name=None if last is None else (last.reported_model or last.configured_model),
            prompt_version=PERFORMANCE_REVIEW.id,
            input_snapshot={"payload": payload, "prompt_sha256": PERFORMANCE_REVIEW.sha256},
            output={
                "review": None if outcome.parsed is None else outcome.parsed.model_dump(),
                "accepted": outcome.ok,
                "unavailable": outcome.unavailable,
                "rejected": outcome.rejected,
                "calls": [
                    {
                        k: c.as_dict()[k]
                        for k in ("attempt", "accepted", "violations", "error", "usage")
                    }
                    for c in outcome.calls
                ],
            },
            rationale=None if outcome.parsed is None else outcome.parsed.summary,
        )
        if outcome.parsed is None:
            return None, decision
        return outcome.parsed.model_dump(), decision

    # --- 3. missed -----------------------------------------------------------

    async def _mark_missed(self, athlete: AthleteProfile, today: dt.date) -> list[str]:
        missed: list[str] = []
        async with self._sessions() as session, session.begin():
            perf = WorkoutPerformanceRepository(session)
            for row in await PlannedWorkoutRepository(session).list_due_before(athlete.id, today):
                if await perf.for_planned(row.id) is not None:
                    row.status = "completed"
                    continue
                row.status = "missed"
                missed.append(row.workout_date.isoformat())
        if missed:
            logger.info("planned_workouts_missed", extra={"dates": missed})
        return missed
