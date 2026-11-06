"""Deterministic training calculations against hand-computed reference values."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from app.training import metrics as m
from app.training.zones import InvalidFTPError, power_zones, validate_ftp, zone_number

D0 = dt.date(2026, 1, 1)


# --- zones -------------------------------------------------------------------


def test_zone_table_for_175w() -> None:
    zones = power_zones(175)
    assert [z.number for z in zones] == [1, 2, 3, 4, 5, 6, 7]
    assert zones[1].lower_watts == pytest.approx(96.25)  # 55 %
    assert zones[1].upper_watts == pytest.approx(131.25)  # 75 %
    assert zones[3].upper_watts == pytest.approx(183.75)  # 105 %
    assert zones[6].upper_watts is None
    # Contiguous: each zone starts where the previous one ends.
    for a, b in itertools.pairwise(zones):
        assert a.upper_watts == b.lower_watts


@pytest.mark.parametrize(
    ("watts", "zone"),
    [(0, 1), (96.24, 1), (96.25, 2), (131.25, 3), (175, 4), (183.75, 5), (262.5, 7), (2000, 7)],
)
def test_zone_boundaries_belong_to_upper_zone(watts: float, zone: int) -> None:
    assert zone_number(watts, 175) == zone


def test_boundary_classification_matches_zone_table() -> None:
    for ftp in (150, 175, 199, 263):
        for z in power_zones(ftp):
            assert zone_number(z.lower_watts, ftp) == z.number


@pytest.mark.parametrize("ftp", [None, 0, -10, 601, True])
def test_invalid_ftp_rejected(ftp: object) -> None:
    with pytest.raises(InvalidFTPError):
        validate_ftp(ftp)  # type: ignore[arg-type]
    with pytest.raises(InvalidFTPError):
        power_zones(ftp)  # type: ignore[arg-type]


def test_negative_power_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        zone_number(-1, 175)


# --- NP / IF / TSS -------------------------------------------------------------


def test_np_of_constant_power_equals_power() -> None:
    summary = m.normalized_power([200.0] * 3600)
    assert summary.normalized_watts == pytest.approx(200.0)
    assert summary.average_watts == pytest.approx(200.0)
    assert summary.np_reliable


def test_np_of_square_wave_matches_closed_form() -> None:
    # 30 s @ 300 W / 30 s @ 100 W: the 30-s rolling mean is a triangle wave
    # uniformly covering 100..300 W -> NP = (E[x^4])^(1/4)
    # E[x^4] = (300^5 - 100^5) / (5 * 200) = 2.42e9 -> NP ~ 221.8 W.
    samples = ([300.0] * 30 + [100.0] * 30) * 60
    summary = m.normalized_power(samples)
    assert summary.average_watts == pytest.approx(200.0)
    assert summary.normalized_watts == pytest.approx(2.42e9**0.25, rel=0.005)


def test_np_handles_missing_data_explicitly() -> None:
    few_gaps = [200.0] * 1000 + [None] * 50
    assert m.normalized_power(few_gaps).normalized_watts is not None
    many_gaps = [200.0] * 800 + [None] * 200
    summary = m.normalized_power(many_gaps)
    assert summary.normalized_watts is None
    assert summary.average_watts == pytest.approx(200.0)
    assert "missing" in summary.notes[0]
    assert m.normalized_power([]).normalized_watts is None
    assert m.normalized_power([None] * 100).average_watts is None
    short = m.normalized_power([200.0] * 600)
    assert short.normalized_watts == pytest.approx(200.0) and not short.np_reliable


def test_np_rejects_implausible_samples() -> None:
    with pytest.raises(ValueError):
        m.normalized_power([200.0, -5.0])
    with pytest.raises(ValueError):
        m.normalized_power([200.0, 5000.0])


def test_tss_and_if_reference_values() -> None:
    assert m.training_stress_score(3600, 175, 175) == pytest.approx(100.0)
    assert m.training_stress_score(7200, 131.25, 175) == pytest.approx(112.5)  # 2 h @ IF .75
    assert m.intensity_factor(140, 175) == pytest.approx(0.8)
    with pytest.raises(InvalidFTPError):
        m.training_stress_score(3600, 175, None)


def test_activity_load_prefers_power_and_activity_ftp() -> None:
    load = m.activity_load(
        duration_s=3600,
        normalized_watts=180,
        ftp_at_activity=180,
        profile_ftp=175,
        source_load=40,
    )
    assert load.method == "power" and load.tss == pytest.approx(100.0)
    assert load.ftp_used == 180


def test_activity_load_fallbacks() -> None:
    profile = m.activity_load(
        duration_s=3600,
        normalized_watts=175,
        ftp_at_activity=None,
        profile_ftp=175,
        source_load=None,
    )
    assert profile.method == "power" and any("profile FTP" in n for n in profile.notes)
    source = m.activity_load(
        duration_s=3600,
        normalized_watts=None,
        ftp_at_activity=175,
        profile_ftp=175,
        source_load=55,
    )
    assert source.method == "source_load" and source.tss == 55
    unknown = m.activity_load(
        duration_s=3600,
        normalized_watts=None,
        ftp_at_activity=None,
        profile_ftp=None,
        source_load=None,
    )
    assert unknown.method == "none" and unknown.tss is None and not unknown.known


def test_activity_load_rejects_implausible_intensity() -> None:
    load = m.activity_load(
        duration_s=7200,
        normalized_watts=300,
        ftp_at_activity=175,
        profile_ftp=175,
        source_load=90,
    )
    assert load.method == "source_load"
    assert any("implausible" in n for n in load.notes)


# --- CTL / ATL / TSB -----------------------------------------------------------


def _loads(tss_by_offset: dict[int, float | None]) -> dict[dt.date, list[m.ActivityLoad]]:
    out: dict[dt.date, list[m.ActivityLoad]] = {}
    for offset, tss in tss_by_offset.items():
        method: m.LoadMethod = "power" if tss is not None else "none"
        out[D0 + dt.timedelta(offset)] = [m.ActivityLoad(tss, method, None, None)]
    return out


def test_min_history_days() -> None:
    assert m.CTL_MIN_HISTORY_DAYS == 96
    assert m.ATL_MIN_HISTORY_DAYS == 15


def test_ctl_atl_step_response_closed_form() -> None:
    days = m.daily_loads(_loads({i: 100.0 for i in range(120)}), D0, D0 + dt.timedelta(119), D0)
    series = m.pmc(days)
    for n in (1, 7, 42, 120):
        assert series[n - 1].ctl == pytest.approx(100 * (1 - (41 / 42) ** n))
        assert series[n - 1].atl == pytest.approx(100 * (1 - (6 / 7) ** n))
        assert series[n - 1].tsb == pytest.approx(series[n - 1].ctl - series[n - 1].atl)
    assert series[6].rolling_7d_tss == 700
    assert series[7].ctl_ramp_7d == pytest.approx(series[7].ctl - series[0].ctl)


def test_authoritative_only_with_enough_complete_history() -> None:
    days = m.daily_loads(
        _loads({i: 50.0 for i in range(0, 120, 2)}), D0, D0 + dt.timedelta(119), D0
    )
    series = m.pmc(days)
    assert not series[94].ctl_authoritative  # 95 days of history
    assert series[95].ctl_authoritative  # 96 days
    assert not series[13].atl_authoritative and series[14].atl_authoritative
    assert any("CTL: 95 of 96" in r for r in series[94].reasons)


def test_unknown_days_reduce_completeness_and_authority() -> None:
    loads = _loads({i: 60.0 for i in range(150)})
    for i in range(100, 105):  # five activities without usable load
        loads[D0 + dt.timedelta(i)] = [m.ActivityLoad(None, "none", None, None)]
    series = m.pmc(m.daily_loads(loads, D0, D0 + dt.timedelta(149), D0))
    day = series[104]
    assert day.daily_tss is None
    assert day.data_completeness == pytest.approx(37 / 42)
    assert not day.ctl_authoritative and not day.atl_authoritative
    # >= 90 % of 42 days must be known, i.e. at most 4 unknown days in the window.
    assert not series[141].ctl_authoritative  # window 100..141: 5 unknown days
    assert series[142].ctl_authoritative  # window 101..142: 4 unknown days


def test_days_before_coverage_are_unknown() -> None:
    coverage = D0 + dt.timedelta(10)
    days = m.daily_loads(_loads({0: 80.0, 20: 80.0}), D0, D0 + dt.timedelta(30), coverage)
    assert days[0].tss is None and not days[0].covered  # activity known, day not covered
    assert days[5].tss is None  # uncovered empty day is unknown, not rest
    assert days[15].tss == 0.0 and days[15].covered  # covered empty day = rest
    assert m.pmc(days)[30].known_history_days == 21


def test_window_without_any_activity_is_not_authoritative() -> None:
    days = m.daily_loads({}, D0, D0 + dt.timedelta(199), D0)
    last = m.pmc(days)[-1]
    assert last.ctl == 0 and last.data_completeness == 1.0
    assert not last.ctl_authoritative
    assert any("missing uploads" in r for r in last.reasons)


def test_pmc_is_reproducible_and_requires_consecutive_days() -> None:
    days = m.daily_loads(
        _loads({i: float(i % 5 * 20) for i in range(100)}), D0, D0 + dt.timedelta(99), D0
    )
    assert m.pmc(days) == m.pmc(list(days))
    with pytest.raises(ValueError, match="consecutive"):
        m.pmc([days[0], days[2]])


def test_weekly_loads_use_iso_weeks() -> None:
    start = dt.date(2026, 11, 2)  # Monday
    days = m.daily_loads(
        _loads({(start - D0).days + i: 50.0 for i in range(10)}),
        start,
        start + dt.timedelta(13),
        start,
    )
    weeks = m.weekly_loads(days)
    assert [w.week_start for w in weeks] == [start, start + dt.timedelta(7)]
    assert weeks[0].tss == 350 and weeks[0].complete
    assert weeks[1].tss == 150 and weeks[1].activity_count == 3


# --- power distribution ---------------------------------------------------------


def test_time_in_zones() -> None:
    samples: list[float | None] = [100.0] * 60 + [140.0] * 30 + [None] * 10 + [300.0] * 5
    dist = m.time_in_zones(samples, 175)
    assert dist.seconds_by_zone[2] == 60 and dist.seconds_by_zone[3] == 30
    assert dist.seconds_by_zone[7] == 5 and dist.missing_seconds == 10
    assert dist.recorded_seconds == 95
    assert sum(dist.fractions().values()) == pytest.approx(1.0)
    total = dist + dist
    assert total.seconds_by_zone[2] == 120 and total.missing_seconds == 20
    assert m.empty_distribution().fractions()[1] == 0.0
