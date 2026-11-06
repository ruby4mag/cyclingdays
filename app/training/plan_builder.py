"""Deterministic long-term plan generator (Phase 7).

The plan is a list of **weekly phases** (Monday-Sunday). Each week has a
type, a weekly TSS target, a weekly time target and the most demanding
session allowed on key days. A weekly template assigns each weekday a role
(rest, recovery, endurance, key, long). ``progression.py`` turns plan,
template and the actual training into the prescription for one day.

Structure (version ``PLAN_BUILDER_VERSION``):

* **4-week blocks:** 3 load weeks, then 1 recovery week.
* **Load weeks** ramp up by ``WEEKLY_RAMP`` (+8 %) per week. A recovery week
  is ``RECOVERY_WEEK_FACTOR`` (60 %) of the week before it.
* **The next block** starts one ramp step above the previous block's second
  week, so fitness builds across blocks without jumping.
* **Key sessions by block:**
  1. base, tempo;
  2. base, sweet spot;
  3. build, threshold;
  4. build, VO2max, and every block after that.
* **Load:**
  * The start is ``START_TSS_PER_HOUR`` x the lower bound of the athlete's
    weekly hours. The default is deliberately conservative: there's no ride
    history to show current fitness.
  * The cap is ``MAX_TSS_PER_HOUR`` x the upper bound. With 6-9 h that is
    300 TSS at the start and at most 495 TSS a week.
* **With an event date:**
  * the last 2 weeks before the event are taper (70 %, then 45 %, with
    threshold openers);
  * the 2 weeks before those are peak (threshold);
  * earlier weeks follow the block pattern.

  Without a date the plan is open-ended. ``extend_plan`` appends blocks so
  the horizon never runs out.

Every number here is a coaching convention, documented in
docs/ADAPTIVE_COACHING.md. Pure functions; no I/O.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.training.periodization import DayRole, PhaseType, PlanPhase, PlanPhases
from app.training.sessions import SessionType

PLAN_BUILDER_VERSION = "plan_builder/v1"

START_TSS_PER_HOUR = 50.0
MAX_TSS_PER_HOUR = 55.0
TSS_PER_HOUR_FOR_TIME = 50.0  # converts a TSS target into a time target
WEEKLY_RAMP = 0.08
RECOVERY_WEEK_FACTOR = 0.60
BLOCK_WEEKS = 4
DEFAULT_BLOCKS = 3  # open-ended horizon: 12 weeks
TAPER_FACTORS = (0.70, 0.45)
PEAK_WEEKS = 2

# (phase type, key session) for block 1, 2, 3, 4+.
BLOCK_SEQUENCE: tuple[tuple[PhaseType, SessionType], ...] = (
    ("base", "tempo"),
    ("base", "sweet_spot"),
    ("build", "threshold"),
    ("build", "vo2max"),
)

DEFAULT_TEMPLATE: dict[str, DayRole] = {
    "mon": "rest",
    "tue": "key",
    "wed": "endurance",
    "thu": "key",
    "fri": "recovery",
    "sat": "long",
    "sun": "endurance",
}


class PlanBuildError(ValueError):
    pass


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _minutes_for(tss: float, hours_min: float, hours_max: float) -> int:
    minutes = tss / TSS_PER_HOUR_FOR_TIME * 60
    return round(min(max(minutes, hours_min * 60 * 0.5), hours_max * 60))


def _week(
    start: dt.date,
    name: str,
    phase_type: PhaseType,
    key: SessionType,
    tss: float,
    hours: tuple[float, float],
    notes: str,
) -> PlanPhase:
    return PlanPhase(
        name=name,
        type=phase_type,
        start_date=start,
        end_date=start + dt.timedelta(days=6),
        weekly_tss_target=round(tss),
        weekly_minutes_target=_minutes_for(tss, *hours),
        key_session=key,
        notes=notes,
    )


def _blocks(
    first_monday: dt.date,
    weeks: int,
    hours: tuple[float, float],
    *,
    first_block: int = 0,
    start_tss: float | None = None,
) -> tuple[list[PlanPhase], float]:
    """``weeks`` weeks of blocks; returns the phases and the next block's start TSS."""
    lo, hi = hours
    cap = hi * MAX_TSS_PER_HOUR
    tss = min(start_tss if start_tss is not None else lo * START_TSS_PER_HOUR, cap)
    phases: list[PlanPhase] = []
    block = first_block
    week_in_block = 0
    block_start = tss
    for w in range(weeks):
        monday = first_monday + dt.timedelta(weeks=w)
        phase_type, key = BLOCK_SEQUENCE[min(block, len(BLOCK_SEQUENCE) - 1)]
        if week_in_block < BLOCK_WEEKS - 1:
            load = min(block_start * (1 + WEEKLY_RAMP) ** week_in_block, cap)
            phases.append(
                _week(
                    monday,
                    f"Block {block + 1} week {week_in_block + 1}",
                    phase_type,
                    key,
                    load,
                    hours,
                    f"{phase_type} load week",
                )
            )
        else:
            last = min(block_start * (1 + WEEKLY_RAMP) ** (BLOCK_WEEKS - 2), cap)
            phases.append(
                _week(
                    monday,
                    f"Block {block + 1} recovery week",
                    "recovery",
                    "endurance",
                    last * RECOVERY_WEEK_FACTOR,
                    hours,
                    "recovery week: absorb the block",
                )
            )
        week_in_block += 1
        if week_in_block == BLOCK_WEEKS:
            block += 1
            week_in_block = 0
            block_start = min(block_start * (1 + WEEKLY_RAMP) ** 2, cap)
    return phases, block_start


def build_plan(
    *,
    start: dt.date,
    weekly_hours: tuple[float, float],
    event_date: dt.date | None = None,
    blocks: int = DEFAULT_BLOCKS,
    template: dict[str, DayRole] | None = None,
) -> PlanPhases:
    lo, hi = weekly_hours
    if not 1 <= lo <= hi <= 30:
        raise PlanBuildError("weekly hours must satisfy 1 <= min <= max <= 30")
    first_monday = monday_of(start)
    hours = (lo, hi)
    if event_date is None:
        phases, _ = _blocks(first_monday, blocks * BLOCK_WEEKS, hours)
    else:
        event_monday = monday_of(event_date)
        total_weeks = (event_monday - first_monday).days // 7 + 1  # event week included
        taper_weeks = len(TAPER_FACTORS)
        if total_weeks < taper_weeks + PEAK_WEEKS + 2:
            raise PlanBuildError(
                f"the event is {total_weeks} weeks away; at least "
                f"{taper_weeks + PEAK_WEEKS + 2} weeks are needed for a plan"
            )
        base_weeks = total_weeks - taper_weeks - PEAK_WEEKS
        phases, _ = _blocks(first_monday, base_weeks, hours)
        last_load = max((p.weekly_tss_target or 0) for p in phases if p.type in ("base", "build"))
        for i in range(PEAK_WEEKS):
            monday = first_monday + dt.timedelta(weeks=base_weeks + i)
            phases.append(
                _week(
                    monday,
                    f"Peak week {i + 1}",
                    "peak",
                    "threshold",
                    last_load,
                    hours,
                    "race-specific long rides, threshold sharpening",
                )
            )
        for i, factor in enumerate(TAPER_FACTORS):
            monday = first_monday + dt.timedelta(weeks=base_weeks + PEAK_WEEKS + i)
            phases.append(
                _week(
                    monday,
                    f"Taper week {i + 1}",
                    "taper",
                    "threshold",
                    last_load * factor,
                    hours,
                    "reduce volume, keep short openers",
                )
            )
    return PlanPhases(
        phases=phases,
        week_template=dict(template or DEFAULT_TEMPLATE),
        builder={
            "version": PLAN_BUILDER_VERSION,
            "start": first_monday.isoformat(),
            "weekly_hours": [lo, hi],
            "event_date": None if event_date is None else event_date.isoformat(),
            "blocks": blocks if event_date is None else None,
        },
    )


def extend_plan(plan: PlanPhases, *, until: dt.date) -> PlanPhases | None:
    """Append blocks to an open-ended plan so it covers ``until``. None if nothing to add."""
    builder: dict[str, Any] = plan.builder or {}
    if builder.get("event_date") or not plan.phases:
        return None
    last = max(plan.phases, key=lambda p: p.end_date)
    if last.end_date >= until:
        return None
    lo, hi = builder.get("weekly_hours") or (None, None)
    if lo is None or hi is None:
        return None
    blocks_done = len(plan.phases) // BLOCK_WEEKS
    weeks_needed = ((until - last.end_date).days + 6) // 7
    weeks = -(-weeks_needed // BLOCK_WEEKS) * BLOCK_WEEKS  # whole blocks
    # Continue the ramp: rebuild the existing blocks' progression to find the next start.
    _, next_start = _blocks(
        monday_of(dt.date.fromisoformat(builder["start"])), blocks_done * BLOCK_WEEKS, (lo, hi)
    )
    new, _ = _blocks(
        last.end_date + dt.timedelta(days=1),
        weeks,
        (lo, hi),
        first_block=blocks_done,
        start_tss=next_start,
    )
    return PlanPhases(
        phases=[*plan.phases, *new],
        week_template=plan.week_template,
        builder={**builder, "blocks": blocks_done + weeks // BLOCK_WEEKS},
    )
