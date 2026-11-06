"""Planned-versus-actual evaluation and plan phases."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.training.intensity import Intensity, cap
from app.training.performance import PlannedStep, evaluate_workout
from app.training.periodization import PlanPhases

PLAN = [
    PlannedStep(600, 90, 110, "warm-up"),
    PlannedStep(300, 170, 180, "interval"),
    PlannedStep(300, 100, 110, "recovery"),
    PlannedStep(300, None, None, "free"),
]


def test_perfect_execution() -> None:
    samples = [100.0] * 600 + [175.0] * 300 + [105.0] * 300 + [120.0] * 300
    ev = evaluate_workout(PLAN, samples)
    assert ev.completion_pct == pytest.approx(100.0)
    assert ev.power_deviation_pct == pytest.approx(0.0)
    assert ev.work_completion_pct == pytest.approx(100.0)
    assert ev.completed_steps == 4
    assert ev.steps[1].in_range_fraction == 1.0
    assert ev.steps[3].deviation_pct is None  # free ride: no target


def test_under_target_and_stopped_early() -> None:
    samples = [100.0] * 600 + [157.5] * 300 + [105.0] * 120
    ev = evaluate_workout(PLAN, samples)
    interval = ev.steps[1]
    assert interval.status == "under_target"
    assert interval.deviation_pct == pytest.approx(-10.0)
    assert ev.steps[2].status == "incomplete"
    assert ev.steps[3].status == "not_started"
    assert ev.completion_pct == pytest.approx(1020 / 1500 * 100)
    # weighted by planned duration over steps with targets and data
    expected = (0 * 600 + -10 * 300 + 0 * 300) / 1200
    assert ev.power_deviation_pct == pytest.approx(expected)


def test_dropouts_and_extra_riding() -> None:
    samples: list[float | None] = [100.0] * 600 + [None] * 300 + [105.0] * 600 + [100.0] * 60
    ev = evaluate_workout(PLAN, samples)
    assert ev.steps[1].status == "not_started"
    assert ev.recorded_s == 1200
    assert ev.extra_recorded_s == 60


def test_start_offset_and_over_target() -> None:
    samples = [0.0] * 30 + [100.0] * 600 + [195.0] * 300
    ev = evaluate_workout(PLAN[:2], samples, start_offset_s=30)
    assert ev.steps[1].status == "over_target"
    assert ev.steps[1].deviation_pct == pytest.approx(11.43, abs=0.01)


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        PlannedStep(0)
    with pytest.raises(ValueError):
        PlannedStep(60, 100, None)
    with pytest.raises(ValueError):
        PlannedStep(60, 200, 100)
    with pytest.raises(ValueError):
        evaluate_workout([], [])
    with pytest.raises(ValueError):
        evaluate_workout(PLAN, [-1.0])
    no_targets = evaluate_workout([PlannedStep(60)], [100.0] * 60)
    assert no_targets.power_deviation_pct is None and no_targets.work_completion_pct is None


def test_plan_phases() -> None:
    plan = PlanPhases.from_json(
        {
            "phases": [
                {
                    "name": "Base 1",
                    "type": "base",
                    "start_date": "2026-11-01",
                    "end_date": "2026-11-21",
                    "weekly_tss_target": 350,
                },
                {
                    "name": "Recovery",
                    "type": "recovery",
                    "start_date": "2026-11-22",
                    "end_date": "2026-11-28",
                },
            ]
        }
    )
    assert plan.phase_for(dt.date(2026, 11, 5)).name == "Base 1"  # type: ignore[union-attr]
    recovery = plan.phase_for(dt.date(2026, 11, 25))
    assert recovery is not None and recovery.intensity_cap == Intensity.ENDURANCE
    assert plan.phase_for(dt.date(2026, 12, 1)) is None
    assert PlanPhases.from_json(None).phases == []


@pytest.mark.parametrize(
    "phases",
    [
        [{"name": "a", "type": "base", "start_date": "2026-11-10", "end_date": "2026-11-01"}],
        [
            {"name": "a", "type": "base", "start_date": "2026-11-01", "end_date": "2026-11-10"},
            {"name": "b", "type": "build", "start_date": "2026-11-10", "end_date": "2026-11-20"},
        ],
        [{"name": "a", "type": "sprint", "start_date": "2026-11-01", "end_date": "2026-11-10"}],
    ],
)
def test_invalid_plan_phases(phases: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError):
        PlanPhases.from_json({"phases": phases})


def test_intensity_cap() -> None:
    assert cap(Intensity.THRESHOLD, Intensity.ENDURANCE) == Intensity.ENDURANCE
    assert cap() == Intensity.VO2MAX
    assert Intensity.TEMPO.label == "tempo"
