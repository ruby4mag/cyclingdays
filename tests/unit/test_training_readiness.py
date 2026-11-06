"""Readiness rules: personal baselines, safety caps, missing data."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from typing import Any

import pytest

from app.training.intensity import Intensity
from app.training.metrics import DayMetrics
from app.training.periodization import PlanPhase
from app.training.readiness import (
    ReadinessInput,
    RecentSession,
    WellnessDay,
    assess_readiness,
    baseline,
)

DAY = dt.date(2026, 11, 5)


def history(days: int = 30, **today: Any) -> list[WellnessDay]:
    """Stable personal baseline: HRV ~50 ms, RHR ~50 bpm, sleep ~7.5 h."""
    out = [
        WellnessDay(
            date=DAY - dt.timedelta(days=i),
            hrv_rmssd_ms=50 + (3 if i % 2 else -3),
            resting_hr_bpm=50 + (2 if i % 2 else -2),
            sleep_s=27000 + (1200 if i % 2 else -1200),
        )
        for i in range(1, days + 1)
    ]
    out.append(replace(WellnessDay(DAY, 50.0, 50.0, 27000), **today))
    return out


def load(
    ctl: float = 60, atl: float = 60, authoritative: bool = True, ramp: float = 2
) -> DayMetrics:
    return DayMetrics(
        date=DAY - dt.timedelta(days=1),
        daily_tss=50,
        ctl=ctl,
        atl=atl,
        tsb=ctl - atl,
        rolling_7d_tss=400,
        ctl_ramp_7d=ramp,
        data_completeness=1.0,
        atl_completeness=1.0,
        known_history_days=120,
        ctl_authoritative=authoritative,
        atl_authoritative=True,
        reasons=() if authoritative else ("CTL: 30 of 96 required days of history",),
    )


def assess(**kwargs: object) -> object:
    data = ReadinessInput(**{"day": DAY, "wellness": history(), "load": load(), **kwargs})  # type: ignore[arg-type]
    return assess_readiness(data)


def factor(result: object, name: str) -> object:
    return next(f for f in result.factors if f.name == name)  # type: ignore[attr-defined]


def test_all_good_is_green_unrestricted_high_confidence() -> None:
    r = assess()
    assert r.status == "green"  # type: ignore[attr-defined]
    assert r.max_intensity == Intensity.VO2MAX  # type: ignore[attr-defined]
    assert r.confidence == "high"  # type: ignore[attr-defined]
    hrv = factor(r, "hrv")
    assert hrv.status == "ok" and hrv.baseline_n == 30  # type: ignore[attr-defined]
    assert hrv.baseline_mean == pytest.approx(math.sqrt(47 * 53), rel=1e-3)  # type: ignore[attr-defined]


def test_low_hrv_alone_caps_at_endurance() -> None:
    r = assess(wellness=history(hrv_rmssd_ms=38.0))
    assert factor(r, "hrv").status == "negative"  # type: ignore[attr-defined]
    assert r.max_intensity == Intensity.ENDURANCE and r.status == "amber"  # type: ignore[attr-defined]


def test_two_negative_signals_cap_at_recovery() -> None:
    r = assess(wellness=history(hrv_rmssd_ms=38.0, resting_hr_bpm=56.0))
    assert r.status == "red" and r.max_intensity == Intensity.RECOVERY  # type: ignore[attr-defined]


def test_baseline_is_personal_not_population() -> None:
    # 38 ms is normal for an athlete whose baseline is ~38 ms.
    low_baseline = [replace(w, hrv_rmssd_ms=w.hrv_rmssd_ms - 12) for w in history()]  # type: ignore[operator]
    low_baseline[-1] = replace(low_baseline[-1], hrv_rmssd_ms=38.0)
    r = assess(wellness=low_baseline)
    assert factor(r, "hrv").status == "ok"  # type: ignore[attr-defined]


def test_caution_caps_at_tempo() -> None:
    r = assess(wellness=history(sleep_s=27000 * 0.85))
    assert factor(r, "sleep").status == "caution"  # type: ignore[attr-defined]
    assert r.max_intensity == Intensity.TEMPO and r.status == "amber"  # type: ignore[attr-defined]


def test_suppressed_hrv_trend_is_caution() -> None:
    wellness = history()
    for i in range(0, 5):
        d = DAY - dt.timedelta(days=i)
        wellness = [replace(w, hrv_rmssd_ms=44.0) if w.date == d else w for w in wellness]
    r = assess(wellness=wellness)
    assert factor(r, "hrv_trend_7d").status == "caution"  # type: ignore[attr-defined]
    assert r.max_intensity <= Intensity.TEMPO  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"illness_reported": True}, Intensity.REST),
        ({"pain_reported": True}, Intensity.REST),
        ({"injury": 3}, Intensity.REST),
        ({"injury": 2}, Intensity.ENDURANCE),
        ({"fatigue": 4}, Intensity.ENDURANCE),
        ({"soreness": 3}, Intensity.TEMPO),
    ],
)
def test_safety_and_subjective_reports(change: dict[str, Any], expected: Intensity) -> None:
    r = assess(wellness=history(**change))
    assert r.max_intensity == expected  # type: ignore[attr-defined]


def test_illness_reported_yesterday_still_applies() -> None:
    wellness = history()
    wellness = [
        replace(w, illness_reported=True) if w.date == DAY - dt.timedelta(1) else w
        for w in wellness
    ]
    assert assess(wellness=wellness).max_intensity == Intensity.REST  # type: ignore[attr-defined]


def test_missing_recovery_data_is_never_assumed_fine() -> None:
    r = assess(wellness=[])
    assert r.max_intensity == Intensity.ENDURANCE  # type: ignore[attr-defined]
    assert r.confidence == "low" and r.status == "amber"  # type: ignore[attr-defined]
    assert {f.status for f in r.factors if f.name in ("hrv", "resting_hr", "sleep")} == {"missing"}  # type: ignore[attr-defined]


def test_stale_recovery_data_is_conservative() -> None:
    stale = [w for w in history() if w.date <= DAY - dt.timedelta(days=2)]
    r = assess(wellness=stale)
    assert factor(r, "hrv").status == "stale"  # type: ignore[attr-defined]
    assert r.max_intensity == Intensity.ENDURANCE and r.confidence == "low"  # type: ignore[attr-defined]


def test_insufficient_baseline_is_uncertain() -> None:
    r = assess(wellness=history(days=10))
    assert factor(r, "hrv").status == "insufficient_baseline"  # type: ignore[attr-defined]
    assert r.max_intensity == Intensity.ENDURANCE  # type: ignore[attr-defined]


def test_short_sleep_floor_applies_without_baseline() -> None:
    r = assess(wellness=history(days=5, sleep_s=4 * 3600))
    assert factor(r, "sleep").status == "negative"  # type: ignore[attr-defined]


def test_non_authoritative_load_caps_and_lowers_confidence() -> None:
    r = assess(load=load(authoritative=False))
    assert factor(r, "form").status == "not_authoritative"  # type: ignore[attr-defined]
    assert r.max_intensity == Intensity.ENDURANCE and r.confidence == "medium"  # type: ignore[attr-defined]
    assert assess(load=None).max_intensity == Intensity.ENDURANCE  # type: ignore[attr-defined]


def test_deep_fatigue_and_fast_ramp() -> None:
    assert factor(assess(load=load(ctl=60, atl=74)), "form").status == "caution"  # type: ignore[attr-defined]
    deep = assess(load=load(ctl=60, atl=85))
    assert factor(deep, "form").status == "negative"  # type: ignore[attr-defined]
    assert deep.max_intensity == Intensity.ENDURANCE  # type: ignore[attr-defined]
    assert factor(assess(load=load(ramp=9)), "ctl_ramp_7d").status == "caution"  # type: ignore[attr-defined]
    low_ctl = assess(load=load(ctl=10, atl=35))
    assert factor(low_ctl, "form").status == "caution"  # type: ignore[attr-defined]


def test_recent_hard_sessions() -> None:
    yesterday = DAY - dt.timedelta(days=1)
    hard_yesterday = [RecentSession(yesterday, 0.9, 80)]
    assert assess(recent_sessions=hard_yesterday).max_intensity == Intensity.ENDURANCE  # type: ignore[attr-defined]
    two_hard = [RecentSession(yesterday, 0.7, 200), RecentSession(DAY - dt.timedelta(3), 0.95, 90)]
    assert assess(recent_sessions=two_hard).max_intensity == Intensity.RECOVERY  # type: ignore[attr-defined]
    easy = [RecentSession(yesterday, 0.6, 50)]
    assert assess(recent_sessions=easy).max_intensity == Intensity.VO2MAX  # type: ignore[attr-defined]


def test_plan_phase_caps_intensity_but_not_status() -> None:
    recovery = PlanPhase(name="R", type="recovery", start_date=DAY, end_date=DAY)
    r = assess(plan_phase=recovery, has_active_plan=True)
    assert r.max_intensity == Intensity.ENDURANCE and r.status == "green"  # type: ignore[attr-defined]
    assert any("recovery phase" in c.reason for c in r.caps)  # type: ignore[attr-defined]
    assert "no active long-term plan" in assess().notes  # type: ignore[attr-defined]


def test_serialisable_and_deterministic() -> None:
    a = assess(wellness=history(hrv_rmssd_ms=38.0)).as_dict()  # type: ignore[attr-defined]
    b = assess(wellness=history(hrv_rmssd_ms=38.0)).as_dict()  # type: ignore[attr-defined]
    assert a == b
    assert a["max_intensity"] == "endurance"
    assert all(isinstance(c["max_intensity"], str) for c in a["caps"])


def test_baseline_helper() -> None:
    assert baseline([1.0] * 20) is None  # zero variance
    assert baseline([1.0, 2.0]) is None  # too few values
    b = baseline([float(i) for i in range(20)])
    assert b is not None and b.n == 20 and b.mean == pytest.approx(9.5)
