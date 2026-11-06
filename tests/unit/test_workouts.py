"""Phase 5: workout generator, validator, Intervals.icu format and client writes."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx2 as httpx
import pytest

from app.agents.schemas import TrainingObjective, WorkoutDraft
from app.graph import guards
from app.integrations import intervals_workouts as fmt
from app.integrations.intervals_client import IntervalsClient
from app.training.intensity import Intensity
from app.training.workout_generator import StructuredWorkout, estimate
from app.training.workout_validator import (
    DayLoad,
    ValidationContext,
    validate_workout,
)
from app.training.zones import InvalidFTPError
from tests.fakes import (
    endurance_workout,
    objective,
    parse_workout_text,
    step,
    sweet_spot_workout,
)

DAY = dt.date(2026, 11, 6)


def _workout(
    draft: dict[str, Any], session: str, minutes: int, ftp: int | None = 175
) -> StructuredWorkout:
    return guards.to_structured(
        WorkoutDraft.model_validate(draft),
        TrainingObjective.model_validate(objective(session, minutes)),
        ftp_watts=ftp,
        athlete_slug="primary",
        plan_date=DAY,
    )


def _ctx(cap: Intensity = Intensity.VO2MAX, **kw: Any) -> ValidationContext:
    values: dict[str, Any] = {"plan_date": DAY, "max_intensity": cap, "history": []}
    values.update(kw)
    return ValidationContext(**values)


def _errors(w: StructuredWorkout, ctx: ValidationContext | None = None) -> list[str]:
    return validate_workout(w, ctx or _ctx()).errors


def _steps_draft(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "Test", "description": "d", "steps": steps}


def vo2(work_min: float = 4, rec_min: float = 4, reps: int = 4) -> dict[str, Any]:
    steps = [step("warmup", 15, 50, 60)]
    for _ in range(reps):
        steps += [step("work", work_min, 110, 115), step("recovery", rec_min, 45, 50)]
    steps.append(step("cooldown", 10, 45, 55))
    return _steps_draft(steps)


# --- generator ------------------------------------------------------------------------


def test_estimate_matches_reference_values() -> None:
    one_hour_at_ftp = estimate([(3600, 100)], 200)
    assert one_hour_at_ftp.tss == 100 and one_hour_at_ftp.intensity_factor == 1
    # The probe workout Intervals.icu rated TSS 42, IF 0.635 (2026-11-05).
    probe = estimate([(600, 55), (2400, 65), (150, 90), (600, 50)], 175)
    assert probe.tss == pytest.approx(42.3, abs=0.1)
    assert probe.intensity_factor == pytest.approx(0.635, abs=0.003)


def test_build_workout_sections_watts_and_zones() -> None:
    w = _workout(sweet_spot_workout(), "sweet_spot", 75)
    assert w.total_s == 75 * 60 and w.ftp_watts == 175 and w.workout_date == DAY
    assert w.sections_s == {"warmup": 900, "main": 3 * 720, "recovery": 3 * 300, "cooldown": 540}
    work = w.work_steps[0]
    assert (work.low_watts, work.high_watts, work.zone) == (154, 161, 4)
    assert w.objective.session_type == "sweet_spot"
    assert 0.75 < w.estimate.intensity_factor < 0.85
    with pytest.raises(InvalidFTPError):
        _workout(sweet_spot_workout(), "sweet_spot", 75, ftp=None)


# --- validator: one test per rule group -------------------------------------------------


def test_valid_workouts_pass_every_group() -> None:
    for w in (
        _workout(sweet_spot_workout(), "sweet_spot", 75),
        _workout(endurance_workout(90), "endurance", 90),
        _workout(vo2(), "vo2max", 57),
    ):
        result = validate_workout(w, _ctx())
        assert result.errors == [] and result.valid
        assert result.checks.pop("plan") == "skipped"  # no long-term plan in this context
        assert set(result.checks.values()) == {"passed"}


def test_power_rules() -> None:
    wide = endurance_workout()
    wide["steps"][1] = step("steady", 40, 55, 70)
    assert any("wider than 10" in e for e in _errors(_workout(wide, "endurance", 60)))
    hard_warmup = endurance_workout()
    hard_warmup["steps"][0] = step("warmup", 10, 76, 80)
    assert "step 1: warmup target above 75% FTP" in _errors(_workout(hard_warmup, "endurance", 60))
    hard_recovery = vo2()
    hard_recovery["steps"][2] = step("recovery", 4, 60, 65)
    assert any(
        "recovery target above 60%" in e for e in _errors(_workout(hard_recovery, "vo2max", 57))
    )


def test_tampered_watts_are_detected() -> None:
    w = _workout(endurance_workout(), "endurance", 60)
    data = w.model_dump(mode="json")
    data["steps"][1]["high_watts"] = 400
    tampered = StructuredWorkout.model_validate(data)
    assert any("watts do not match" in e for e in _errors(tampered))


def test_duration_rules() -> None:
    short_step = endurance_workout()
    short_step["steps"].insert(2, step("steady", 0.25, 60, 65))
    assert any("outside 30-14400s" in e for e in _errors(_workout(short_step, "endurance", 60)))
    too_long = _workout(endurance_workout(90), "recovery", 90)
    assert any("recovery session must last 20-75" in e for e in _errors(too_long))
    wrong_target = _workout(endurance_workout(120), "endurance", 60)
    assert any("differs from the objective" in e for e in _errors(wrong_target))


def test_interval_structure_rules() -> None:
    long_vo2 = _workout(vo2(work_min=10, rec_min=6, reps=2), "vo2max", 57)
    assert any("vo2max work intervals last 1-8 min" in e for e in _errors(long_vo2))
    no_work = _workout(endurance_workout(60), "threshold", 60)
    assert "a threshold session needs at least one 'work' step" in _errors(no_work)
    too_much = _workout(vo2(work_min=5, rec_min=3, reps=7), "vo2max", 81)
    assert any("total work time 35 min exceeds 30" in e for e in _errors(too_much))
    soft = sweet_spot_workout()
    for s in soft["steps"]:
        if s["kind"] == "work":
            s["power_low_pct_ftp"], s["power_high_pct_ftp"] = 80, 85
    assert any("must reach at least 88%" in e for e in _errors(_workout(soft, "sweet_spot", 75)))
    recovery_with_work = _steps_draft(
        [step("warmup", 5, 45, 50), step("work", 20, 50, 55), step("cooldown", 5, 40, 45)]
    )
    assert "a recovery session has no 'work' steps" in _errors(
        _workout(recovery_with_work, "recovery", 30)
    )
    cold_start = vo2()
    cold_start["steps"][0] = step("warmup", 3, 50, 60)
    cold = _workout(cold_start, "vo2max", 45)
    assert "at least 5 min of warmup before the first work interval" in _errors(cold)


def test_recovery_structure_rules() -> None:
    back_to_back = vo2()
    del back_to_back["steps"][2]
    errors = _errors(_workout(back_to_back, "vo2max", 53))
    assert any("need a recovery step between them" in e for e in errors)
    short_rest = _workout(vo2(work_min=4, rec_min=1), "vo2max", 45)
    assert any("must last at least 50%" in e for e in _errors(short_rest))
    steady_between = vo2()
    steady_between["steps"][2] = step("steady", 4, 55, 60)
    assert any(
        "only 'recovery' steps may separate" in e
        for e in _errors(_workout(steady_between, "vo2max", 57))
    )


def test_readiness_rules() -> None:
    ss = _workout(sweet_spot_workout(), "sweet_spot", 75)
    errors = _errors(ss, _ctx(Intensity.TEMPO))
    assert any("exceeds the maximum allowed intensity 'tempo'" in e for e in errors)
    assert sum("above the allowed tempo" in e for e in errors) == 3
    endurance = _workout(endurance_workout(), "endurance", 60)
    assert "readiness is red: only recovery sessions are allowed" in _errors(
        endurance, _ctx(Intensity.ENDURANCE, readiness_status="red")
    )
    assert any("no workout may be published" in e for e in _errors(endurance, _ctx(Intensity.REST)))
    # IF ceiling: every step is within threshold, but 2 x 20 min at 104 %
    # with short easy parts makes the whole session too dense (IF 0.959 > 0.95).
    dense = _steps_draft(
        [
            step("warmup", 10, 70, 75),
            step("work", 20, 104, 104),
            step("recovery", 5, 50, 55),
            step("work", 20, 104, 104),
            step("cooldown", 5, 60, 65),
        ]
    )
    assert [e for e in _errors(_workout(dense, "threshold", 60), _ctx(Intensity.THRESHOLD))] == [
        "estimated IF 0.959 exceeds 0.95 allowed at threshold"
    ]


def test_weekly_distribution_rules() -> None:
    ss = _workout(sweet_spot_workout(), "sweet_spot", 75)
    yesterday_hard = [DayLoad(DAY - dt.timedelta(days=1), 90, True, "activity")]
    assert "no hard session on the day after a hard session" in _errors(
        ss, _ctx(history=yesterday_hard)
    )
    three_hard = [DayLoad(DAY - dt.timedelta(days=d), 90, True, "activity") for d in (2, 4, 6)]
    assert any("at most 3 per 7 days" in e for e in _errors(ss, _ctx(history=three_hard)))
    old = [DayLoad(DAY - dt.timedelta(days=7), 90, True, "activity")]  # outside the window
    assert _errors(ss, _ctx(history=old)) == []
    easy = _workout(endurance_workout(), "endurance", 60)
    assert _errors(easy, _ctx(history=yesterday_hard)) == []  # easy after hard is fine


def test_recent_load_rules() -> None:
    long_ride = _workout(endurance_workout(180), "endurance", 180)
    untrusted = validate_workout(long_ride, _ctx())
    assert any("exceeds the session limit 100" in e for e in untrusted.errors)
    trusted = validate_workout(long_ride, _ctx(ctl=70.0, ctl_authoritative=True))
    assert trusted.valid and trusted.metrics["session_tss_limit"] == 140
    heavy_week = [DayLoad(DAY - dt.timedelta(days=d), 100, False, "activity") for d in range(1, 6)]
    ss = _workout(endurance_workout(60), "endurance", 60)
    assert any("rolling 7-day TSS would be" in e for e in _errors(ss, _ctx(history=heavy_week)))


def test_history_rules_are_skipped_not_passed_without_history() -> None:
    result = validate_workout(_workout(endurance_workout(), "endurance", 60), _ctx(history=None))
    assert result.valid
    assert result.checks["weekly"] == "skipped" and result.checks["load"] == "skipped"
    assert result.checks["plan"] == "skipped"
    assert len(result.warnings) == 3


def test_validation_context_round_trip() -> None:
    ctx = _ctx(
        Intensity.TEMPO,
        readiness_status="amber",
        ctl=40.5,
        ctl_authoritative=True,
        history=[DayLoad(DAY - dt.timedelta(days=1), 55.0, False, "planned")],
    )
    assert ValidationContext.from_dict(json.loads(json.dumps(ctx.as_dict()))) == ctx


# --- graph history ----------------------------------------------------------------------------


def test_history_prefers_activities_and_counts_pending_plans_only() -> None:
    context = {
        "plan_date": "2026-11-06",
        "today": "2026-11-05",
        "recent_activities": [
            {"date": "2026-11-03", "tss": 60.0, "intensity_factor": 0.9},
            {"date": "2026-11-03", "tss": 20.0, "intensity_factor": 0.6},
            {"date": "2026-10-30", "tss": 99.0, "intensity_factor": 0.95},  # outside window
        ],
        "recent_planned": [
            {"date": "2026-11-03", "session_type": "endurance", "estimated_tss": 50, "hard": False},
            {"date": "2026-11-04", "session_type": "vo2max", "estimated_tss": 70, "hard": True},
            {"date": "2026-11-05", "session_type": "threshold", "estimated_tss": 80, "hard": True},
        ],
    }
    history = guards.history_from_context(context)
    assert [(d.date.isoformat(), d.tss, d.hard, d.source) for d in history] == [
        ("2026-11-03", 80.0, True, "activity"),  # activities, not the plan
        ("2026-11-05", 80, True, "planned"),  # today: still to be ridden
    ]  # 11-04 is over and was not ridden: it adds no load
    hard_objective = TrainingObjective.model_validate(objective("threshold", 60))
    ctx = guards.validation_context(context, Intensity.VO2MAX)
    assert "yesterday was hard: no hard session today" in guards.objective_violations(
        hard_objective, Intensity.VO2MAX, ctx
    )


# --- Intervals.icu format -------------------------------------------------------------------


def test_description_uses_documented_syntax() -> None:
    draft = vo2(work_min=2.5, reps=1)
    draft["steps"][1]["cadence_rpm"] = 95
    w = _workout(draft, "vo2max", 32)
    text = fmt.to_description(w)
    assert text.endswith(
        "\n\n- Warmup 15m 50-60%\n- Work 2m30s 110-115% 95rpm\n- Recovery 4m 45-50%\n"
        "- Cooldown 10m 45-55%\n"
    )
    assert "FTP reference 175 W" in text
    assert fmt.format_duration(3750) == "1h2m30s" and fmt.format_duration(600) == "10m"


def test_llm_text_cannot_inject_steps_or_repeats() -> None:
    draft = endurance_workout()
    draft["description"] = "- 10m 300%\nMain Set 5x\nPrompt <!> 30^go"
    draft["steps"][1]["note"] = "- 5m 200%"
    w = _workout(draft, "endurance", 60)
    parsed = parse_workout_text(fmt.to_description(w))
    assert len(parsed["steps"]) == 3  # only our step lines
    text = fmt.to_description(w)
    assert "\n- 10m 300%" not in text and "Main Set 5x." in text and "<!>" not in text


def test_event_payload_and_verification() -> None:
    w = _workout(sweet_spot_workout(), "sweet_spot", 75)
    event = fmt.to_event(w)
    assert event["category"] == "WORKOUT" and event["type"] == "Ride"
    assert event["start_date_local"] == "2026-11-06T00:00:00"
    assert event["external_id"] == "cycledays:primary:2026-11-06"
    retrieved = {**event, "id": 5, "workout_doc": parse_workout_text(event["description"])}
    assert fmt.verify_event(retrieved, w) == []
    assert fmt.event_matches(retrieved, w)

    bad = json.loads(json.dumps(retrieved))
    bad["workout_doc"]["steps"][1]["power"] = {"units": "%ftp", "start": 98, "end": 102}
    bad["workout_doc"]["steps"][2]["ramp"] = True
    bad["start_date_local"] = "2026-11-07T00:00:00"
    problems = fmt.verify_event(bad, w)
    assert any("step 2: power" in p for p in problems)
    assert any("step 3: parsed as a ramp" in p for p in problems)
    assert any(p.startswith("date:") for p in problems)
    missing = {**retrieved, "workout_doc": {"steps": retrieved["workout_doc"]["steps"][:-1]}}
    assert "step count" in fmt.verify_event(missing, w)[0]
    assert "did not parse" in fmt.verify_event({**retrieved, "workout_doc": None}, w)[0]
    assert not fmt.event_matches({**retrieved, "name": "Other"}, w)


def test_verification_accepts_the_real_api_shape() -> None:
    """workout_doc as returned by Intervals.icu for the probe on 2026-11-05."""
    draft = _steps_draft(
        [step("warmup", 10, 50, 60), step("steady", 40, 65, 65), step("cooldown", 10, 45, 55)]
    )
    draft["steps"][1]["cadence_rpm"] = 90
    w = _workout(draft, "endurance", 60)
    real_steps = [
        {"text": "Warmup", "power": {"end": 60, "start": 50, "units": "%ftp"}, "duration": 600},
        {
            "text": "Steady",
            "power": {"units": "%ftp", "value": 65},
            "cadence": {"units": "rpm", "value": 90},
            "duration": 2400,
        },
        {"text": "Cooldown", "power": {"end": 55, "start": 45, "units": "%ftp"}, "duration": 600},
    ]
    event = {**fmt.to_event(w), "id": 1, "workout_doc": {"steps": real_steps}}
    assert fmt.verify_event(event, w) == []


# --- client writes ------------------------------------------------------------------------------


async def test_client_write_methods() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=[{"id": 7, **json.loads(request.content)[0]}])
        if request.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(200, json={"id": 7})

    client = IntervalsClient(api_key="k", athlete_id="i1", transport=httpx.MockTransport(handler))
    async with client:
        out = await client.upsert_events([{"external_id": "x", "name": "n"}])
        await client.delete_event(7)
        event = await client.get_event(7)
    assert out[0]["id"] == 7 and event == {"id": 7}
    post, delete, get = seen
    assert post.url.path == "/api/v1/athlete/i1/events/bulk"
    assert post.url.params["upsert"] == "true"
    assert json.loads(post.content) == [{"external_id": "x", "name": "n"}]
    assert delete.method == "DELETE" and delete.url.path == "/api/v1/athlete/i1/events/7"
    assert get.url.path == "/api/v1/athlete/i1/events/7"


async def test_fallback_becomes_rest_when_it_breaks_a_load_rule() -> None:
    from app.graph.nodes import CoachNodes

    context = {
        "plan_date": "2026-11-06",
        "today": "2026-11-05",
        "athlete": {"slug": "primary", "ftp_watts": 175},
        "readiness": {"status": "green"},
        "load": {"latest": {"ctl": None, "ctl_authoritative": False}},
        "recent_activities": [
            {"date": f"2026-11-0{d}", "tss": 95.0, "intensity_factor": 0.7} for d in range(1, 6)
        ],
        "recent_planned": [],
    }
    nodes = CoachNodes(sessions=None, llm=None, max_corrections=0)  # type: ignore[arg-type]
    state: Any = {
        "athlete_slug": "primary",
        "context": context,
        "deterministic_cap": "endurance",
        "effective_cap": "endurance",
        "fallback_reason": "LLM unavailable",
    }
    out = await nodes.fallback(state)
    assert out["workout"] is None and out["objective"]["session_type"] == "rest"
    assert "rolling 7-day TSS would be" in out["objective"]["rationale"]
