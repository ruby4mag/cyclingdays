"""What the long-term plan allows on one day (Phase 7).

The plan's week (``periodization``), the weekday's role in the template, the
load already done this week, and the adaptation from recent performance
(``adaptation.py``) give a :class:`DayPrescription`:

* the **most demanding session type** allowed (a ladder: recovery <
  endurance = long endurance < tempo < sweet spot < threshold < VO2max);
* the **TSS budget** of the day: ``target_tss`` and the hard limit
  ``max_tss``;
* the **time limit** ``max_minutes``.

The prescription only ever *restricts*. Readiness, the validator and the LLM
work inside it.

**No cramming.** Each day's budget is its share of the week's target
(``ROLE_SHARE``), plus ``DAY_TOLERANCE``. A missed day's budget is never
moved to another day, and the week is capped at ``WEEK_TOLERANCE`` x its
target. The lost load is simply not made up.

Pure functions; no I/O.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.training.intensity import Intensity
from app.training.periodization import WEEKDAYS, DayRole, PlanPhases
from app.training.sessions import SESSION_INTENSITY, SessionType

PROGRESSION_VERSION = "progression/v1"

ROLE_SHARE: dict[str, float] = {
    "rest": 0.0,
    "recovery": 0.3,
    "endurance": 1.0,
    "key": 1.4,
    "long": 1.8,
}
DAY_TOLERANCE = 1.25  # a day may use up to 125 % of its budget
WEEK_TOLERANCE = 1.10  # the week may end at most 10 % above its target
MIN_SESSION_TSS = 25.0  # any training day can at least hold an easy spin
MIN_SESSION_MINUTES = 30
LONG_MIN_MINUTES = 120

# Ladder of session types; equal rank = same demand.
SESSION_RANK: dict[str, int] = {
    "rest": 0,
    "recovery": 1,
    "endurance": 2,
    "long_endurance": 2,
    "tempo": 3,
    "sweet_spot": 4,
    "threshold": 5,
    "vo2max": 6,
}
LADDER: tuple[SessionType, ...] = (
    "rest",
    "recovery",
    "endurance",
    "tempo",
    "sweet_spot",
    "threshold",
    "vo2max",
)
ROLE_MAX_SESSION: dict[str, SessionType] = {
    "rest": "rest",
    "recovery": "recovery",
    "endurance": "endurance",
    "long": "long_endurance",
}


def lower(session: SessionType, steps: int = 1) -> SessionType:
    """``steps`` rungs down the ladder (long endurance counts as endurance)."""
    rank = SESSION_RANK[session]
    target = max(0, rank - steps)
    return LADDER[target]


def strictest(*sessions: SessionType) -> SessionType:
    return min(sessions, key=lambda s: SESSION_RANK[s])


def allowed(session: SessionType, max_session: SessionType) -> bool:
    return SESSION_RANK[session] <= SESSION_RANK[max_session]


@dataclass(frozen=True, slots=True)
class Adaptation:
    """Lower-only adjustments derived from recent performance (``adaptation.py``)."""

    key_max_session: SessionType | None = None  # cap on key days
    day_max_session: SessionType | None = None  # cap on every day
    tss_factor: float = 1.0  # <= 1
    hold_progression: bool = False  # don't ramp this week above last week's target
    reasons: tuple[str, ...] = ()
    flags: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_max_session": self.key_max_session,
            "day_max_session": self.day_max_session,
            "tss_factor": self.tss_factor,
            "hold_progression": self.hold_progression,
            "reasons": list(self.reasons),
            "flags": self.flags,
        }


@dataclass(frozen=True, slots=True)
class DayPrescription:
    date: dt.date
    phase_name: str
    phase_type: str
    role: DayRole
    max_session: SessionType
    target_tss: float
    max_tss: float
    max_minutes: int
    week_tss_target: float
    week_tss_planned: float  # before any hold
    week_tss_done: float
    reasons: tuple[str, ...]

    @property
    def max_intensity(self) -> Intensity:
        return SESSION_INTENSITY[self.max_session]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": PROGRESSION_VERSION,
            "date": self.date.isoformat(),
            "phase": self.phase_name,
            "phase_type": self.phase_type,
            "role": self.role,
            "max_session_type": self.max_session,
            "max_intensity": self.max_intensity.label,
            "target_tss": round(self.target_tss, 1),
            "max_tss": round(self.max_tss, 1),
            "max_minutes": self.max_minutes,
            "week_tss_target": round(self.week_tss_target, 1),
            "week_tss_planned": round(self.week_tss_planned, 1),
            "week_tss_done": round(self.week_tss_done, 1),
            "reasons": list(self.reasons),
        }


def prescribe(
    plan: PlanPhases,
    day: dt.date,
    *,
    week_tss_done: float,
    adaptation: Adaptation | None = None,
) -> DayPrescription | None:
    """The prescription for ``day``; None when the plan has no week or template for it.

    ``week_tss_done`` is the load of this plan week *before* ``day``
    (activities, duplicates excluded).
    """
    phase = plan.phase_for(day)
    if phase is None or plan.week_template is None or phase.weekly_tss_target is None:
        return None
    adaptation = adaptation or Adaptation()
    template = plan.week_template
    role = template[WEEKDAYS[day.weekday()]]
    reasons: list[str] = [f"{phase.name} ({phase.type}), {WEEKDAYS[day.weekday()]}: {role} day"]

    planned_week = phase.weekly_tss_target
    week_target = planned_week
    if adaptation.hold_progression:
        prev = plan.phase_for(phase.start_date - dt.timedelta(days=1))
        if (
            prev is not None
            and prev.weekly_tss_target is not None
            and prev.weekly_tss_target < week_target
        ):
            week_target = prev.weekly_tss_target
            reasons.append(
                f"progression held at last week's target {week_target:.0f} TSS "
                "(last week's training fell short)"
            )

    shares = sum(ROLE_SHARE[r] for r in template.values())
    share = ROLE_SHARE[role] / shares if shares else 0.0
    target = week_target * share * adaptation.tss_factor
    minutes_target = (phase.weekly_minutes_target or 0) * share

    if role == "rest":
        max_session: SessionType = "rest"
    elif role == "key":
        max_session = phase.key_session or "endurance"
    else:
        max_session = ROLE_MAX_SESSION[role]
    if role == "key" and adaptation.key_max_session is not None:
        max_session = strictest(max_session, adaptation.key_max_session)
    if adaptation.day_max_session is not None:
        max_session = strictest(max_session, adaptation.day_max_session)
    if adaptation.tss_factor < 1.0:
        reasons.append(f"budget reduced to {adaptation.tss_factor:.0%} after recent sessions")
    reasons.extend(adaptation.reasons)

    if max_session == "rest":
        return DayPrescription(
            day, phase.name, phase.type, role, "rest", 0.0, 0.0, 0,
            week_target, planned_week, week_tss_done, tuple(reasons),
        )  # fmt: skip

    max_tss = max(target * DAY_TOLERANCE, MIN_SESSION_TSS)
    remaining = week_target * WEEK_TOLERANCE - week_tss_done
    if remaining < max_tss:
        if remaining < MIN_SESSION_TSS:
            max_tss = MIN_SESSION_TSS
            max_session = strictest(max_session, "recovery")
            reasons.append(
                f"week target already reached ({week_tss_done:.0f} of {week_target:.0f} TSS): "
                "easy spin at most"
            )
        else:
            max_tss = remaining
            reasons.append(f"limited by the week's remaining budget ({remaining:.0f} TSS)")
    floor = LONG_MIN_MINUTES if role == "long" else MIN_SESSION_MINUTES
    max_minutes = round(max(minutes_target * DAY_TOLERANCE, floor))
    return DayPrescription(
        day,
        phase.name,
        phase.type,
        role,
        max_session,
        min(target, max_tss),
        max_tss,
        max_minutes,
        week_target,
        planned_week,
        week_tss_done,
        tuple(reasons),
    )
