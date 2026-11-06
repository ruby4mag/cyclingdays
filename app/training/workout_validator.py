"""Deterministic workout validation: the gate before anything is published.

Every rule is a pure function of the structured workout and a
:class:`ValidationContext`. The graph runs the same validator on LLM drafts
(violations go back to the model as corrections), on the fallback plan, and
again in the publisher immediately before writing to Intervals.icu.

Rule groups, matching the Phase 5 specification:

* **power**: valid FTP, targets within 30-160 % FTP, ranges narrow enough for
  ERG, warmup, recovery and cooldown kept easy;
* **duration**: step and session length, and agreement with the objective;
* **intervals**: work-interval length and total work per session type, hard
  efforts labelled as work;
* **recovery**: recoveries between hard efforts, long and easy enough;
* **readiness**: every step and the whole session within the readiness cap,
  with an IF ceiling per cap;
* **weekly**: the number of hard sessions in the rolling 7 days;
* **load**: session TSS and rolling 7-day TSS against limits derived from CTL
  (fixed conservative limits while the load history is not authoritative).

``weekly`` and ``load`` need the recent history; without it they are
reported as ``skipped`` (a warning, not a pass). Thresholds are coaching
conventions documented in ``docs/WORKOUTS.md``; change them only with a new
``VALIDATOR_VERSION``.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.training.intensity import Intensity, cap
from app.training.progression import allowed
from app.training.readiness import HARD_SESSION_IF, HARD_SESSION_TSS
from app.training.sessions import (
    INTERVAL_RULES,
    SESSION_DURATION_MINUTES,
    SESSION_INTENSITY,
)
from app.training.workout_generator import StructuredWorkout, WorkoutStepSpec
from app.training.zones import InvalidFTPError, validate_ftp

VALIDATOR_VERSION = "v2"  # v2 (Phase 7): "plan" group

# Zone -> the intensity level it represents.
ZONE_INTENSITY = {
    1: Intensity.RECOVERY,
    2: Intensity.ENDURANCE,
    3: Intensity.TEMPO,
    4: Intensity.THRESHOLD,
    5: Intensity.VO2MAX,
    6: Intensity.VO2MAX,
    7: Intensity.VO2MAX,
}

# power
MIN_TARGET_PCT = 30
MAX_TARGET_PCT = 160
MAX_RANGE_WIDTH_PCT = 10  # ERG holds one target; wide ranges are ambiguous
MAX_EASY_STEP_PCT = {"warmup": 75, "cooldown": 65, "recovery": 60}
# duration
MIN_STEP_S = 30
MAX_STEP_S = 4 * 3600
DURATION_TOLERANCE = 0.20  # workout total vs objective target
WARMUP_REQUIRED_FROM_MINUTES = 30
MIN_WARMUP_BEFORE_INTERVALS_S = 5 * 60
MAX_SINGLE_STEP_ABOVE_THRESHOLD_S = 20 * 60
# readiness: IF ceiling per cap
MAX_INTENSITY_FACTOR = {
    Intensity.RECOVERY: 0.65,
    Intensity.ENDURANCE: 0.78,
    Intensity.TEMPO: 0.88,
    Intensity.THRESHOLD: 0.95,
    Intensity.VO2MAX: 1.00,
}
# weekly distribution
MAX_HARD_SESSIONS_7D = 3  # including the planned one
# recent load
SESSION_TSS_FLOOR = 80.0  # always allowed (when other rules pass)
SESSION_TSS_CTL_MULTIPLIER = 2.0
UNTRUSTED_SESSION_TSS_MAX = 100.0  # while CTL is not authoritative
WEEK_TSS_FLOOR = 400.0
WEEK_TSS_CTL_MULTIPLIER = 1.5  # x 7 x CTL
UNTRUSTED_WEEK_TSS_MAX = 500.0

Group = Literal["power", "duration", "intervals", "recovery", "readiness", "weekly", "load", "plan"]
GROUPS: tuple[Group, ...] = (
    "power",
    "duration",
    "intervals",
    "recovery",
    "readiness",
    "weekly",
    "load",
    "plan",
)
PLAN_MINUTES_TOLERANCE = 1.05  # rounding slack on the plan's time limit


@dataclass(frozen=True, slots=True)
class DayLoad:
    """Load of one earlier day in the rolling window (actual or still planned)."""

    date: dt.date
    tss: float | None
    hard: bool
    source: Literal["activity", "planned"]


@dataclass(frozen=True, slots=True)
class ValidationContext:
    plan_date: dt.date
    max_intensity: Intensity
    readiness_status: str | None = None
    target_minutes: int | None = None
    ctl: float | None = None
    ctl_authoritative: bool = False
    # Days plan_date-6 .. plan_date-1; None = history unavailable (rules skipped).
    history: Sequence[DayLoad] | None = None
    # Long-term plan prescription for the day (Phase 7); None = no plan (rules skipped).
    max_session_type: str | None = None
    plan_max_tss: float | None = None
    plan_max_minutes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_date": self.plan_date.isoformat(),
            "max_intensity": self.max_intensity.label,
            "readiness_status": self.readiness_status,
            "target_minutes": self.target_minutes,
            "ctl": self.ctl,
            "ctl_authoritative": self.ctl_authoritative,
            "history": None
            if self.history is None
            else [
                {"date": d.date.isoformat(), "tss": d.tss, "hard": d.hard, "source": d.source}
                for d in self.history
            ],
            "max_session_type": self.max_session_type,
            "plan_max_tss": self.plan_max_tss,
            "plan_max_minutes": self.plan_max_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationContext:
        history = data.get("history")
        return cls(
            plan_date=dt.date.fromisoformat(data["plan_date"]),
            max_intensity=Intensity[str(data["max_intensity"]).upper()],
            readiness_status=data.get("readiness_status"),
            target_minutes=data.get("target_minutes"),
            ctl=data.get("ctl"),
            ctl_authoritative=bool(data.get("ctl_authoritative")),
            history=None
            if history is None
            else [
                DayLoad(
                    date=dt.date.fromisoformat(d["date"]),
                    tss=d.get("tss"),
                    hard=bool(d.get("hard")),
                    source=d.get("source", "activity"),
                )
                for d in history
            ],
            max_session_type=data.get("max_session_type"),
            plan_max_tss=data.get("plan_max_tss"),
            plan_max_minutes=data.get("plan_max_minutes"),
        )


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # group -> passed/failed/skipped
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "validator_version": VALIDATOR_VERSION,
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checks,
            "metrics": self.metrics,
        }


def step_intensity(step: WorkoutStepSpec) -> Intensity:
    return ZONE_INTENSITY[step.zone]


def is_hard(workout: StructuredWorkout) -> bool:
    """Same definition of "hard" as the readiness assessment."""
    session = SESSION_INTENSITY[workout.objective.session_type]
    return (
        session >= Intensity.THRESHOLD
        or workout.estimate.intensity_factor >= HARD_SESSION_IF
        or workout.estimate.tss >= HARD_SESSION_TSS
    )


# --- rule groups ----------------------------------------------------------------


def _power(w: StructuredWorkout) -> list[str]:
    problems: list[str] = []
    try:
        validate_ftp(w.ftp_watts)
    except InvalidFTPError as exc:
        return [f"invalid FTP reference: {exc}"]
    for s in w.steps:
        if not MIN_TARGET_PCT <= s.low_pct <= s.high_pct <= MAX_TARGET_PCT:
            problems.append(
                f"step {s.index}: target {s.low_pct}-{s.high_pct}% FTP must satisfy "
                f"{MIN_TARGET_PCT} <= low <= high <= {MAX_TARGET_PCT}"
            )
        if s.high_pct - s.low_pct > MAX_RANGE_WIDTH_PCT:
            problems.append(
                f"step {s.index}: target range {s.low_pct}-{s.high_pct}% is wider than "
                f"{MAX_RANGE_WIDTH_PCT} percentage points"
            )
        limit = MAX_EASY_STEP_PCT.get(s.kind)
        if limit is not None and s.high_pct > limit:
            problems.append(f"step {s.index}: {s.kind} target above {limit}% FTP")
        expected = (round(s.low_pct / 100 * w.ftp_watts), round(s.high_pct / 100 * w.ftp_watts))
        if (s.low_watts, s.high_watts) != expected:
            problems.append(f"step {s.index}: watts do not match the FTP reference")
    return problems


def _duration(w: StructuredWorkout, ctx: ValidationContext) -> list[str]:
    problems: list[str] = []
    for s in w.steps:
        if not MIN_STEP_S <= s.duration_s <= MAX_STEP_S:
            problems.append(
                f"step {s.index}: duration {s.duration_s}s outside {MIN_STEP_S}-{MAX_STEP_S}s"
            )
    total_min = w.total_s / 60
    session = w.objective.session_type
    bounds = SESSION_DURATION_MINUTES.get(session)
    if bounds and not bounds[0] <= total_min <= bounds[1]:
        problems.append(
            f"a {session} session must last {bounds[0]}-{bounds[1]} min (got {total_min:.0f})"
        )
    target = ctx.target_minutes or w.objective.target_duration_minutes
    if target > 0 and abs(total_min - target) > DURATION_TOLERANCE * target:
        problems.append(
            f"total duration {total_min:.0f} min differs from the objective's {target} min "
            f"by more than {DURATION_TOLERANCE:.0%}"
        )
    if total_min >= WARMUP_REQUIRED_FROM_MINUTES:
        if w.steps[0].kind != "warmup":
            problems.append("the first step must be a warmup")
        if w.steps[-1].kind != "cooldown":
            problems.append("the last step must be a cooldown")
    return problems


def _intervals(w: StructuredWorkout) -> list[str]:
    problems: list[str] = []
    for s in w.steps:
        level = step_intensity(s)
        if level >= Intensity.THRESHOLD and s.kind != "work":
            problems.append(f"step {s.index}: efforts at {level.label} must be 'work' steps")
        if level >= Intensity.VO2MAX and s.duration_s > MAX_SINGLE_STEP_ABOVE_THRESHOLD_S:
            problems.append(f"step {s.index}: {level.label} step longer than 20 minutes")

    session = w.objective.session_type
    work = w.work_steps
    if session == "recovery" and work:
        problems.append("a recovery session has no 'work' steps")
    rules = INTERVAL_RULES.get(session)
    if rules is None:
        return problems
    if not work:
        return [*problems, f"a {session} session needs at least one 'work' step"]
    for s in work:
        if s.high_pct < rules.min_work_pct_ftp:
            problems.append(
                f"step {s.index}: a {session} work interval must reach at least "
                f"{rules.min_work_pct_ftp}% FTP"
            )
        if not rules.work_step_min_s <= s.duration_s <= rules.work_step_max_s:
            problems.append(
                f"step {s.index}: {session} work intervals last "
                f"{rules.work_step_min_s // 60}-{rules.work_step_max_s // 60} min"
            )
    total_work = sum(s.duration_s for s in work)
    if total_work > rules.total_work_max_s:
        problems.append(
            f"total work time {total_work // 60} min exceeds {rules.total_work_max_s // 60} min "
            f"for a {session} session"
        )
    first = w.steps.index(work[0])
    warmup_s = sum(s.duration_s for s in w.steps[:first])
    if warmup_s < MIN_WARMUP_BEFORE_INTERVALS_S:
        problems.append("at least 5 min of warmup before the first work interval")
    return problems


def _recovery(w: StructuredWorkout) -> list[str]:
    problems: list[str] = []
    rules = INTERVAL_RULES.get(w.objective.session_type)
    steps = list(w.steps)
    work_idx = [i for i, s in enumerate(steps) if s.kind == "work"]
    for a, b in itertools.pairwise(work_idx):
        effort = steps[a]
        between = steps[a + 1 : b]
        if step_intensity(effort) < Intensity.THRESHOLD:
            continue
        if not between:
            problems.append(
                f"steps {effort.index} and {steps[b].index}: hard efforts need a recovery "
                "step between them"
            )
            continue
        if any(s.kind != "recovery" for s in between):
            problems.append(
                f"step {effort.index}: only 'recovery' steps may separate hard work intervals"
            )
        recovery_s = sum(s.duration_s for s in between)
        ratio = rules.min_recovery_ratio if rules else 0.25
        if recovery_s < ratio * effort.duration_s:
            problems.append(
                f"step {effort.index}: recovery after a {effort.duration_s // 60} min effort must "
                f"last at least {ratio:.0%} of it ({recovery_s}s given)"
            )
    if work_idx and work_idx[-1] == len(steps) - 1 and w.total_s / 60 >= 20:
        problems.append("the workout must not end on a work interval")
    return problems


def _readiness(w: StructuredWorkout, ctx: ValidationContext) -> list[str]:
    problems: list[str] = []
    session = SESSION_INTENSITY[w.objective.session_type]
    if session > ctx.max_intensity:
        problems.append(
            f"session_type {w.objective.session_type!r} ({session.label}) exceeds the maximum "
            f"allowed intensity {ctx.max_intensity.label!r}"
        )
    if ctx.max_intensity <= Intensity.REST:
        return [*problems, "readiness requires a rest day: no workout may be published"]
    allowed = cap(ctx.max_intensity, session)
    for s in w.steps:
        level = step_intensity(s)
        if level > allowed:
            problems.append(
                f"step {s.index} ({s.kind}, up to {s.high_pct}% FTP) is {level.label}, "
                f"above the allowed {allowed.label}"
            )
    if ctx.readiness_status == "red" and session > Intensity.RECOVERY:
        problems.append("readiness is red: only recovery sessions are allowed")
    ceiling = MAX_INTENSITY_FACTOR[cap(ctx.max_intensity, session)]
    if w.estimate.intensity_factor > ceiling:
        problems.append(
            f"estimated IF {w.estimate.intensity_factor:.3f} exceeds {ceiling:.2f} allowed at "
            f"{allowed.label}"
        )
    return problems


def _window(ctx: ValidationContext) -> list[DayLoad]:
    assert ctx.history is not None
    start = ctx.plan_date - dt.timedelta(days=6)
    return [d for d in ctx.history if start <= d.date < ctx.plan_date]


def _weekly(w: StructuredWorkout, ctx: ValidationContext) -> list[str]:
    days = _window(ctx)
    hard_days = sorted({d.date for d in days if d.hard})
    if not is_hard(w):
        return []
    problems: list[str] = []
    if len(hard_days) + 1 > MAX_HARD_SESSIONS_7D:
        problems.append(
            f"{len(hard_days)} hard sessions already in the last 6 days; at most "
            f"{MAX_HARD_SESSIONS_7D} per 7 days"
        )
    if ctx.plan_date - dt.timedelta(days=1) in hard_days:
        problems.append("no hard session on the day after a hard session")
    return problems


def load_limits(ctx: ValidationContext) -> tuple[float, float]:
    """(session TSS limit, rolling 7-day TSS limit)."""
    if ctx.ctl_authoritative and ctx.ctl is not None:
        return (
            max(SESSION_TSS_FLOOR, SESSION_TSS_CTL_MULTIPLIER * ctx.ctl),
            max(WEEK_TSS_FLOOR, WEEK_TSS_CTL_MULTIPLIER * 7 * ctx.ctl),
        )
    return UNTRUSTED_SESSION_TSS_MAX, UNTRUSTED_WEEK_TSS_MAX


def _load(w: StructuredWorkout, ctx: ValidationContext, metrics: dict[str, Any]) -> list[str]:
    session_max, week_max = load_limits(ctx)
    prior = sum(d.tss or 0.0 for d in _window(ctx))
    week = prior + w.estimate.tss
    metrics.update(
        session_tss_limit=round(session_max, 1),
        week_tss_limit=round(week_max, 1),
        week_tss_with_workout=round(week, 1),
    )
    problems: list[str] = []
    if w.estimate.tss > session_max:
        problems.append(
            f"estimated TSS {w.estimate.tss:.0f} exceeds the session limit {session_max:.0f}"
        )
    if week > week_max:
        problems.append(f"rolling 7-day TSS would be {week:.0f}, above the limit {week_max:.0f}")
    return problems


def _plan(w: StructuredWorkout, ctx: ValidationContext) -> list[str]:
    problems: list[str] = []
    session = w.objective.session_type
    if ctx.max_session_type is not None and not allowed(session, ctx.max_session_type):  # type: ignore[arg-type]
        problems.append(
            f"the long-term plan allows at most a {ctx.max_session_type} session today, "
            f"not {session}"
        )
    if ctx.plan_max_tss is not None and w.estimate.tss > ctx.plan_max_tss:
        problems.append(
            f"estimated TSS {w.estimate.tss:.0f} exceeds today's plan budget {ctx.plan_max_tss:.0f}"
        )
    if (
        ctx.plan_max_minutes is not None
        and w.total_minutes > ctx.plan_max_minutes * PLAN_MINUTES_TOLERANCE
    ):
        problems.append(
            f"duration {w.total_minutes:.0f} min exceeds today's plan limit "
            f"{ctx.plan_max_minutes} min"
        )
    return problems


def _has_plan(ctx: ValidationContext) -> bool:
    return any(
        v is not None for v in (ctx.max_session_type, ctx.plan_max_tss, ctx.plan_max_minutes)
    )


def validate_workout(workout: StructuredWorkout, ctx: ValidationContext) -> ValidationResult:
    result = ValidationResult()
    result.metrics = {
        "total_minutes": round(workout.total_minutes, 1),
        "estimated_tss": workout.estimate.tss,
        "estimated_if": workout.estimate.intensity_factor,
        "hard": is_hard(workout),
        "sections_s": workout.sections_s,
    }
    groups: dict[Group, list[str] | None] = {
        "power": _power(workout),
        "duration": _duration(workout, ctx),
        "intervals": _intervals(workout),
        "recovery": _recovery(workout),
        "readiness": _readiness(workout, ctx),
        "weekly": None if ctx.history is None else _weekly(workout, ctx),
        "load": None if ctx.history is None else _load(workout, ctx, result.metrics),
        "plan": _plan(workout, ctx) if _has_plan(ctx) else None,
    }
    for group in GROUPS:
        problems = groups[group]
        if problems is None:
            result.checks[group] = "skipped"
            why = (
                "no long-term plan prescription for this day"
                if group == "plan"
                else "recent training history unavailable"
            )
            result.warnings.append(f"{group} rules skipped: {why}")
        elif problems:
            result.checks[group] = "failed"
            result.errors.extend(problems)
        else:
            result.checks[group] = "passed"
    return result
