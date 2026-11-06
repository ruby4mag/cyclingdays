"""Phase 9: alert rules, the alert tracker and email texts (pure, no I/O)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.alerts import (
    Alert,
    AlertTracker,
    daily_plan_alerts,
    daily_plan_message,
    poll_alerts,
    poll_message,
)

T0 = dt.datetime(2026, 11, 6, 14, 30, tzinfo=dt.UTC)


def ok_poll() -> dict[str, Any]:
    return {
        "sync": {"ok": True, "results": {"wellness": {"status": "ok", "error": None}}},
        "activities": {"status": "ok", "failed": 0},
    }


def plan_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "completed",
        "plan_date": "2026-11-07",
        "outcome": "planned",
        "readiness_status": "green",
        "effective_cap": "endurance",
        "objective": {"session_type": "long_endurance", "primary_goal": "aerobic base"},
        "workout": {
            "name": "90-minute steady endurance",
            "estimate": {"tss": 62.4, "intensity_factor": 0.66},
        },
        "workout_minutes": 90,
        "publish": {"status": "published", "errors": [], "warnings": []},
    }
    result.update(overrides)
    return result


# --- rules ------------------------------------------------------------------


def test_poll_rules() -> None:
    assert poll_alerts(ok_poll()) == []
    assert poll_alerts(None) == []  # locked: skipped run
    assert [a.key for a in poll_alerts(None, "RuntimeError: boom")] == ["poll.exception"]
    bad = ok_poll()
    bad["sync"]["results"]["wellness"] = {"status": "failed", "error": "HTTP 500"}
    bad["activities"] = {"status": "ok", "failed": 2}
    alerts = poll_alerts(bad)
    assert [(a.key, a.severity) for a in alerts] == [
        ("poll.sync.wellness", "error"),
        ("poll.activities", "warning"),
    ]
    assert "HTTP 500" in alerts[0].detail
    down = {"error": "IntervalsConnectionError: timed out"}
    assert [a.key for a in poll_alerts(down)] == ["poll.intervals"]


def test_daily_plan_rules() -> None:
    assert daily_plan_alerts(plan_result()) == []
    for status in ("unchanged", "rest_day", "removed"):
        assert daily_plan_alerts(plan_result(publish={"status": status})) == []
    blocked = plan_result(
        publish={"status": "blocked", "errors": ["another workout is planned"], "warnings": []}
    )
    [alert] = daily_plan_alerts(blocked)
    assert alert.key == "daily_plan.publish" and alert.severity == "error"
    assert "another workout is planned" in alert.detail
    fallback = daily_plan_alerts(plan_result(outcome="fallback", fallback_reason="LLM timeout"))
    assert [(a.key, a.severity) for a in fallback] == [("daily_plan.fallback", "warning")]
    aborted = daily_plan_alerts(plan_result(outcome="aborted", abort_reason="no FTP", publish=None))
    assert [a.key for a in aborted] == ["daily_plan.aborted"]  # no extra publish alert
    # Publishing switched off: a missing publish result is fine.
    assert daily_plan_alerts(plan_result(publish=None), publish_enabled=False) == []
    assert [a.key for a in daily_plan_alerts(plan_result(publish=None))] == ["daily_plan.publish"]
    assert [a.key for a in daily_plan_alerts(None, "KeyError: x")] == ["daily_plan.exception"]


# --- tracker ----------------------------------------------------------------


def test_tracker_threshold_repeat_and_resolve() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12), thresholds={"poll": 2})
    down = [Alert("poll.intervals", "error", "Intervals.icu poll failed")]

    first = tracker.update("poll", down, T0)
    assert not first.anything  # one blip stays quiet
    second = tracker.update("poll", down, T0 + dt.timedelta(minutes=30))
    assert [a.key for a in second.new] == ["poll.intervals"]
    third = tracker.update("poll", down, T0 + dt.timedelta(hours=1))
    assert not third.anything and [a.key for a in third.ongoing] == ["poll.intervals"]
    later = tracker.update("poll", down, T0 + dt.timedelta(hours=12, minutes=30))
    assert [a.key for a in later.repeated] == ["poll.intervals"]
    fixed = tracker.update("poll", [], T0 + dt.timedelta(hours=13))
    assert [a.key for a in fixed.resolved] == ["poll.intervals"]
    assert not tracker.update("poll", [], T0 + dt.timedelta(hours=14)).anything


def test_tracker_never_reports_unnotified_problems_as_resolved() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12), thresholds={"poll": 2})
    blip = [Alert("poll.intervals", "error", "x")]
    tracker.update("poll", blip, T0)
    assert not tracker.update("poll", [], T0 + dt.timedelta(minutes=30)).anything
    # ... and the count starts again after a clean run.
    assert not tracker.update("poll", blip, T0 + dt.timedelta(hours=1)).anything


def test_scopes_are_independent() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12))
    a = Alert("daily_plan.publish", "error", "not published")
    assert tracker.update("daily_plan", [a], T0).new == [a]
    assert not tracker.update("poll", [], T0).anything  # doesn't resolve another scope


# --- messages -----------------------------------------------------------------


def test_poll_message() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12))
    assert poll_message(tracker.update("poll", [], T0)) is None
    down = [Alert("poll.intervals", "error", "Intervals.icu poll failed", "timeout")]
    subject, body = poll_message(tracker.update("poll", down, T0)) or ("", "")
    assert subject == "Problem: Intervals.icu poll failed"
    assert "timeout" in body and "OPERATIONS.md" in body
    subject, body = poll_message(tracker.update("poll", [], T0)) or ("", "")
    assert subject == "Resolved: Intervals.icu poll failed"


def test_daily_summary_when_all_is_well() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12))
    update = tracker.update("daily_plan", [], T0)
    subject, body = daily_plan_message(plan_result(), update, daily_summary=True) or ("", "")
    assert subject == "2026-11-07: 90-minute steady endurance (published)"
    assert "90 min, TSS 62, IF 0.66" in body and "Readiness:  green" in body
    assert daily_plan_message(plan_result(), update, daily_summary=False) is None


def test_rest_day_summary() -> None:
    update = AlertTracker(repeat=dt.timedelta(hours=12)).update("daily_plan", [], T0)
    result = plan_result(outcome="rest", workout=None, publish={"status": "rest_day"})
    subject, _ = daily_plan_message(result, update, daily_summary=True) or ("", "")
    assert subject == "2026-11-07: rest day (published)"


def test_rerun_that_changed_nothing_is_quiet() -> None:
    update = AlertTracker(repeat=dt.timedelta(hours=12)).update("daily_plan", [], T0)
    rerun = plan_result(status="already_planned", publish={"status": "unchanged"})
    assert daily_plan_message(rerun, update, daily_summary=True) is None


def test_problem_email_includes_the_plan() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12))
    result = plan_result(publish={"status": "failed", "errors": ["HTTP 503"], "warnings": []})
    update = tracker.update("daily_plan", daily_plan_alerts(result), T0)
    subject, body = daily_plan_message(result, update, daily_summary=True) or ("", "")
    assert subject.startswith("Problem: Tomorrow's workout was not published (status: failed)")
    assert "HTTP 503" in body and "Tomorrow's plan:" in body
    # The next day's success resolves it, in the summary email.
    update = tracker.update("daily_plan", [], T0 + dt.timedelta(days=1))
    subject, body = daily_plan_message(plan_result(), update, daily_summary=True) or ("", "")
    assert "(published)" in subject and "Resolved:" in body


def test_crash_email_without_result() -> None:
    tracker = AlertTracker(repeat=dt.timedelta(hours=12))
    update = tracker.update("daily_plan", daily_plan_alerts(None, "RuntimeError: db"), T0)
    subject, body = daily_plan_message(None, update, daily_summary=True) or ("", "")
    assert subject.startswith("Problem: The daily plan job crashed")
    assert "RuntimeError: db" in body
