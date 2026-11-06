"""Adapt the next days to how recent sessions went (Phase 7).

Input: the deterministic evaluations of recent rides (``evaluation.py``) and
how much of last week's planned load was actually done. Output: an
:class:`~app.training.progression.Adaptation` that can only **hold or lower**
the plan. Nothing here ever raises intensity or load. Progression comes only
from the plan's ramp, and completing a workout is never by itself a reason
to go harder.

Rules (version ``ADAPTATION_VERSION``):

1. **Struggled key session.** The latest structured session (tempo or
   harder) in the last ``KEY_LOOKBACK_DAYS`` days counts as struggled if:
   * completion < ``MIN_COMPLETION_PCT`` (85 %);
   * power deviation < ``-MAX_UNDER_TARGET_PCT`` (-5 %);
   * or RPE ≥ expected + ``RPE_MARGIN`` (2).

   Then key days are capped one rung below that session type.
2. **Two struggled key sessions in a row:** key days are capped at endurance.
   The note suggests checking FTP. FTP is never changed automatically.
3. **Hard-feeling recent ride.** A ride in the last ``RECENT_DAYS`` days with:
   * RPE ≥ expected + 2;
   * aerobic decoupling > ``MAX_DECOUPLING_PCT`` (8 %) on an endurance ride;
   * or the evaluator's recommendation "reduce" (LLM, lower-only).

   Then the day budget is cut to ``REDUCED_TSS_FACTOR`` (85 %).
4. **Evaluator says "recover" for yesterday's ride** (LLM, lower-only): today
   is capped at recovery.
5. **Last week fell short.** Under ``MIN_WEEK_COMPLIANCE`` (70 %) of the
   week's target was done, so this week's target is held at last week's
   level. The missed load is **not** added to this week.

Pure functions; no I/O.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.training.progression import SESSION_RANK, Adaptation, lower, strictest
from app.training.sessions import SessionType

ADAPTATION_VERSION = "adaptation/v1"

KEY_LOOKBACK_DAYS = 10
RECENT_DAYS = 3
MIN_COMPLETION_PCT = 85.0
MAX_UNDER_TARGET_PCT = 5.0
RPE_MARGIN = 2.0
MAX_DECOUPLING_PCT = 8.0
REDUCED_TSS_FACTOR = 0.85
MIN_WEEK_COMPLIANCE = 0.70

# Expected session RPE (0-10 scale used by Intervals.icu) when a session goes to plan.
EXPECTED_RPE: dict[str, float] = {
    "recovery": 2.0,
    "endurance": 4.0,
    "long_endurance": 5.0,
    "tempo": 5.0,
    "sweet_spot": 6.0,
    "threshold": 7.0,
    "vo2max": 8.0,
}


@dataclass(frozen=True, slots=True)
class SessionResult:
    """One evaluated ride, reduced to what adaptation needs."""

    date: dt.date
    session_type: str | None  # planned session type; None = unplanned ride
    completion_pct: float | None
    power_deviation_pct: float | None
    rpe: float | None
    decoupling_pct: float | None
    recommendation: str | None = None  # LLM evaluator: keep | reduce | recover

    @property
    def is_key(self) -> bool:
        return self.session_type is not None and SESSION_RANK.get(self.session_type, 0) >= 3

    @property
    def rpe_delta(self) -> float | None:
        expected = EXPECTED_RPE.get(self.session_type or "")
        if self.rpe is None or expected is None:
            return None
        return self.rpe - expected

    def struggle_reasons(self) -> list[str]:
        out: list[str] = []
        if self.completion_pct is not None and self.completion_pct < MIN_COMPLETION_PCT:
            out.append(f"completed {self.completion_pct:.0f} % of the planned time")
        if (
            self.power_deviation_pct is not None
            and self.power_deviation_pct < -MAX_UNDER_TARGET_PCT
        ):
            out.append(f"power {self.power_deviation_pct:.1f} % below target")
        delta = self.rpe_delta
        if delta is not None and self.rpe is not None and delta >= RPE_MARGIN:
            out.append(f"RPE {self.rpe:.0f} vs about {self.rpe - delta:.0f} expected")
        return out


def adapt(
    day: dt.date,
    results: Sequence[SessionResult],
    *,
    last_week_compliance: float | None,
) -> Adaptation:
    reasons: list[str] = []
    flags: dict[str, Any] = {}
    key_cap: SessionType | None = None
    day_cap: SessionType | None = None
    factor = 1.0

    past = sorted((r for r in results if r.date < day), key=lambda r: r.date)
    keys = [r for r in past if r.is_key and (day - r.date).days <= KEY_LOOKBACK_DAYS]
    if keys:
        latest = keys[-1]
        why = latest.struggle_reasons()
        flags["latest_key_session"] = {
            "date": latest.date.isoformat(),
            "session_type": latest.session_type,
            "struggled": bool(why),
            "reasons": why,
        }
        if why:
            assert latest.session_type is not None
            key_cap = lower(latest.session_type)  # type: ignore[arg-type]
            reasons.append(
                f"key session on {latest.date.isoformat()} ({latest.session_type}) was hard: "
                f"{'; '.join(why)}; key days capped at {key_cap}"
            )
            if len(keys) >= 2 and keys[-2].struggle_reasons():
                key_cap = strictest(key_cap, "endurance")
                reasons.append(
                    "two key sessions in a row were hard: key days capped at endurance; "
                    "check whether FTP is set too high (never changed automatically)"
                )
                flags["ftp_check_suggested"] = True

    for r in past:
        if (day - r.date).days > RECENT_DAYS:
            continue
        delta = r.rpe_delta
        heavy = delta is not None and delta >= RPE_MARGIN
        drift = (
            r.session_type in ("endurance", "long_endurance")
            and r.decoupling_pct is not None
            and r.decoupling_pct > MAX_DECOUPLING_PCT
        )
        if (heavy or drift or r.recommendation in ("reduce", "recover")) and (
            factor > REDUCED_TSS_FACTOR
        ):
            factor = REDUCED_TSS_FACTOR
            what = (
                "high RPE"
                if heavy
                else f"aerobic decoupling {r.decoupling_pct:.1f} %"
                if drift
                else f"evaluator recommended '{r.recommendation}'"
            )
            reasons.append(f"ride on {r.date.isoformat()}: {what}")
        if r.recommendation == "recover" and (day - r.date).days == 1:
            day_cap = "recovery"
            reasons.append("evaluator recommended recovery after yesterday's ride")

    hold = last_week_compliance is not None and last_week_compliance < MIN_WEEK_COMPLIANCE
    if last_week_compliance is not None:
        flags["last_week_compliance"] = round(last_week_compliance, 2)
    if hold:
        reasons.append(
            f"last week reached {last_week_compliance:.0%} of its target: "
            "no ramp this week, and missed load is not made up"
        )
    return Adaptation(
        key_max_session=key_cap,
        day_max_session=day_cap,
        tss_factor=factor,
        hold_progression=hold,
        reasons=tuple(reasons),
        flags=flags,
    )
