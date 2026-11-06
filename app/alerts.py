"""What the scheduler emails about, and when (Phase 9).

Pure functions plus a small in-memory tracker; no I/O.

* ``poll_alerts`` / ``daily_plan_alerts`` turn a job's result (or its
  exception) into :class:`Alert` objects. An alert's ``key`` identifies the
  problem, not the occurrence, so the same problem on the next run has the
  same key.
* :class:`AlertTracker` decides what to send. A problem is emailed once it
  has been seen ``threshold`` runs in a row, then again every
  ``repeat`` while it lasts. When it disappears, a "resolved" line is sent.
  State lives in memory: after a restart, a lasting problem is emailed once
  more, which is acceptable for a restart.
* ``poll_message`` / ``daily_plan_message`` build at most one email per job
  run. The daily one doubles as the daily summary ("tomorrow: …,
  published"), so a missing summary email also means something is wrong.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning"]

# Publish results that mean the calendar is right for that day.
PUBLISH_OK = frozenset({"published", "unchanged", "rest_day", "removed"})


@dataclass(frozen=True, slots=True)
class Alert:
    key: str
    severity: Severity
    title: str
    detail: str = ""

    def line(self) -> str:
        label = "ERROR" if self.severity == "error" else "WARNING"
        return f"[{label}] {self.title}" + (f"\n    {self.detail}" if self.detail else "")


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def poll_alerts(result: Mapping[str, Any] | None, error: str | None = None) -> list[Alert]:
    if error is not None:
        return [Alert("poll.exception", "error", "The poll job crashed", error)]
    if result is None:  # skipped: another run held the lock
        return []
    alerts: list[Alert] = []
    if result.get("error"):
        alerts.append(
            Alert("poll.intervals", "error", "Intervals.icu poll failed", str(result["error"]))
        )
    sync = (result.get("sync") or {}).get("results") or {}
    for resource, r in sorted(sync.items()):
        if (r or {}).get("status") == "failed":
            alerts.append(
                Alert(
                    f"poll.sync.{resource}",
                    "error",
                    f"Intervals.icu sync failed: {resource}",
                    str(r.get("error") or ""),
                )
            )
    activities = result.get("activities") or {}
    if activities.get("status") == "failed":
        alerts.append(
            Alert(
                "poll.activities",
                "error",
                "Ride evaluation failed",
                str(activities.get("error") or ""),
            )
        )
    elif activities.get("failed"):
        alerts.append(
            Alert(
                "poll.activities",
                "warning",
                f"{activities['failed']} ride evaluation(s) failed and will be retried",
            )
        )
    return alerts


def daily_plan_alerts(
    result: Mapping[str, Any] | None,
    error: str | None = None,
    *,
    publish_enabled: bool = True,
) -> list[Alert]:
    if error is not None:
        return [
            Alert(
                "daily_plan.exception",
                "error",
                "The daily plan job crashed: tomorrow's workout may be missing",
                error,
            )
        ]
    if result is None:
        return []
    alerts: list[Alert] = []
    outcome = result.get("outcome")
    if outcome == "aborted":
        alerts.append(
            Alert(
                "daily_plan.aborted",
                "error",
                "Planning was aborted: no workout for tomorrow",
                str(result.get("abort_reason") or ""),
            )
        )
    elif outcome == "fallback":
        alerts.append(
            Alert(
                "daily_plan.fallback",
                "warning",
                "The AI coach failed; a conservative fallback workout was planned",
                str(result.get("fallback_reason") or ""),
            )
        )
    if publish_enabled and outcome != "aborted":
        publish = result.get("publish")
        status = None if publish is None else publish.get("status")
        if status not in PUBLISH_OK:
            reasons = (
                []
                if publish is None
                else [*publish.get("errors", []), *publish.get("warnings", [])]
            )
            alerts.append(
                Alert(
                    "daily_plan.publish",
                    "error" if status != "skipped" else "warning",
                    f"Tomorrow's workout was not published (status: {status or 'none'})",
                    "; ".join(str(r) for r in reasons),
                )
            )
    return alerts


# --------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Open:
    alert: Alert
    count: int
    notified_at: dt.datetime | None = None


@dataclass(slots=True)
class AlertUpdate:
    new: list[Alert] = field(default_factory=list)
    repeated: list[Alert] = field(default_factory=list)
    ongoing: list[Alert] = field(default_factory=list)  # open, already notified, not due
    resolved: list[Alert] = field(default_factory=list)

    @property
    def to_send(self) -> list[Alert]:
        return [*self.new, *self.repeated]

    @property
    def anything(self) -> bool:
        return bool(self.new or self.repeated or self.resolved)


class AlertTracker:
    def __init__(
        self, *, repeat: dt.timedelta, thresholds: Mapping[str, int] | None = None
    ) -> None:
        self._repeat = repeat
        self._thresholds = dict(thresholds or {})
        self._open: dict[str, dict[str, _Open]] = {}

    def update(self, scope: str, alerts: Sequence[Alert], now: dt.datetime) -> AlertUpdate:
        """Record one run of ``scope`` (a job) and say what to notify."""
        threshold = self._thresholds.get(scope, 1)
        current = self._open.setdefault(scope, {})
        out = AlertUpdate()
        seen = {a.key for a in alerts}
        for key in [k for k in current if k not in seen]:
            gone = current.pop(key)
            if gone.notified_at is not None:
                out.resolved.append(gone.alert)
        for alert in alerts:
            entry = current.get(alert.key)
            if entry is None:
                entry = current[alert.key] = _Open(alert, 0)
            entry.alert = alert  # latest detail
            entry.count += 1
            if entry.count < threshold:
                continue
            if entry.notified_at is None:
                entry.notified_at = now
                out.new.append(alert)
            elif now - entry.notified_at >= self._repeat:
                entry.notified_at = now
                out.repeated.append(alert)
            else:
                out.ongoing.append(alert)
        return out


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


def _problem_subject(alerts: Sequence[Alert]) -> str:
    first = alerts[0]
    more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
    label = "Problem" if any(a.severity == "error" for a in alerts) else "Warning"
    return f"{label}: {first.title}{more}"


def _problem_lines(update: AlertUpdate) -> list[str]:
    lines: list[str] = []
    if update.new:
        lines += ["New problems:", *(a.line() for a in update.new), ""]
    if update.repeated:
        lines += ["Still unresolved:", *(a.line() for a in update.repeated), ""]
    if update.resolved:
        lines += ["Resolved:", *(f"[OK] {a.title}" for a in update.resolved), ""]
    return lines


FOOTER = "Logs: podman logs --since 2h cycling-coach_scheduler_1. See docs/OPERATIONS.md."


def poll_message(update: AlertUpdate) -> tuple[str, str] | None:
    if not update.anything:
        return None
    subject = (
        _problem_subject(update.to_send)
        if update.to_send
        else f"Resolved: {update.resolved[0].title}"
    )
    return subject, "\n".join([*_problem_lines(update), FOOTER])


def _plan_lines(result: Mapping[str, Any]) -> tuple[str, list[str]]:
    """A one-line headline and detail lines for the planned day."""
    workout = result.get("workout") or {}
    objective = result.get("objective") or {}
    outcome = result.get("outcome")
    if outcome == "rest":
        headline = "rest day"
    elif workout:
        headline = str(workout.get("name") or objective.get("session_type") or "workout")
    else:
        headline = "no workout"
    lines = [f"Date:       {result.get('plan_date') or '?'}", f"Workout:    {headline}"]
    if workout:
        estimate = workout.get("estimate") or {}
        minutes = result.get("workout_minutes")
        parts = [
            f"{minutes} min" if minutes is not None else None,
            f"TSS {estimate['tss']:.0f}" if estimate.get("tss") is not None else None,
            f"IF {estimate['intensity_factor']:.2f}"
            if estimate.get("intensity_factor") is not None
            else None,
        ]
        lines.append("Load:       " + ", ".join(p for p in parts if p))
    if objective.get("session_type"):
        lines.append(f"Session:    {objective['session_type']}")
    if objective.get("primary_goal"):
        lines.append(f"Goal:       {objective['primary_goal']}")
    lines.append(
        f"Readiness:  {result.get('readiness_status') or '?'}"
        f" (cap: {result.get('effective_cap') or '?'})"
    )
    lines.append(f"Planning:   {result.get('status')} / {outcome}")
    publish = result.get("publish") or {}
    lines.append(f"Publishing: {publish.get('status') or 'not run'}")
    return headline, lines


def daily_plan_message(
    result: Mapping[str, Any] | None,
    update: AlertUpdate,
    *,
    daily_summary: bool,
) -> tuple[str, str] | None:
    """The email after a daily-plan run, or None if there's nothing to say.

    A re-run that changed nothing (already planned, calendar unchanged) sends
    no summary, so a scheduler restart after the plan time stays quiet.
    """
    publish_status = ((result or {}).get("publish") or {}).get("status")
    repeat_run = (
        result is not None
        and result.get("status") == "already_planned"
        and publish_status in {"unchanged", "rest_day", None}
    )
    want_summary = daily_summary and result is not None and not repeat_run
    if not update.anything and not want_summary:
        return None
    lines = _problem_lines(update)
    if result is not None:
        headline, plan = _plan_lines(result)
        lines += ["Tomorrow's plan:", *plan, ""]
    else:
        headline = "no result"
    if update.to_send:
        subject = _problem_subject(update.to_send)
    elif update.resolved and not want_summary:
        subject = f"Resolved: {update.resolved[0].title}"
    else:
        state = "published" if publish_status in PUBLISH_OK else (publish_status or "planned")
        subject = f"{(result or {}).get('plan_date')}: {headline} ({state})"
    return subject, "\n".join([*lines, FOOTER])
