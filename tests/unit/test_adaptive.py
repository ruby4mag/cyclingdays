"""Phase 7: long-term plan, day prescription, adaptation, ride evaluation, guidance."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.graph.guidance import build_guidance
from app.training.adaptation import SessionResult, adapt
from app.training.evaluation import decoupling_pct, evaluate_ride, to_seconds_series
from app.training.intensity import Intensity
from app.training.periodization import PlanPhases
from app.training.plan_builder import (
    PlanBuildError,
    build_plan,
    extend_plan,
    monday_of,
)
from app.training.progression import (
    MIN_SESSION_TSS,
    Adaptation,
    allowed,
    lower,
    prescribe,
)
from app.training.workout_generator import StepInput, WorkoutObjective, build_workout
from app.training.workout_validator import ValidationContext, validate_workout

MON = dt.date(2026, 11, 9)


def plan() -> PlanPhases:
    return build_plan(start=MON, weekly_hours=(6, 9))


# --------------------------------------------------------------------------
# Plan builder
# --------------------------------------------------------------------------


def test_open_ended_plan_blocks_ramp_and_recovery_weeks() -> None:
    p = plan()
    assert len(p.phases) == 12
    assert p.phases[0].start_date == MON and p.phases[0].start_date.weekday() == 0
    tss = [ph.weekly_tss_target for ph in p.phases]
    assert tss[:4] == [300, 324, 350, 210]  # +8 %/week, recovery = 60 % of week 3
    assert [ph.type for ph in p.phases[:4]] == ["base", "base", "base", "recovery"]
    assert [ph.key_session for ph in p.phases[::4]] == ["tempo", "sweet_spot", "threshold"]
    assert max(t or 0 for t in tss) <= 9 * 55  # never above the weekly-hours cap
    # Weeks are contiguous and never overlap (validated by PlanPhases too).
    for a, b in zip(p.phases, p.phases[1:], strict=False):
        assert b.start_date == a.end_date + dt.timedelta(days=1)


def test_plan_round_trips_through_json() -> None:
    p = plan()
    again = PlanPhases.from_json(p.model_dump(mode="json"))
    assert again == p
    assert again.builder is not None and again.builder["weekly_hours"] == [6, 9]


def test_plan_with_event_ends_with_peak_and_taper() -> None:
    event = MON + dt.timedelta(weeks=15, days=5)  # Saturday of week 16
    p = build_plan(start=MON, weekly_hours=(6, 9), event_date=event)
    types = [ph.type for ph in p.phases]
    assert types[-4:] == ["peak", "peak", "taper", "taper"]
    assert p.phases[-1].start_date <= event <= p.phases[-1].end_date
    assert (p.phases[-1].weekly_tss_target or 0) < (p.phases[-3].weekly_tss_target or 0)


def test_plan_rejects_bad_inputs() -> None:
    with pytest.raises(PlanBuildError):
        build_plan(start=MON, weekly_hours=(9, 6))
    with pytest.raises(PlanBuildError):
        build_plan(start=MON, weekly_hours=(6, 9), event_date=MON + dt.timedelta(weeks=3))


def test_extend_plan_continues_the_ramp() -> None:
    p = plan()
    extended = extend_plan(p, until=p.phases[-1].end_date + dt.timedelta(days=10))
    assert extended is not None
    assert len(extended.phases) == 16
    assert extended.phases[12].name == "Block 4 week 1"
    assert extended.phases[12].key_session == "vo2max"
    assert (extended.phases[12].weekly_tss_target or 0) >= (
        extended.phases[8].weekly_tss_target or 0
    )
    assert extend_plan(extended, until=MON) is None  # already covered


# --------------------------------------------------------------------------
# Prescription
# --------------------------------------------------------------------------


def test_weekly_template_roles_and_caps() -> None:
    p = plan()
    week = [prescribe(p, MON + dt.timedelta(days=i), week_tss_done=0) for i in range(7)]
    assert all(x is not None for x in week)
    roles = [x.role for x in week if x]
    assert roles == ["rest", "key", "endurance", "key", "recovery", "long", "endurance"]
    mon, tue, _, _, fri, sat, _ = week
    assert mon and mon.max_session == "rest" and mon.max_tss == 0
    assert tue and tue.max_session == "tempo" and tue.max_intensity == Intensity.TEMPO
    assert fri and fri.max_session == "recovery"
    assert sat and sat.max_session == "long_endurance" and sat.max_minutes >= 120
    # Budgets add up to about the weekly target.
    assert sum(x.target_tss for x in week if x) == pytest.approx(300, abs=1)


def test_missed_sessions_are_not_crammed() -> None:
    p = plan()
    sat = MON + dt.timedelta(days=5)
    fresh = prescribe(p, sat, week_tss_done=150)
    behind = prescribe(p, sat, week_tss_done=0)  # every session this week missed
    assert fresh and behind
    # The day's limit doesn't grow when earlier sessions were missed.
    assert behind.max_tss == fresh.max_tss
    assert behind.max_minutes == fresh.max_minutes


def test_week_already_full_limits_to_easy_spin() -> None:
    p = plan()
    sun = MON + dt.timedelta(days=6)
    x = prescribe(p, sun, week_tss_done=340)
    assert x and x.max_session == "recovery" and x.max_tss == MIN_SESSION_TSS


def test_no_prescription_outside_the_plan() -> None:
    assert prescribe(plan(), MON - dt.timedelta(days=1), week_tss_done=0) is None


def test_adaptation_only_lowers_the_prescription() -> None:
    p = plan()
    tue = MON + dt.timedelta(days=1)
    base = prescribe(p, tue, week_tss_done=0)
    eased = prescribe(
        p,
        tue,
        week_tss_done=0,
        adaptation=Adaptation(key_max_session="endurance", tss_factor=0.85),
    )
    assert base and eased
    assert eased.max_session == "endurance"
    assert eased.target_tss < base.target_tss
    # An adaptation can't raise anything: a "vo2max" cap on a tempo day stays tempo.
    same = prescribe(p, tue, week_tss_done=0, adaptation=Adaptation(key_max_session="vo2max"))
    assert same and same.max_session == "tempo"


def test_hold_progression_uses_last_weeks_target() -> None:
    p = plan()
    week2_tue = MON + dt.timedelta(days=8)
    ramped = prescribe(p, week2_tue, week_tss_done=0)
    held = prescribe(p, week2_tue, week_tss_done=0, adaptation=Adaptation(hold_progression=True))
    assert ramped and held
    assert ramped.week_tss_target == 324 and held.week_tss_target == 300


def test_ladder_helpers() -> None:
    assert lower("vo2max") == "threshold"
    assert lower("long_endurance") == "recovery"
    assert lower("recovery", 5) == "rest"
    assert allowed("long_endurance", "endurance") and not allowed("tempo", "endurance")


# --------------------------------------------------------------------------
# Adaptation
# --------------------------------------------------------------------------


def res(days_ago: int, session: str | None, **kw: Any) -> SessionResult:
    values: dict[str, Any] = {
        "date": MON - dt.timedelta(days=days_ago),
        "session_type": session,
        "completion_pct": 100.0,
        "power_deviation_pct": 0.0,
        "rpe": None,
        "decoupling_pct": None,
    }
    values.update(kw)
    return SessionResult(**values)


def test_completed_sessions_never_raise_anything() -> None:
    a = adapt(MON, [res(2, "threshold", rpe=6), res(4, "sweet_spot")], last_week_compliance=1.0)
    assert a.key_max_session is None and a.day_max_session is None
    assert a.tss_factor == 1.0 and not a.hold_progression


def test_struggled_key_session_lowers_next_key_days() -> None:
    a = adapt(MON, [res(2, "threshold", completion_pct=70.0)], last_week_compliance=None)
    assert a.key_max_session == "sweet_spot"
    assert any("completed 70 %" in r for r in a.reasons)


def test_two_struggled_key_sessions_cap_at_endurance_and_suggest_ftp_check() -> None:
    a = adapt(
        MON,
        [res(5, "sweet_spot", power_deviation_pct=-9.0), res(2, "threshold", rpe=9)],
        last_week_compliance=None,
    )
    assert a.key_max_session == "endurance"
    assert a.flags["ftp_check_suggested"] is True


def test_recent_hard_feeling_ride_reduces_budget() -> None:
    a = adapt(MON, [res(1, "endurance", rpe=7)], last_week_compliance=None)
    assert a.tss_factor < 1.0
    drift = adapt(MON, [res(2, "long_endurance", decoupling_pct=11.0)], last_week_compliance=None)
    assert drift.tss_factor < 1.0


def test_llm_recover_recommendation_caps_the_next_day() -> None:
    a = adapt(MON, [res(1, "endurance", recommendation="recover")], last_week_compliance=None)
    assert a.day_max_session == "recovery"
    older = adapt(MON, [res(3, "endurance", recommendation="recover")], last_week_compliance=None)
    assert older.day_max_session is None  # only the day after


def test_low_compliance_holds_progression() -> None:
    assert adapt(MON, [], last_week_compliance=0.5).hold_progression
    assert not adapt(MON, [], last_week_compliance=0.9).hold_progression


def test_future_results_are_ignored() -> None:
    a = adapt(MON, [res(-1, "threshold", completion_pct=10.0)], last_week_compliance=None)
    assert a.key_max_session is None


# --------------------------------------------------------------------------
# Ride evaluation
# --------------------------------------------------------------------------


def planned_workout() -> dict[str, Any]:
    w = build_workout(
        athlete_slug="a",
        workout_date=MON,
        name="2x10 sweet spot",
        description="",
        ftp_watts=200,
        objective=WorkoutObjective(
            session_type="sweet_spot", target_duration_minutes=45, primary_goal="", rationale=""
        ),
        steps=[
            StepInput(kind="warmup", duration_s=600, low_pct=50, high_pct=60),
            StepInput(kind="work", duration_s=600, low_pct=88, high_pct=92),
            StepInput(kind="recovery", duration_s=300, low_pct=50, high_pct=55),
            StepInput(kind="work", duration_s=600, low_pct=88, high_pct=92),
            StepInput(kind="cooldown", duration_s=600, low_pct=45, high_pct=55),
        ],
    )
    return w.model_dump(mode="json")


def ride_power(
    plan_json: dict[str, Any], scale: float = 1.0, stop_at: int | None = None
) -> list[float | None]:
    out: list[float | None] = []
    for s in plan_json["steps"]:
        mid = (s["low_watts"] + s["high_watts"]) / 2
        out.extend([mid * scale] * s["duration_s"])
    if stop_at is not None:
        out = out[:stop_at]
    return out


def test_ride_as_planned() -> None:
    pw = planned_workout()
    power = ride_power(pw)
    hr = [130.0] * len(power)
    ev = evaluate_ride(
        planned=pw,
        activity={"moving_time_s": len(power), "avg_power_watts": 130, "rpe": 6, "avg_hr_bpm": 130},
        power=power,
        heartrate=hr,
    )
    assert ev["outcome"] == "as_planned"
    assert ev["power"]["method"] == "stream"
    assert ev["power"]["completion_pct"] == 100.0
    assert ev["power"]["power_deviation_pct"] == 0.0
    assert ev["subjective"]["rpe_delta"] == 0.0
    assert len(ev["heart_rate"]["work_steps"]) == 2


def test_ride_under_target_or_hard_is_a_deviation() -> None:
    pw = planned_workout()
    under = evaluate_ride(
        planned=pw, activity={"rpe": 6}, power=ride_power(pw, 0.9), heartrate=None
    )
    assert under["outcome"] == "completed_with_deviation"
    assert under["power"]["power_deviation_pct"] == pytest.approx(-10.0, abs=0.1)
    hard = evaluate_ride(planned=pw, activity={"rpe": 9}, power=ride_power(pw), heartrate=None)
    assert hard["outcome"] == "completed_with_deviation"


def test_partial_and_abandoned_rides() -> None:
    pw = planned_workout()
    total = pw["total_s"]
    partial = evaluate_ride(
        planned=pw, activity={}, power=ride_power(pw, stop_at=int(total * 0.7)), heartrate=None
    )
    assert partial["outcome"] == "partial"
    abandoned = evaluate_ride(
        planned=pw, activity={}, power=ride_power(pw, stop_at=int(total * 0.3)), heartrate=None
    )
    assert abandoned["outcome"] == "abandoned"


def test_summary_method_without_power_stream() -> None:
    pw = planned_workout()
    ev = evaluate_ride(
        planned=pw,
        activity={"moving_time_s": pw["total_s"], "avg_power_watts": None, "avg_hr_bpm": 120},
        power=None,
        heartrate=None,
    )
    assert ev["power"]["method"] == "summary"
    assert ev["heart_rate"] == {"avg_hr_bpm": 120, "decoupling_pct": None}
    assert any("no power stream" in n for n in ev["notes"])
    assert any("no RPE" in n for n in ev["notes"])


def test_unplanned_ride() -> None:
    ev = evaluate_ride(planned=None, activity={"rpe": 4}, power=[150.0] * 600, heartrate=None)
    assert ev["outcome"] == "unplanned" and ev["power"] is None


def test_decoupling() -> None:
    power = [150.0] * 3600
    steady = [130.0] * 3600
    drifting = [125.0] * 1800 + [137.5] * 1800  # HR rises 10 % at the same power
    assert decoupling_pct(power, steady, 0, 3600) == 0.0
    assert decoupling_pct(power, drifting, 0, 3600) == pytest.approx(9.09, abs=0.01)
    assert decoupling_pct(power[:600], steady[:600], 0, 600) is None  # too short


def test_to_seconds_series_places_pauses() -> None:
    assert to_seconds_series([100, 110, 120], [0, 1, 4]) == [100.0, 110.0, None, None, 120.0]
    assert to_seconds_series([100, None], None) == [100.0, None]
    assert to_seconds_series(None) is None


# --------------------------------------------------------------------------
# Guidance and validator plan group
# --------------------------------------------------------------------------

READINESS = {"max_intensity": "vo2max", "caps": [], "status": "green"}


def test_guidance_combines_readiness_and_plan() -> None:
    tue = MON + dt.timedelta(days=1)
    g = build_guidance(
        plan_date=tue,
        today=MON,
        readiness={
            **READINESS,
            "max_intensity": "endurance",
            "caps": [{"max_intensity": "endurance", "reason": "HRV low"}],
        },
        plan=plan(),
        plan_name="P",
        recent_activities=[],
        recent_planned=[],
        results=[],
    )
    assert g["max_intensity"] == "endurance"  # readiness is stricter than the tempo plan
    assert g["max_session_type"] == "tempo"
    assert g["prescription"]["role"] == "key"
    mon = build_guidance(
        plan_date=MON,
        today=MON,
        readiness=READINESS,
        plan=plan(),
        plan_name="P",
        recent_activities=[],
        recent_planned=[],
        results=[],
    )
    assert mon["max_intensity"] == "rest"
    assert any("long-term plan" in c["reason"] for c in mon["caps"])


def test_guidance_without_plan_keeps_readiness() -> None:
    g = build_guidance(
        plan_date=MON,
        today=MON,
        readiness=READINESS,
        plan=None,
        plan_name=None,
        recent_activities=[],
        recent_planned=[],
        results=[],
    )
    assert g["max_intensity"] == "vo2max" and g["max_tss"] is None


def test_guidance_week_done_counts_rides_and_todays_plan() -> None:
    thu = MON + dt.timedelta(days=3)
    g = build_guidance(
        plan_date=thu,
        today=MON + dt.timedelta(days=2),
        readiness=READINESS,
        plan=plan(),
        plan_name="P",
        recent_activities=[{"date": (MON + dt.timedelta(days=1)).isoformat(), "tss": 60}],
        recent_planned=[
            {
                "date": (MON + dt.timedelta(days=2)).isoformat(),
                "session_type": "endurance",
                "estimated_tss": 40,
            }
        ],
        results=[],
    )
    assert g["week_tss_done"] == 100.0
    assert g["last_week_compliance"] is None  # the plan starts this week


def test_validator_plan_group() -> None:
    w = build_workout(
        athlete_slug="a",
        workout_date=MON,
        name="Tempo",
        description="",
        ftp_watts=200,
        objective=WorkoutObjective(
            session_type="tempo", target_duration_minutes=60, primary_goal="", rationale=""
        ),
        steps=[
            StepInput(kind="warmup", duration_s=600, low_pct=50, high_pct=60),
            StepInput(kind="work", duration_s=2400, low_pct=78, high_pct=84),
            StepInput(kind="cooldown", duration_s=600, low_pct=45, high_pct=55),
        ],
    )
    ok = validate_workout(
        w,
        ValidationContext(
            plan_date=MON,
            max_intensity=Intensity.VO2MAX,
            max_session_type="tempo",
            plan_max_tss=80,
            plan_max_minutes=70,
        ),
    )
    assert ok.checks["plan"] == "passed"
    bad = validate_workout(
        w,
        ValidationContext(
            plan_date=MON,
            max_intensity=Intensity.VO2MAX,
            max_session_type="endurance",
            plan_max_tss=30,
            plan_max_minutes=40,
        ),
    )
    assert bad.checks["plan"] == "failed"
    assert len([e for e in bad.errors if "plan" in e]) == 3
    # Plan limits round-trip through the stored validation context.
    ctx = ValidationContext(plan_date=MON, max_intensity=Intensity.TEMPO, plan_max_tss=50)
    assert ValidationContext.from_dict(ctx.as_dict()) == ctx


def test_monday_of() -> None:
    assert monday_of(dt.date(2026, 11, 15)) == MON
