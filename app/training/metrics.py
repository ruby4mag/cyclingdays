"""Training-load metrics: NP, IF, TSS, CTL/ATL/TSB, weekly load, zone distribution.

All formulas are documented with sources and assumptions in
``docs/TRAINING_ENGINE.md``. Summary:

* **NP** (Normalized Power, Coggan): 30-s rolling mean of 1-Hz power, raised to
  the 4th power, averaged, 4th root.
* **IF** = NP / FTP.
* **TSS** = duration_s x NP x IF / (FTP x 3600) x 100.
* **CTL/ATL** (Performance Manager, TrainingPeaks convention):
  ``X_t = X_{t-1} + (TSS_t - X_{t-1}) / τ`` with τ = 42 d (CTL) and 7 d (ATL).
  Values are end-of-day, after day *t*'s training.
* **TSB** = CTL_t - ATL_t (end of day *t*), so it is the form going into day *t+1*.

Unknown is never silently zero. A day's load is ``None`` when it is not
covered by synchronized history or when an activity on it has no usable load.
For the exponential recursions an unknown day contributes 0, but it lowers
``data_completeness``, and metrics are flagged ``authoritative`` only when
history is long and complete enough (see ``pmc``).
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.training.zones import ZONE_DEFINITIONS, power_zones, validate_ftp

# --- constants (changing any of them requires a new CALCULATION_VERSION) ------
NP_WINDOW_SECONDS = 30
NP_MIN_SECONDS = 60  # below this NP is not computed
NP_RELIABLE_SECONDS = 20 * 60  # Coggan: NP is less meaningful for efforts < ~20 min
MAX_MISSING_FRACTION = 0.10  # more power dropouts than this -> NP not computed
MAX_PLAUSIBLE_WATTS = 2500.0
# A ride longer than an hour at IF > 1.15 is physiologically implausible and
# points to a wrong FTP or bad power data.
IMPLAUSIBLE_IF_LONG = 1.15
IMPLAUSIBLE_IF_MIN_SECONDS = 3600

CTL_TIME_CONSTANT_DAYS = 42
ATL_TIME_CONSTANT_DAYS = 7
# Metrics count as authoritative when the unknown starting value weighs at most
# this much: (1 - 1/τ)^n <= SEED_WEIGHT_MAX.
SEED_WEIGHT_MAX = 0.10
MIN_COMPLETENESS = 0.90


def min_history_days(time_constant_days: int, seed_weight_max: float = SEED_WEIGHT_MAX) -> int:
    """Days of history after which the unknown seed weighs ≤ ``seed_weight_max``."""
    return math.ceil(math.log(seed_weight_max) / math.log(1 - 1 / time_constant_days))


CTL_MIN_HISTORY_DAYS = min_history_days(CTL_TIME_CONSTANT_DAYS)  # 96
ATL_MIN_HISTORY_DAYS = min_history_days(ATL_TIME_CONSTANT_DAYS)  # 15


# --------------------------------------------------------------------------
# Single activity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PowerSummary:
    duration_s: int
    recorded_s: int
    missing_fraction: float
    average_watts: float | None
    normalized_watts: float | None
    np_reliable: bool
    notes: tuple[str, ...] = ()


def _validate_samples(samples: Sequence[float | None]) -> None:
    for value in samples:
        if value is not None and not 0 <= value <= MAX_PLAUSIBLE_WATTS:
            raise ValueError(f"implausible power sample {value!r} W")


def normalized_power(samples: Sequence[float | None]) -> PowerSummary:
    """NP from 1-Hz power samples. ``None`` = dropout (no reading).

    Dropouts are counted as 0 W (coasting) when they are at most
    ``MAX_MISSING_FRACTION`` of the samples; above that NP is not computed.
    """
    _validate_samples(samples)
    n = len(samples)
    recorded = sum(1 for s in samples if s is not None)
    missing_fraction = 0.0 if n == 0 else (n - recorded) / n
    notes: list[str] = []
    if recorded == 0:
        return PowerSummary(n, 0, 1.0 if n else 0.0, None, None, False, ("no power data",))
    average = sum(s for s in samples if s is not None) / recorded
    if missing_fraction > MAX_MISSING_FRACTION:
        notes.append(f"{missing_fraction:.0%} of samples missing; NP not computed")
        return PowerSummary(n, recorded, missing_fraction, average, None, False, tuple(notes))
    if n < NP_MIN_SECONDS:
        notes.append(f"only {n} s of data; NP not computed")
        return PowerSummary(n, recorded, missing_fraction, average, None, False, tuple(notes))

    filled = [0.0 if s is None else float(s) for s in samples]
    window = NP_WINDOW_SECONDS
    rolling_sum = sum(filled[:window])
    fourth_powers = [(rolling_sum / window) ** 4]
    for i in range(window, n):
        rolling_sum += filled[i] - filled[i - window]
        fourth_powers.append((rolling_sum / window) ** 4)
    np_value = (sum(fourth_powers) / len(fourth_powers)) ** 0.25
    reliable = n >= NP_RELIABLE_SECONDS
    if not reliable:
        notes.append("effort shorter than 20 min; NP is indicative only")
    return PowerSummary(n, recorded, missing_fraction, average, np_value, reliable, tuple(notes))


def intensity_factor(normalized_watts: float, ftp_watts: float | None) -> float:
    return normalized_watts / validate_ftp(ftp_watts)


def training_stress_score(
    duration_s: float, normalized_watts: float, ftp_watts: float | None
) -> float:
    """TSS (Coggan). One hour at FTP = 100."""
    if duration_s < 0 or normalized_watts < 0:
        raise ValueError("duration and NP must be non-negative")
    ftp = validate_ftp(ftp_watts)
    if_ = normalized_watts / ftp
    return duration_s * normalized_watts * if_ / (ftp * 3600) * 100


LoadMethod = Literal["power", "source_load", "none"]


@dataclass(frozen=True)
class ActivityLoad:
    tss: float | None
    method: LoadMethod
    intensity_factor: float | None
    ftp_used: float | None
    notes: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.tss is not None


def activity_load(
    *,
    duration_s: float | None,
    normalized_watts: float | None,
    ftp_at_activity: float | None,
    profile_ftp: float | None,
    source_load: float | None,
) -> ActivityLoad:
    """Choose the load for one activity.

    1. Power-based TSS (our formula) when duration, NP and an FTP are available.
       The FTP recorded with the activity is preferred over the current profile
       FTP, because zones at the time of the ride are what defined its intensity.
    2. Otherwise the source platform's training load (e.g. heart-rate based),
       flagged ``source_load``.
    3. Otherwise unknown (``tss=None``), which lowers data completeness.
    """
    notes: list[str] = []
    ftp: float | None = None
    for candidate, label in ((ftp_at_activity, "activity"), (profile_ftp, "profile")):
        if candidate is None:
            continue
        try:
            ftp = validate_ftp(candidate)
        except ValueError:
            notes.append(f"{label} FTP {candidate!r} invalid")
            continue
        if label == "profile":
            notes.append("no FTP recorded with the activity; used profile FTP")
        break

    if duration_s and duration_s > 0 and normalized_watts and normalized_watts > 0 and ftp:
        if_ = normalized_watts / ftp
        if if_ > IMPLAUSIBLE_IF_LONG and duration_s >= IMPLAUSIBLE_IF_MIN_SECONDS:
            notes.append(
                f"implausible IF {if_:.2f} for {duration_s / 3600:.1f} h; power TSS rejected"
            )
        else:
            tss = training_stress_score(duration_s, normalized_watts, ftp)
            return ActivityLoad(tss, "power", if_, ftp, tuple(notes))
    if source_load is not None and source_load >= 0:
        notes.append("no usable power; used the source platform's training load")
        return ActivityLoad(float(source_load), "source_load", None, ftp, tuple(notes))
    notes.append("no power and no source load; load unknown")
    return ActivityLoad(None, "none", None, ftp, tuple(notes))


# --------------------------------------------------------------------------
# Daily series: CTL / ATL / TSB
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DayLoad:
    """Load for one local day.

    ``tss`` is None when the day is not covered or any activity's load is
    unknown (``known_tss`` then holds the part that is known).
    """

    date: dt.date
    tss: float | None
    known_tss: float
    activity_count: int
    covered: bool


def daily_loads(
    loads_by_date: Mapping[dt.date, Sequence[ActivityLoad]],
    start: dt.date,
    end: dt.date,
    coverage_start: dt.date | None,
) -> list[DayLoad]:
    """Day-by-day load from ``start`` to ``end`` (inclusive).

    A day with no activities inside the covered history is a rest day
    (TSS 0). A day before ``coverage_start`` (or with no coverage at all) is
    unknown.
    """
    if end < start:
        raise ValueError("end before start")
    days: list[DayLoad] = []
    day = start
    while day <= end:
        covered = coverage_start is not None and day >= coverage_start
        loads = list(loads_by_date.get(day, ()))
        known = sum(load.tss for load in loads if load.tss is not None)
        all_known = all(load.known for load in loads)
        tss = known if covered and all_known else None
        days.append(DayLoad(day, tss, known, len(loads), covered))
        day += dt.timedelta(days=1)
    return days


@dataclass(frozen=True)
class DayMetrics:
    date: dt.date
    daily_tss: float | None
    ctl: float
    atl: float
    tsb: float
    rolling_7d_tss: float
    ctl_ramp_7d: float | None
    data_completeness: float  # share of known days in the last CTL τ days
    atl_completeness: float  # share of known days in the last ATL τ days
    known_history_days: int  # consecutive covered days up to this date
    ctl_authoritative: bool
    atl_authoritative: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tsb_authoritative(self) -> bool:
        return self.ctl_authoritative and self.atl_authoritative


def _window_completeness(days: Sequence[DayLoad], index: int, size: int) -> float:
    lo = max(0, index - size + 1)
    window = days[lo : index + 1]
    known = sum(1 for d in window if d.tss is not None)
    return known / size  # days before the series start count as unknown


def pmc(
    days: Sequence[DayLoad],
    *,
    ctl_tc: int = CTL_TIME_CONSTANT_DAYS,
    atl_tc: int = ATL_TIME_CONSTANT_DAYS,
    ctl_seed: float = 0.0,
    atl_seed: float = 0.0,
) -> list[DayMetrics]:
    """Performance-Manager series over consecutive ``days``.

    Seeds default to 0 (no prior knowledge). The seed's weight decays as
    (1 - 1/τ)^n, so CTL needs ``CTL_MIN_HISTORY_DAYS`` (96) covered days and
    ATL ``ATL_MIN_HISTORY_DAYS`` (15) before it is authoritative, plus at
    least ``MIN_COMPLETENESS`` known days in the respective window. CTL is
    also not authoritative when its window holds no activity at all: an empty
    synced history cannot be told apart from uploads that never arrived.
    """
    for a, b in itertools.pairwise(days):
        if b.date - a.date != dt.timedelta(days=1):
            raise ValueError("days must be consecutive")
    ctl_min = min_history_days(ctl_tc)
    atl_min = min_history_days(atl_tc)
    out: list[DayMetrics] = []
    ctl, atl = ctl_seed, atl_seed
    covered_run = 0
    for i, day in enumerate(days):
        load = day.tss if day.tss is not None else 0.0
        ctl += (load - ctl) / ctl_tc
        atl += (load - atl) / atl_tc
        covered_run = covered_run + 1 if day.covered else 0
        completeness = _window_completeness(days, i, ctl_tc)
        atl_completeness = _window_completeness(days, i, atl_tc)
        reasons: list[str] = []
        ctl_window = days[max(0, i - ctl_tc + 1) : i + 1]
        no_activities = not any(d.activity_count for d in ctl_window)
        if no_activities:
            reasons.append(
                f"CTL: no activities in the last {ctl_tc} days; rest cannot be "
                "distinguished from missing uploads"
            )
        if covered_run < ctl_min:
            reasons.append(f"CTL: {covered_run} of {ctl_min} required days of history")
        if completeness < MIN_COMPLETENESS:
            reasons.append(f"CTL: only {completeness:.0%} of the last {ctl_tc} days known")
        if covered_run < atl_min:
            reasons.append(f"ATL: {covered_run} of {atl_min} required days of history")
        if atl_completeness < MIN_COMPLETENESS:
            reasons.append(f"ATL: only {atl_completeness:.0%} of the last {atl_tc} days known")
        week = days[max(0, i - 6) : i + 1]
        rolling = sum(d.tss if d.tss is not None else d.known_tss for d in week)
        ramp = ctl - out[i - 7].ctl if i >= 7 else None
        out.append(
            DayMetrics(
                date=day.date,
                daily_tss=day.tss,
                ctl=ctl,
                atl=atl,
                tsb=ctl - atl,
                rolling_7d_tss=rolling,
                ctl_ramp_7d=ramp,
                data_completeness=completeness,
                atl_completeness=atl_completeness,
                known_history_days=covered_run,
                ctl_authoritative=covered_run >= ctl_min
                and completeness >= MIN_COMPLETENESS
                and not no_activities,
                atl_authoritative=covered_run >= atl_min and atl_completeness >= MIN_COMPLETENESS,
                reasons=tuple(reasons),
            )
        )
    return out


# --------------------------------------------------------------------------
# Weekly load
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WeekLoad:
    week_start: dt.date  # Monday (ISO week)
    tss: float  # sum of known load
    known_days: int
    covered_days: int
    activity_count: int

    @property
    def complete(self) -> bool:
        return self.known_days == 7


def weekly_loads(days: Iterable[DayLoad]) -> list[WeekLoad]:
    weeks: dict[dt.date, list[DayLoad]] = {}
    for day in days:
        monday = day.date - dt.timedelta(days=day.date.weekday())
        weeks.setdefault(monday, []).append(day)
    return [
        WeekLoad(
            week_start=monday,
            tss=sum(d.tss if d.tss is not None else d.known_tss for d in items),
            known_days=sum(1 for d in items if d.tss is not None),
            covered_days=sum(1 for d in items if d.covered),
            activity_count=sum(d.activity_count for d in items),
        )
        for monday, items in sorted(weeks.items())
    ]


# --------------------------------------------------------------------------
# Power distribution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneDistribution:
    seconds_by_zone: dict[int, int]
    missing_seconds: int

    @property
    def recorded_seconds(self) -> int:
        return sum(self.seconds_by_zone.values())

    def fractions(self) -> dict[int, float]:
        total = self.recorded_seconds
        return {z: (s / total if total else 0.0) for z, s in self.seconds_by_zone.items()}

    def __add__(self, other: ZoneDistribution) -> ZoneDistribution:
        return ZoneDistribution(
            {z: self.seconds_by_zone[z] + other.seconds_by_zone[z] for z in self.seconds_by_zone},
            self.missing_seconds + other.missing_seconds,
        )


def empty_distribution() -> ZoneDistribution:
    return ZoneDistribution({number: 0 for number, *_ in ZONE_DEFINITIONS}, 0)


def time_in_zones(samples: Sequence[float | None], ftp_watts: float | None) -> ZoneDistribution:
    """Seconds per power zone from 1-Hz samples; ``None`` counts as missing."""
    _validate_samples(samples)
    zones = power_zones(ftp_watts)
    seconds = dict(empty_distribution().seconds_by_zone)
    missing = 0
    for value in samples:
        if value is None:
            missing += 1
            continue
        for zone in zones:
            if zone.contains(value):
                seconds[zone.number] += 1
                break
    return ZoneDistribution(seconds, missing)
