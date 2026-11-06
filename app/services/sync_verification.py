"""Verify the third-party synchronization paths around Intervals.icu (Phase 6).

CycleDays only talks to Intervals.icu. icTrainer (indoor trainer app) and
COROS (watch) connect to Intervals.icu through their own integrations, which
the athlete links in the apps. This module checks **what actually arrived**,
direction by direction, using only data visible through the Intervals.icu API:

=====================================  ==========================================
Check                                  Evidence
=====================================  ==========================================
``coros.link``                         athlete ``coros_*`` settings
``intervals_to_ictrainer.planned``     an icTrainer ride paired with a published
                                       CycleDays event and carrying its name
``ictrainer.erg_execution``            that ride: indoor, power, average power and
                                       duration close to the plan
``ictrainer_to_intervals.activities``  activity from the icTrainer OAuth client
``coros_to_intervals.activities``      activity with ``source = COROS``
``coros_to_intervals.wellness``        recent device wellness fields
``intervals_to_coros.workouts``        athlete ``coros_upload_workouts`` only
``activities.duplicates``              same ride recorded twice
=====================================  ==========================================

Some directions can't be observed through the API at all (whether icTrainer
*shows* the workout, or whether the COROS watch received it). Those can be
confirmed manually by the athlete (``status = confirmed``) and are never
reported as ``verified`` without data.

``evaluate`` is pure; :class:`SyncVerifier` loads the inputs from the database
and stores results in ``coach.sync_checks`` when they change.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Activity, AthleteProfile, DailyRecovery, SyncCheck
from app.database.repositories import (
    ActivityRepository,
    AthleteProfileRepository,
    DailyRecoveryRepository,
    PlannedWorkoutRepository,
)

logger = logging.getLogger(__name__)

VERIFICATION_VERSION = "v1"
ACTOR = "cycledays.sync_check"
ATHLETE_ACTOR = "athlete"

ICTRAINER_PATTERN = re.compile(r"ic\s*trainer", re.IGNORECASE)
# ERG execution: the ride must look like the plan was followed on the trainer.
ERG_POWER_TOLERANCE = 0.10  # actual average power within ±10 % of planned
ERG_MIN_DURATION_SHARE = 0.80  # at least 80 % of the planned duration
WELLNESS_STALE_DAYS = 3
# Wellness fields a device can deliver (subjective fields are typed in by the athlete).
DEVICE_WELLNESS_FIELDS = (
    "restingHR",
    "hrv",
    "hrvSDNN",
    "sleepSecs",
    "sleepScore",
    "sleepQuality",
    "avgSleepingHR",
    "steps",
    "spO2",
    "respiration",
    "readiness",
    "weight",
    "vo2max",
)
OTHER_WELLNESS_PROVIDERS = (
    "icu_garmin_download_wellness",
    "polar_download_wellness",
    "suunto_download_wellness",
)

CHECKS: dict[str, str] = {
    "coros.link": "COROS account linked to Intervals.icu",
    "intervals_to_ictrainer.planned": "Intervals.icu → icTrainer: planned workouts",
    "ictrainer.erg_execution": "icTrainer: ERG workout executed",
    "ictrainer_to_intervals.activities": "icTrainer → Intervals.icu: completed rides",
    "coros_to_intervals.activities": "COROS → Intervals.icu: activities",
    "coros_to_intervals.wellness": "COROS → Intervals.icu: wellness",
    "intervals_to_coros.workouts": "Intervals.icu → COROS: planned workouts",
    "activities.duplicates": "Duplicate recordings of one ride",
}
# Directions the API can't observe; the athlete may confirm them.
CONFIRMABLE = (
    "intervals_to_ictrainer.planned",
    "ictrainer.erg_execution",
    "intervals_to_coros.workouts",
)


# --------------------------------------------------------------------------
# Inputs and results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublishedRef:
    event_id: str
    workout_date: dt.date
    name: str
    planned_duration_s: int | None
    planned_avg_power_watts: float | None


@dataclass(frozen=True, slots=True)
class ActivityRef:
    id: int
    source_activity_id: str
    start_date_local: dt.date
    name: str | None
    activity_type: str | None
    origin: str | None
    origin_client: str | None
    device_name: str | None
    paired_event_id: str | None
    is_indoor: bool | None
    avg_power_watts: float | None
    moving_time_s: int | None
    duplicate_of_activity_id: int | None
    compliance: Any = None

    @property
    def from_ictrainer(self) -> bool:
        return any(
            v is not None and ICTRAINER_PATTERN.search(v)
            for v in (self.origin_client, self.device_name)
        )

    def brief(self) -> dict[str, Any]:
        return {
            "activity_id": self.source_activity_id,
            "date": self.start_date_local.isoformat(),
            "name": self.name,
            "type": self.activity_type,
            "source": self.origin,
            "oauth_client": self.origin_client,
            "device": self.device_name,
            "paired_event_id": self.paired_event_id,
        }


@dataclass(frozen=True, slots=True)
class WellnessRef:
    day: dt.date
    fields: frozenset[str]  # device wellness fields with a recorded value


@dataclass(frozen=True, slots=True)
class Confirmation:
    check_name: str
    note: str
    confirmed_at: dt.datetime


@dataclass(slots=True)
class CheckResult:
    check_name: str
    status: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return CHECKS[self.check_name]

    def as_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, **asdict(self)}


# --------------------------------------------------------------------------
# Evaluation (pure)
# --------------------------------------------------------------------------


def _truthy(snapshot: dict[str, Any], key: str) -> bool:
    return snapshot.get(key) is True


def _close(actual: float, planned: float, tolerance: float) -> bool:
    return planned > 0 and abs(actual - planned) <= tolerance * planned


def _with_confirmation(result: CheckResult, confirmations: dict[str, Confirmation]) -> CheckResult:
    """Data evidence wins; otherwise an athlete confirmation upgrades the status."""
    conf = confirmations.get(result.check_name)
    if conf is None or result.status in ("verified", "attention", "not_configured"):
        return result
    result.status = "confirmed"
    result.summary = f"confirmed by the athlete: {conf.note}".rstrip(": ")
    result.evidence["confirmation"] = {
        "note": conf.note,
        "confirmed_at": conf.confirmed_at.isoformat(),
    }
    return result


def evaluate(
    *,
    snapshot: dict[str, Any],
    activities: Sequence[ActivityRef],
    wellness: Sequence[WellnessRef],
    published: Sequence[PublishedRef],
    confirmations: Iterable[Confirmation] = (),
    today: dt.date,
) -> list[CheckResult]:
    """One result per entry in :data:`CHECKS`, in that order."""
    confirmed = {c.check_name: c for c in sorted(confirmations, key=lambda c: c.confirmed_at)}
    by_event = {p.event_id: p for p in published}
    coros_linked = bool(snapshot.get("coros_user_id"))
    origins = Counter(
        f"{a.origin or 'unknown'}" + (f" ({a.origin_client})" if a.origin_client else "")
        for a in activities
    )
    seen = {"activities_in_window": len(activities), "sources": dict(origins)}

    results: list[CheckResult] = []

    # --- COROS link ---------------------------------------------------------
    coros_flags = {
        "sync_activities": _truthy(snapshot, "coros_sync_activities"),
        "download_wellness": _truthy(snapshot, "coros_download_wellness"),
        "upload_workouts": _truthy(snapshot, "coros_upload_workouts"),
        "last_upload": snapshot.get("coros_last_upload"),
    }
    results.append(
        CheckResult(
            "coros.link",
            "configured" if coros_linked else "not_configured",
            "COROS is linked; settings: "
            + ", ".join(k for k, v in coros_flags.items() if v is True)
            if coros_linked
            else "no COROS account linked in Intervals.icu",
            coros_flags if coros_linked else {},
        )
    )

    # --- icTrainer ----------------------------------------------------------
    ictrainer = [a for a in activities if a.from_ictrainer]
    paired = [
        (a, by_event[a.paired_event_id])
        for a in ictrainer
        if a.paired_event_id is not None and a.paired_event_id in by_event
    ]
    named = [(a, p) for a, p in paired if (a.name or "").strip() == p.name]

    if named:
        a, _ = named[-1]
        planned_result = CheckResult(
            "intervals_to_ictrainer.planned",
            "verified",
            f"icTrainer ride {a.source_activity_id} carries the planned workout "
            f"'{a.name}' and is paired with CycleDays event {a.paired_event_id}",
            {"activity": a.brief()},
        )
    elif paired:
        a, plan = paired[-1]
        planned_result = CheckResult(
            "intervals_to_ictrainer.planned",
            "not_observed",
            "an icTrainer ride is paired with a CycleDays event, but under a different "
            "name, so it may not have come from the calendar; confirm manually",
            {"activity": a.brief(), "planned_name": plan.name},
        )
    else:
        planned_result = CheckResult(
            "intervals_to_ictrainer.planned",
            "not_observed",
            "not observable through the API until an icTrainer ride of a published "
            "workout is uploaded; confirm manually once icTrainer shows the workout",
            {"published_events": len(published)},
        )
    results.append(_with_confirmation(planned_result, confirmed))

    erg: CheckResult | None = None
    for a, plan in reversed(paired):
        evidence: dict[str, Any] = {
            "activity": a.brief(),
            "indoor": a.is_indoor,
            "avg_power_watts": a.avg_power_watts,
            "planned_avg_power_watts": plan.planned_avg_power_watts,
            "moving_time_s": a.moving_time_s,
            "planned_duration_s": plan.planned_duration_s,
            "intervals_compliance": a.compliance,
            "tolerance": {
                "power": ERG_POWER_TOLERANCE,
                "min_duration_share": ERG_MIN_DURATION_SHARE,
            },
        }
        problems: list[str] = []
        if a.is_indoor is not True:
            problems.append("not marked as an indoor/trainer ride")
        if a.avg_power_watts is None:
            problems.append("no power data")
        elif plan.planned_avg_power_watts is None or not _close(
            a.avg_power_watts, plan.planned_avg_power_watts, ERG_POWER_TOLERANCE
        ):
            problems.append(
                f"average power {a.avg_power_watts:.0f} W vs planned "
                f"{plan.planned_avg_power_watts or 0:.0f} W"
            )
        if (
            plan.planned_duration_s
            and (a.moving_time_s or 0) < ERG_MIN_DURATION_SHARE * plan.planned_duration_s
        ):
            problems.append(
                f"moving time {a.moving_time_s or 0} s vs planned {plan.planned_duration_s} s"
            )
        evidence["problems"] = problems
        erg = CheckResult(
            "ictrainer.erg_execution",
            "attention" if problems else "verified",
            "; ".join(problems)
            if problems
            else f"ride {a.source_activity_id} followed the plan on the trainer (average "
            f"power {a.avg_power_watts:.0f} W vs planned {plan.planned_avg_power_watts:.0f} W)",
            evidence,
        )
        break
    if erg is None:
        erg = CheckResult(
            "ictrainer.erg_execution",
            "not_observed",
            "no icTrainer ride paired with a published CycleDays workout yet",
        )
    results.append(_with_confirmation(erg, confirmed))

    if ictrainer:
        a = ictrainer[-1]
        results.append(
            CheckResult(
                "ictrainer_to_intervals.activities",
                "verified",
                f"{len(ictrainer)} icTrainer rides in the window; latest {a.source_activity_id} "
                f"on {a.start_date_local.isoformat()}",
                {"latest": a.brief(), **seen},
            )
        )
    else:
        results.append(
            CheckResult(
                "ictrainer_to_intervals.activities",
                "not_observed",
                "no activity from icTrainer in the window",
                seen,
            )
        )

    # --- COROS activities -----------------------------------------------------
    coros = [a for a in activities if (a.origin or "").upper() == "COROS"]
    if coros:
        a = coros[-1]
        results.append(
            CheckResult(
                "coros_to_intervals.activities",
                "verified",
                f"{len(coros)} COROS activities in the window; latest {a.source_activity_id} "
                f"on {a.start_date_local.isoformat()}",
                {"latest": a.brief(), **seen},
            )
        )
    elif not coros_linked or not coros_flags["sync_activities"]:
        results.append(
            CheckResult(
                "coros_to_intervals.activities",
                "not_configured",
                "COROS activity sync is not enabled in Intervals.icu",
                seen,
            )
        )
    else:
        results.append(
            CheckResult(
                "coros_to_intervals.activities",
                "not_observed",
                "COROS activity sync is enabled, but no COROS activity is in the window",
                seen,
            )
        )

    # --- COROS wellness -------------------------------------------------------
    field_days = Counter(f for w in wellness for f in w.fields)
    with_data = sorted((w.day for w in wellness if w.fields), reverse=True)
    others = [k for k in OTHER_WELLNESS_PROVIDERS if _truthy(snapshot, k)]
    w_evidence: dict[str, Any] = {
        "days_with_device_data": len(with_data),
        "latest_day": with_data[0].isoformat() if with_data else None,
        "fields": dict(sorted(field_days.items())),
        "fields_never_seen": [f for f in DEVICE_WELLNESS_FIELDS if f not in field_days],
        "other_wellness_providers_enabled": others,
        # Intervals.icu wellness records carry no source field.
        "attribution": "COROS is the only wellness provider enabled"
        if not others
        else "ambiguous: several providers deliver wellness",
    }
    if not coros_linked or not coros_flags["download_wellness"]:
        w_result = CheckResult(
            "coros_to_intervals.wellness",
            "not_configured",
            "COROS wellness download is not enabled in Intervals.icu",
            w_evidence,
        )
    elif not with_data:
        w_result = CheckResult(
            "coros_to_intervals.wellness",
            "not_observed",
            "COROS wellness download is enabled, but no device wellness is in the window",
            w_evidence,
        )
    elif (today - with_data[0]).days > WELLNESS_STALE_DAYS:
        w_result = CheckResult(
            "coros_to_intervals.wellness",
            "attention",
            f"last device wellness on {with_data[0].isoformat()}: more than "
            f"{WELLNESS_STALE_DAYS} days old",
            w_evidence,
        )
    else:
        w_result = CheckResult(
            "coros_to_intervals.wellness",
            "verified",
            f"device wellness on {len(with_data)} days, latest {with_data[0].isoformat()}; "
            "fields: " + ", ".join(sorted(field_days)),
            w_evidence,
        )
    results.append(w_result)

    # --- Intervals.icu → COROS workouts --------------------------------------
    upload = CheckResult(
        "intervals_to_coros.workouts",
        "configured" if coros_linked and coros_flags["upload_workouts"] else "not_configured",
        "Intervals.icu uploads planned workouts to COROS (not observable through the API)"
        if coros_linked and coros_flags["upload_workouts"]
        else "workout upload to COROS is off",
        {"last_upload": coros_flags["last_upload"]} if coros_linked else {},
    )
    results.append(_with_confirmation(upload, confirmed))

    # --- Duplicates -------------------------------------------------------------
    dups = [a for a in activities if a.duplicate_of_activity_id is not None]
    ids = {a.id: a for a in activities}
    if dups:
        results.append(
            CheckResult(
                "activities.duplicates",
                "attention",
                f"{len(dups)} rides were recorded twice; the duplicates are excluded from "
                "training load. Record each ride with one device, or turn off one upload",
                {
                    "pairs": [
                        {
                            "duplicate": d.brief(),
                            "kept": ids[d.duplicate_of_activity_id].brief()
                            if d.duplicate_of_activity_id in ids
                            else {"id": d.duplicate_of_activity_id},
                        }
                        for d in dups
                    ]
                },
            )
        )
    else:
        results.append(
            CheckResult(
                "activities.duplicates",
                "verified",
                "no duplicate recordings in the window",
                {"activities_in_window": len(activities)},
            )
        )
    return results


# --------------------------------------------------------------------------
# Service (database)
# --------------------------------------------------------------------------


def activity_ref(a: Activity) -> ActivityRef:
    return ActivityRef(
        id=a.id,
        source_activity_id=a.source_activity_id,
        start_date_local=a.start_date_local,
        name=a.name,
        activity_type=a.activity_type,
        origin=a.origin,
        origin_client=a.origin_client,
        device_name=a.device_name,
        paired_event_id=a.paired_event_id,
        is_indoor=a.is_indoor,
        avg_power_watts=a.avg_power_watts,
        moving_time_s=a.moving_time_s,
        duplicate_of_activity_id=a.duplicate_of_activity_id,
        compliance=(a.raw_payload or {}).get("compliance"),
    )


def wellness_ref(r: DailyRecovery) -> WellnessRef:
    raw = r.raw_payload or {}
    present = frozenset(
        f
        for f in DEVICE_WELLNESS_FIELDS
        if raw.get(f) not in (None, 0, "", False) and not isinstance(raw.get(f), bool)
    )
    return WellnessRef(day=r.recovery_date, fields=present)


@dataclass(slots=True)
class SyncReport:
    athlete_slug: str
    today: dt.date
    window_days: int
    checks: list[CheckResult]
    stored: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "athlete_slug": self.athlete_slug,
            "today": self.today.isoformat(),
            "window_days": self.window_days,
            "verification_version": VERIFICATION_VERSION,
            "stored_changes": self.stored,
            "checks": [c.as_dict() for c in self.checks],
        }


class UnknownCheckError(ValueError):
    pass


class SyncVerifier:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def _today(self, athlete: AthleteProfile) -> dt.date:
        return self._now().astimezone(ZoneInfo(athlete.timezone)).date()

    async def _athlete(self, session: AsyncSession, slug: str) -> AthleteProfile:
        athlete = await AthleteProfileRepository(session).get_by_slug(slug)
        if athlete is None:
            raise LookupError(f"athlete {slug!r} not found")
        return athlete

    async def confirm(self, slug: str, check_name: str, note: str) -> None:
        """Record the athlete's manual confirmation of a direction the API can't see."""
        if check_name not in CONFIRMABLE:
            raise UnknownCheckError(
                f"{check_name!r} can't be confirmed manually; choose one of {CONFIRMABLE}"
            )
        async with self._sessions() as session, session.begin():
            athlete = await self._athlete(session, slug)
            session.add(
                SyncCheck(
                    athlete_id=athlete.id,
                    check_name=check_name,
                    status="confirmed",
                    summary=note.strip() or "confirmed",
                    evidence={"note": note.strip()},
                    actor=ATHLETE_ACTOR,
                )
            )

    async def run(self, slug: str, *, window_days: int = 28) -> SyncReport:
        async with self._sessions() as session, session.begin():
            athlete = await self._athlete(session, slug)
            today = self._today(athlete)
            oldest = today - dt.timedelta(days=window_days)
            activities = await ActivityRepository(session).list_between(athlete.id, oldest, today)
            wellness = await DailyRecoveryRepository(session).list_between(
                athlete.id, oldest, today
            )
            planned = await PlannedWorkoutRepository(session).list_between(
                athlete.id, oldest, today + dt.timedelta(days=7)
            )
            published = [
                PublishedRef(
                    event_id=p.intervals_event_id,
                    workout_date=p.workout_date,
                    name=p.name,
                    planned_duration_s=p.planned_duration_s,
                    planned_avg_power_watts=(
                        (p.structure or {}).get("workout", {}).get("estimate", {})
                    ).get("average_power_watts"),
                )
                for p in planned
                if p.intervals_event_id
            ]
            history = await self._history(session, athlete.id)
            confirmations = [
                Confirmation(r.check_name, r.summary, r.created_at)
                for r in history
                if r.actor == ATHLETE_ACTOR
            ]
            checks = evaluate(
                snapshot=athlete.intervals_snapshot or {},
                activities=[activity_ref(a) for a in activities],
                wellness=[wellness_ref(w) for w in wellness],
                published=published,
                confirmations=confirmations,
                today=today,
            )
            latest: dict[str, SyncCheck] = {}
            for r in history:
                if r.actor == ACTOR:
                    latest[r.check_name] = r  # history is ordered oldest first
            stored = 0
            for c in checks:
                prev = latest.get(c.check_name)
                if prev and (prev.status, prev.summary, prev.evidence) == (
                    c.status,
                    c.summary,
                    c.evidence,
                ):
                    continue
                session.add(
                    SyncCheck(
                        athlete_id=athlete.id,
                        check_name=c.check_name,
                        status=c.status,
                        summary=c.summary,
                        evidence=c.evidence,
                        actor=ACTOR,
                    )
                )
                stored += 1
        report = SyncReport(athlete.slug, today, window_days, checks, stored)
        logger.info(
            "sync_verification_finished",
            extra={
                "statuses": {c.check_name: c.status for c in checks},
                "stored_changes": stored,
            },
        )
        return report

    @staticmethod
    async def _history(session: AsyncSession, athlete_id: int) -> list[SyncCheck]:
        result = await session.execute(
            select(SyncCheck)
            .where(SyncCheck.athlete_id == athlete_id)
            .order_by(SyncCheck.created_at, SyncCheck.id)
        )
        return list(result.scalars())
