"""Scheduler: job configuration, per-athlete runs, notifications (no database)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from app import scheduler as sched
from app.scheduler import (
    CoachedAthlete,
    CoachScheduler,
    JobNotifier,
    JobRun,
    build_scheduler,
    plan_due,
    plan_time,
    write_heartbeat,
)
from tests.conftest import make_unit_settings


def test_plan_time_parsing_and_validation() -> None:
    assert plan_time(make_unit_settings(scheduler_plan_time="05:45")) == (5, 45)
    with pytest.raises(ValueError):
        make_unit_settings(scheduler_plan_time="25:00")


def test_jobs_are_configured_safely() -> None:
    settings = make_unit_settings(scheduler_poll_minutes=15)
    scheduler = build_scheduler(settings, cast(Any, None), cast(Any, None))
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert set(jobs) == {"poll", "daily_plan", "onboarding", "heartbeat"}
    assert jobs["poll"].trigger.interval.total_seconds() == 15 * 60
    assert jobs["daily_plan"].trigger.interval.total_seconds() == 60
    assert jobs["onboarding"].trigger.interval.total_seconds() == 120
    for job in jobs.values():
        assert job.max_instances == 1 and job.coalesce is True


def test_heartbeat_job_is_scheduled(tmp_path: Path) -> None:
    scheduler = build_scheduler(make_unit_settings(), cast(Any, None), cast(Any, None))
    job = {j.id: j for j in scheduler.get_jobs()}["heartbeat"]
    assert job.trigger.interval.total_seconds() == 60
    target = tmp_path / "hb"
    write_heartbeat(target)
    assert target.read_text().startswith("20")
    write_heartbeat(tmp_path / "missing-dir" / "hb")  # logged, not raised


# --- plan_due: each athlete's own timezone --------------------------------------


def test_plan_due_uses_the_athletes_timezone_once_per_day() -> None:
    at = dt.datetime(2026, 11, 6, 14, 45, tzinfo=dt.UTC)  # 20:15 in Kolkata, 15:45 in Paris
    assert plan_due(at, "Asia/Kolkata", (20, 0), None) == dt.date(2026, 11, 6)
    assert plan_due(at, "Europe/Paris", (20, 0), None) is None
    assert plan_due(at, "Asia/Kolkata", (20, 0), dt.date(2026, 11, 6)) is None  # done today
    assert plan_due(at, "Asia/Kolkata", (20, 0), dt.date(2026, 11, 5)) == dt.date(2026, 11, 6)
    assert plan_due(at, "Not/AZone", (20, 0), None) is None


# --- CoachScheduler: per-athlete work -------------------------------------------


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str] | None]] = []

    async def send(self, subject: str, body: str, to: Sequence[str] | None = None) -> bool:
        self.sent.append((subject, body, None if to is None else list(to)))
        return True


class FakePlanner:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail or set()

    async def poll(self, slug: str) -> dict[str, Any]:
        self.calls.append(("poll", slug))
        if slug in self.fail:
            raise RuntimeError("boom")
        return {"sync": {"results": {}}}

    async def plan_day(self, slug: str, *, publish: bool = True) -> dict[str, Any]:
        self.calls.append(("plan", slug))
        if slug in self.fail:
            raise RuntimeError("boom")
        return {
            "status": "completed",
            "plan_date": "2026-11-07",
            "outcome": "rest",
            "publish": {"status": "rest_day"},
        }


async def _fake_run_job(
    engine: Any, job: str, func: Callable[[], Awaitable[dict[str, Any]]]
) -> JobRun:
    try:
        return JobRun("ok", result=await func())
    except Exception as exc:
        return JobRun("failed", error=f"{type(exc).__name__}: {exc}")


ATHLETES = [
    CoachedAthlete("anna", "Asia/Kolkata", never_synced=False, has_plan=True, ever_planned=True),
    CoachedAthlete("ben", "Europe/Paris", never_synced=False, has_plan=True, ever_planned=True),
    CoachedAthlete("cara", "Asia/Kolkata", never_synced=True, has_plan=False, ever_planned=False),
    CoachedAthlete("dev", "Asia/Kolkata", never_synced=True, has_plan=True, ever_planned=False),
]


def _coach(
    planner: FakePlanner,
    notifier: RecordingNotifier,
    now: list[dt.datetime],
    athletes: list[CoachedAthlete] = ATHLETES,
) -> CoachScheduler:
    async def listing() -> list[CoachedAthlete]:
        return athletes

    async def recipients(slug: str) -> list[str]:
        return [f"{slug}@example.com"] if slug != "ben" else []

    return CoachScheduler(
        make_unit_settings(),
        cast(Any, None),
        cast(Any, planner),
        notifier,
        athletes=listing,
        recipients=recipients,
        now=lambda: now[0],
    )


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sched, "run_job", _fake_run_job)


async def test_poll_runs_every_connected_athlete_and_isolates_failures() -> None:
    planner, out = FakePlanner(fail={"ben"}), RecordingNotifier()
    now = [dt.datetime(2026, 11, 6, 6, tzinfo=dt.UTC)]
    coach = _coach(planner, out, now)
    await coach.poll_all()
    assert sorted(slug for kind, slug in planner.calls) == ["anna", "ben", "cara", "dev"]
    assert out.sent == []  # first failure: below the threshold
    await coach.poll_all()
    assert [s for s, _, _ in out.sent] == ["ben: Problem: The poll job crashed"]
    assert out.sent[0][2] is None  # problems go to the operator


async def test_daily_plan_runs_per_timezone_once_and_skips_athletes_without_goal() -> None:
    planner, out = FakePlanner(), RecordingNotifier()
    now = [dt.datetime(2026, 11, 6, 14, 45, tzinfo=dt.UTC)]  # 20:15 Kolkata, 15:45 Paris
    coach = _coach(planner, out, now)
    await coach.plan_tick()
    assert sorted(planner.calls) == [("plan", "anna"), ("plan", "dev")]  # cara: no plan
    await coach.plan_tick()  # same local day: nothing new
    assert len(planner.calls) == 2
    now[0] = dt.datetime(2026, 11, 6, 19, 5, tzinfo=dt.UTC)  # 20:05 in Paris
    await coach.plan_tick()
    assert planner.calls[-1] == ("plan", "ben")
    # The athlete summary goes to the athlete's own address (ben has none).
    summaries = [(s, to) for s, _, to in out.sent if to is not None]
    assert sorted(to[0] for _, to in summaries) == ["anna@example.com", "dev@example.com"]
    assert summaries[0][0] == "2026-11-07: rest day (on your calendar)"


async def test_onboarding_syncs_new_athletes_then_plans_once_a_goal_is_set() -> None:
    planner, out = FakePlanner(), RecordingNotifier()
    now = [dt.datetime(2026, 11, 6, 4, tzinfo=dt.UTC)]
    coach = _coach(planner, out, now)
    await coach.onboard()
    assert sorted(planner.calls) == [("plan", "dev"), ("poll", "cara")]
    await coach.onboard()  # retried only after an hour
    assert len(planner.calls) == 2
    now[0] += dt.timedelta(hours=1, minutes=1)
    await coach.onboard()
    assert len(planner.calls) == 4


# --- JobNotifier ------------------------------------------------------------------


async def test_job_notifier_poll_needs_two_failures_then_resolves() -> None:
    clock = [dt.datetime(2026, 11, 6, 10, tzinfo=dt.UTC)]
    out = RecordingNotifier()
    jobs = JobNotifier(make_unit_settings(), out, now=lambda: clock[0])
    down = JobRun("ok", result={"error": "IntervalsConnectionError: timeout"})
    await jobs.after_poll("anna", down)
    await jobs.after_poll("ben", down)  # another athlete: its own count
    assert out.sent == []
    await jobs.after_poll("anna", JobRun("locked"))  # skipped runs don't count either way
    clock[0] += dt.timedelta(minutes=30)
    await jobs.after_poll("anna", down)
    assert [s for s, _, _ in out.sent] == ["anna: Problem: Intervals.icu poll failed"]
    await jobs.after_poll("anna", JobRun("ok", result={"sync": {"results": {}}}))
    assert out.sent[-1][0] == "anna: Resolved: Intervals.icu poll failed"


async def test_job_notifier_daily_plan_problem_to_operator_summary_to_athlete() -> None:
    out = RecordingNotifier()

    async def recipients(slug: str) -> list[str]:
        return ["anna@example.com"]

    jobs = JobNotifier(make_unit_settings(), out, recipients=recipients)
    await jobs.after_daily_plan("anna", JobRun("failed", error="RuntimeError: boom"))
    assert out.sent[0][0].startswith("anna: Problem: The daily plan job crashed")
    assert out.sent[0][2] is None and len(out.sent) == 1  # nothing for the athlete
    ok = {
        "status": "completed",
        "plan_date": "2026-11-07",
        "outcome": "rest",
        "publish": {"status": "rest_day"},
    }
    await jobs.after_daily_plan("anna", JobRun("ok", result=ok))
    subjects = [(s, to) for s, _, to in out.sent[1:]]
    assert subjects == [
        ("anna: Resolved: The daily plan job crashed: tomorrow's workout may be missing", None),
        ("2026-11-07: rest day (on your calendar)", ["anna@example.com"]),
    ]
    # A re-run of an unchanged day sends nothing.
    again = {**ok, "status": "already_planned", "publish": {"status": "unchanged"}}
    await jobs.after_daily_plan("anna", JobRun("ok", result=again))
    assert len(out.sent) == 3


async def test_daily_summary_off_sends_only_problems() -> None:
    out = RecordingNotifier()

    async def recipients(slug: str) -> list[str]:
        return ["anna@example.com"]

    jobs = JobNotifier(make_unit_settings(notify_daily_summary=False), out, recipients=recipients)
    ok = {
        "status": "completed",
        "plan_date": "2026-11-07",
        "outcome": "rest",
        "publish": {"status": "rest_day"},
    }
    await jobs.after_daily_plan("anna", JobRun("ok", result=ok))
    assert out.sent == []
