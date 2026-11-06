"""Phase 7: scheduler job configuration (no database, jobs not executed)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, cast

import pytest

from app.scheduler import JobNotifier, JobRun, build_scheduler, plan_time, write_heartbeat
from tests.conftest import make_unit_settings


def test_plan_time_parsing_and_validation() -> None:
    assert plan_time(make_unit_settings(scheduler_plan_time="05:45")) == (5, 45)
    with pytest.raises(ValueError):
        make_unit_settings(scheduler_plan_time="25:00")


def test_jobs_are_configured_safely() -> None:
    settings = make_unit_settings(scheduler_poll_minutes=15, scheduler_plan_time="00:00")
    scheduler = build_scheduler(settings, cast(Any, None), "primary", cast(Any, None))
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert {"poll", "daily_plan"} <= set(jobs)
    assert jobs["poll"].trigger.interval.total_seconds() == 15 * 60
    for job in (jobs["poll"], jobs["daily_plan"]):
        assert job.max_instances == 1 and job.coalesce is True
    assert str(jobs["daily_plan"].trigger.timezone) == settings.app_timezone
    # Plan time 00:00 has always passed: a catch-up run is scheduled.
    assert "daily_plan_catch_up" in jobs


# --- Phase 9: heartbeat and notifications ------------------------------------


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


def test_heartbeat_job_is_scheduled(tmp_path: Path) -> None:
    scheduler = build_scheduler(make_unit_settings(), cast(Any, None), "primary", cast(Any, None))
    job = {j.id: j for j in scheduler.get_jobs()}["heartbeat"]
    assert job.trigger.interval.total_seconds() == 60
    target = tmp_path / "hb"
    write_heartbeat(target)
    assert target.read_text().startswith("20")
    write_heartbeat(tmp_path / "missing-dir" / "hb")  # logged, not raised


async def test_job_notifier_poll_needs_two_failures_then_resolves() -> None:
    clock = [dt.datetime(2026, 11, 6, 10, tzinfo=dt.UTC)]
    out = RecordingNotifier()
    jobs = JobNotifier(make_unit_settings(), out, now=lambda: clock[0])
    down = JobRun("ok", result={"error": "IntervalsConnectionError: timeout"})
    await jobs.after_poll(down)
    assert out.sent == []
    await jobs.after_poll(JobRun("locked"))  # skipped runs don't count either way
    clock[0] += dt.timedelta(minutes=30)
    await jobs.after_poll(down)
    assert [s for s, _ in out.sent] == ["Problem: Intervals.icu poll failed"]
    await jobs.after_poll(JobRun("ok", result={"sync": {"results": {}}}))
    assert out.sent[-1][0] == "Resolved: Intervals.icu poll failed"


async def test_job_notifier_daily_plan_summary_and_crash() -> None:
    out = RecordingNotifier()
    jobs = JobNotifier(make_unit_settings(), out)
    await jobs.after_daily_plan(JobRun("failed", error="RuntimeError: boom"))
    assert out.sent[0][0].startswith("Problem: The daily plan job crashed")
    ok = {
        "status": "completed",
        "plan_date": "2026-11-07",
        "outcome": "rest",
        "publish": {"status": "rest_day"},
    }
    await jobs.after_daily_plan(JobRun("ok", result=ok))
    subject, body = out.sent[1]
    assert subject == "2026-11-07: rest day (published)" and "Resolved:" in body
