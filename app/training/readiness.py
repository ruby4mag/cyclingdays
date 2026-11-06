"""Deterministic readiness assessment for a planning day.

Rule-based and fully explainable: every factor reports its value, the personal
baseline it was compared with, and its verdict. Every cap on intensity carries
its reason. The LLM (Phase 4) may choose a session *at or below* ``max_intensity``
but can never raise it (validation in Phase 5 enforces this).

Personal baselines, not population thresholds:

* HRV: ln(rMSSD), mean ± SD of the athlete's own values in the
  ``BASELINE_DAYS`` before the day (≥ ``BASELINE_MIN_VALUES`` values). The
  7-day rolling mean is compared with the baseline's normal range
  (mean ± ``SWC_SD`` x SD, the "smallest worthwhile change" approach of
  Plews et al. / Buchheit). The single-day value is compared as a z-score.
* Resting HR and sleep: z-score / ratio against the athlete's own baseline.

Safety rules (spec §9) built in:

* Reported illness or pain → rest. An injury "niggle" → endurance at most.
* Recovery data missing or stale → never assumed fine. Intensity is capped
  at endurance and confidence is low.
* Not enough history for a baseline, or load metrics not authoritative →
  uncertain → capped at endurance (no automatic hard sessions).
* Hard session yesterday → endurance at most. Two or more hard sessions in
  the last 3 days → recovery at most.

All thresholds are module constants and documented in docs/TRAINING_ENGINE.md.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.training import CALCULATION_VERSION
from app.training.intensity import Intensity, cap
from app.training.metrics import DayMetrics
from app.training.periodization import PlanPhase

# --- thresholds ------------------------------------------------------------
BASELINE_DAYS = 42
BASELINE_MIN_VALUES = 14
SWC_SD = 0.5  # normal range = baseline mean ± 0.5 SD (HRV 7-day trend)
ROLLING_DAYS = 7
ROLLING_MIN_VALUES = 4
FRESH_MAX_AGE_DAYS = 1  # a measurement from today or yesterday is fresh
STALE_MAX_AGE_DAYS = 3  # older than this counts as missing

HRV_Z_NEGATIVE = -1.5
HRV_Z_CAUTION = -1.0
RHR_Z_NEGATIVE = 1.5
RHR_Z_CAUTION = 1.0
SLEEP_RATIO_NEGATIVE = 0.80  # < 80 % of the athlete's usual sleep
SLEEP_RATIO_CAUTION = 0.90
SLEEP_ABSOLUTE_MIN_S = 5 * 3600  # safety floor, applies even without a baseline

# Intervals.icu subjective scale: 1 = good … 4 = bad (developer's forum post).
SUBJECTIVE_NEGATIVE = 4
SUBJECTIVE_CAUTION = 3
# Intervals.icu "injury": 1 none, 2 niggle, 3 poor, 4 injured (community source,
# unverified -> interpreted conservatively).
INJURY_PAIN = 3
INJURY_NIGGLE = 2

FORM_RATIO_NEGATIVE = -0.30  # TSB / CTL
FORM_RATIO_CAUTION = -0.20
FORM_MIN_CTL = 15.0  # below this the ratio is unstable; use absolute TSB
FORM_ABS_CAUTION = -20.0
CTL_RAMP_CAUTION = 8.0  # CTL points per 7 days

HARD_SESSION_IF = 0.85
HARD_SESSION_TSS = 150.0  # long endurance rides are hard by load

FactorStatus = Literal[
    "ok", "caution", "negative", "missing", "stale", "insufficient_baseline", "not_authoritative"
]
ReadinessStatus = Literal["green", "amber", "red"]
Confidence = Literal["high", "medium", "low"]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WellnessDay:
    date: dt.date
    resting_hr_bpm: float | None = None
    hrv_rmssd_ms: float | None = None
    sleep_s: float | None = None
    fatigue: int | None = None
    soreness: int | None = None
    stress: int | None = None
    injury: int | None = None
    illness_reported: bool = False
    pain_reported: bool = False


@dataclass(frozen=True)
class RecentSession:
    date: dt.date
    intensity_factor: float | None
    tss: float | None

    @property
    def hard(self) -> bool:
        return (self.intensity_factor is not None and self.intensity_factor >= HARD_SESSION_IF) or (
            self.tss is not None and self.tss >= HARD_SESSION_TSS
        )


@dataclass(frozen=True)
class ReadinessInput:
    day: dt.date
    wellness: Sequence[WellnessDay]
    load: DayMetrics | None  # end of the previous day
    recent_sessions: Sequence[RecentSession] = ()
    plan_phase: PlanPhase | None = None
    has_active_plan: bool = False


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    mean: float
    sd: float
    n: int


@dataclass
class Factor:
    name: str
    status: FactorStatus
    value: float | None = None
    measured_on: dt.date | None = None
    baseline_mean: float | None = None
    baseline_sd: float | None = None
    baseline_n: int = 0
    z_score: float | None = None
    detail: str = ""


@dataclass
class CapReason:
    max_intensity: Intensity
    reason: str


@dataclass
class ReadinessAssessment:
    day: dt.date
    status: ReadinessStatus
    max_intensity: Intensity
    confidence: Confidence
    factors: list[Factor]
    caps: list[CapReason]
    notes: list[str] = field(default_factory=list)
    calculation_version: str = CALCULATION_VERSION

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["max_intensity"] = self.max_intensity.label
        for item in data["caps"]:
            item["max_intensity"] = Intensity(item["max_intensity"]).label
        return data


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def baseline(values: Sequence[float], min_values: int = BASELINE_MIN_VALUES) -> Baseline | None:
    if len(values) < min_values:
        return None
    sd = statistics.stdev(values)
    if sd <= 0:
        return None
    return Baseline(statistics.fmean(values), sd, len(values))


def _series(
    wellness: Sequence[WellnessDay], attr: str, start: dt.date, end: dt.date
) -> list[tuple[dt.date, float]]:
    out = []
    for w in wellness:
        value = getattr(w, attr)
        if value is not None and value > 0 and start <= w.date <= end:
            out.append((w.date, float(value)))
    return sorted(out)


def _latest(
    wellness: Sequence[WellnessDay], attr: str, day: dt.date
) -> tuple[dt.date, float] | None:
    series = _series(wellness, attr, day - dt.timedelta(days=STALE_MAX_AGE_DAYS), day)
    return series[-1] if series else None


def _freshness(measured: dt.date | None, day: dt.date) -> FactorStatus | None:
    if measured is None:
        return "missing"
    age = (day - measured).days
    if age <= FRESH_MAX_AGE_DAYS:
        return None
    return "stale"


def _baseline_values(
    wellness: Sequence[WellnessDay], attr: str, day: dt.date, transform: Any = None
) -> list[float]:
    values = [
        v
        for _, v in _series(
            wellness, attr, day - dt.timedelta(days=BASELINE_DAYS), day - dt.timedelta(days=1)
        )
    ]
    return [transform(v) for v in values] if transform else values


# --------------------------------------------------------------------------
# Factors
# --------------------------------------------------------------------------


def hrv_factor(wellness: Sequence[WellnessDay], day: dt.date) -> list[Factor]:
    latest = _latest(wellness, "hrv_rmssd_ms", day)
    single = Factor("hrv", "missing", detail="no HRV measurement in the last 3 days")
    if latest is None:
        return [single]
    measured, value = latest
    single.value, single.measured_on = value, measured
    fresh = _freshness(measured, day)
    base = baseline(_baseline_values(wellness, "hrv_rmssd_ms", measured, math.log))
    if base is not None:
        single.baseline_mean = math.exp(base.mean)  # geometric mean in ms
        single.baseline_sd, single.baseline_n = base.sd, base.n
    if fresh is not None:
        single.status, single.detail = fresh, f"latest HRV is from {measured}"
        return [single]
    if base is None:
        single.status = "insufficient_baseline"
        single.detail = (
            f"fewer than {BASELINE_MIN_VALUES} HRV values in the last {BASELINE_DAYS} days"
        )
        return [single]

    z = (math.log(value) - base.mean) / base.sd
    single.z_score = z
    if z <= HRV_Z_NEGATIVE:
        single.status, single.detail = "negative", "HRV well below personal baseline"
    elif z <= HRV_Z_CAUTION:
        single.status, single.detail = "caution", "HRV below personal baseline"
    else:
        single.status, single.detail = "ok", "HRV within personal range"

    trend = Factor(
        "hrv_trend_7d",
        "missing",
        baseline_mean=math.exp(base.mean),
        baseline_sd=base.sd,
        baseline_n=base.n,
    )
    recent = [
        math.log(v)
        for _, v in _series(
            wellness, "hrv_rmssd_ms", day - dt.timedelta(days=ROLLING_DAYS - 1), day
        )
    ]
    if len(recent) < ROLLING_MIN_VALUES:
        trend.detail = f"fewer than {ROLLING_MIN_VALUES} HRV values in the last 7 days"
    else:
        rolling = statistics.fmean(recent)
        trend.value = math.exp(rolling)
        trend.z_score = (rolling - base.mean) / base.sd
        if rolling < base.mean - SWC_SD * base.sd:
            trend.status = "caution"
            trend.detail = "7-day HRV average below the personal normal range"
        else:
            trend.status, trend.detail = "ok", "7-day HRV average within or above the normal range"
    return [single, trend]


def resting_hr_factor(wellness: Sequence[WellnessDay], day: dt.date) -> Factor:
    factor = Factor("resting_hr", "missing", detail="no resting HR in the last 3 days")
    latest = _latest(wellness, "resting_hr_bpm", day)
    if latest is None:
        return factor
    measured, value = latest
    factor.value, factor.measured_on = value, measured
    base = baseline(_baseline_values(wellness, "resting_hr_bpm", measured))
    if base is not None:
        factor.baseline_mean, factor.baseline_sd, factor.baseline_n = base.mean, base.sd, base.n
    fresh = _freshness(measured, day)
    if fresh is not None:
        factor.status, factor.detail = fresh, f"latest resting HR is from {measured}"
    elif base is None:
        factor.status = "insufficient_baseline"
        factor.detail = f"fewer than {BASELINE_MIN_VALUES} values in the last {BASELINE_DAYS} days"
    else:
        z = (value - base.mean) / base.sd
        factor.z_score = z
        if z >= RHR_Z_NEGATIVE:
            factor.status, factor.detail = "negative", "resting HR well above personal baseline"
        elif z >= RHR_Z_CAUTION:
            factor.status, factor.detail = "caution", "resting HR above personal baseline"
        else:
            factor.status, factor.detail = "ok", "resting HR within personal range"
    return factor


def sleep_factor(wellness: Sequence[WellnessDay], day: dt.date) -> Factor:
    factor = Factor("sleep", "missing", detail="no sleep data in the last 3 days")
    latest = _latest(wellness, "sleep_s", day)
    if latest is None:
        return factor
    measured, value = latest
    factor.value, factor.measured_on = value, measured
    fresh = _freshness(measured, day)
    if fresh is not None:
        factor.status, factor.detail = fresh, f"latest sleep record is from {measured}"
        return factor
    base = baseline(_baseline_values(wellness, "sleep_s", measured))
    if value < SLEEP_ABSOLUTE_MIN_S:
        factor.status = "negative"
        factor.detail = (
            f"slept {value / 3600:.1f} h, below the {SLEEP_ABSOLUTE_MIN_S / 3600:.0f} h floor"
        )
    if base is None:
        if factor.status != "negative":
            factor.status = "insufficient_baseline"
            factor.detail = (
                f"fewer than {BASELINE_MIN_VALUES} nights in the last {BASELINE_DAYS} days"
            )
        return factor
    factor.baseline_mean, factor.baseline_sd, factor.baseline_n = base.mean, base.sd, base.n
    factor.z_score = (value - base.mean) / base.sd
    if factor.status == "negative":
        return factor
    ratio = value / base.mean
    if ratio < SLEEP_RATIO_NEGATIVE:
        factor.status, factor.detail = "negative", f"slept {ratio:.0%} of usual"
    elif ratio < SLEEP_RATIO_CAUTION:
        factor.status, factor.detail = "caution", f"slept {ratio:.0%} of usual"
    else:
        factor.status, factor.detail = "ok", f"slept {ratio:.0%} of usual"
    return factor


def subjective_factor(wellness: Sequence[WellnessDay], day: dt.date) -> Factor:
    """Worst of fatigue / soreness / stress reported today or yesterday."""
    recent = [w for w in wellness if day - dt.timedelta(days=FRESH_MAX_AGE_DAYS) <= w.date <= day]
    scores = [
        (name, value, w.date)
        for w in recent
        for name, value in (("fatigue", w.fatigue), ("soreness", w.soreness), ("stress", w.stress))
        if value is not None
    ]
    if not scores:
        return Factor("subjective", "missing", detail="no subjective wellness reported")
    name, worst, measured = max(scores, key=lambda s: (s[1], s[2]))
    status: FactorStatus = "ok"
    if worst >= SUBJECTIVE_NEGATIVE:
        status = "negative"
    elif worst >= SUBJECTIVE_CAUTION:
        status = "caution"
    return Factor(
        "subjective",
        status,
        value=worst,
        measured_on=measured,
        detail=f"worst score: {name} {worst} (scale 1 good - 4 bad)",
    )


def form_factors(load: DayMetrics | None) -> list[Factor]:
    if load is None:
        return [Factor("form", "missing", detail="no load metrics")]
    form = Factor("form", "ok", value=load.tsb, measured_on=load.date)
    if not load.tsb_authoritative:
        form.status = "not_authoritative"
        form.detail = "; ".join(load.reasons) or "load history incomplete"
        return [form]
    if load.ctl >= FORM_MIN_CTL:
        ratio = load.tsb / load.ctl
        form.z_score = None
        form.detail = f"TSB {load.tsb:.1f} = {ratio:.0%} of CTL {load.ctl:.1f}"
        if ratio <= FORM_RATIO_NEGATIVE:
            form.status = "negative"
        elif ratio <= FORM_RATIO_CAUTION:
            form.status = "caution"
    else:
        form.detail = f"TSB {load.tsb:.1f} (CTL {load.ctl:.1f} too low for a ratio)"
        if load.tsb <= FORM_ABS_CAUTION:
            form.status = "caution"
    factors = [form]
    if load.ctl_ramp_7d is not None:
        ramp = Factor(
            "ctl_ramp_7d",
            "ok",
            value=load.ctl_ramp_7d,
            measured_on=load.date,
            detail=f"CTL change over 7 days: {load.ctl_ramp_7d:+.1f}",
        )
        if load.ctl_ramp_7d > CTL_RAMP_CAUTION:
            ramp.status = "caution"
        factors.append(ramp)
    return factors


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------


def assess_readiness(data: ReadinessInput) -> ReadinessAssessment:
    day = data.day
    caps: list[CapReason] = []
    notes: list[str] = []
    factors: list[Factor] = []

    # 1. Safety flags (today or yesterday).
    recent = [
        w for w in data.wellness if day - dt.timedelta(days=FRESH_MAX_AGE_DAYS) <= w.date <= day
    ]
    if any(w.illness_reported for w in recent):
        caps.append(CapReason(Intensity.REST, "illness reported"))
    if any(w.pain_reported for w in recent):
        caps.append(CapReason(Intensity.REST, "pain reported"))
    injuries = [w.injury for w in recent if w.injury is not None]
    if injuries:
        worst = max(injuries)
        factors.append(
            Factor(
                "injury",
                "ok" if worst < INJURY_NIGGLE else "negative",
                value=worst,
                detail="scale 1 none, 2 niggle, 3 poor, 4 injured",
            )
        )
        if worst >= INJURY_PAIN:
            caps.append(CapReason(Intensity.REST, f"injury score {worst} reported"))
        elif worst >= INJURY_NIGGLE:
            caps.append(CapReason(Intensity.ENDURANCE, "injury niggle reported"))

    # 2. Physiological factors against personal baselines.
    factors.extend(hrv_factor(data.wellness, day))
    factors.append(resting_hr_factor(data.wellness, day))
    factors.append(sleep_factor(data.wellness, day))
    factors.append(subjective_factor(data.wellness, day))
    factors.extend(form_factors(data.load))

    by_name = {f.name: f for f in factors}
    scored = ("hrv", "resting_hr", "sleep", "subjective", "form")
    negatives = [n for n in scored if n in by_name and by_name[n].status == "negative"]
    cautions = [f.name for f in factors if f.status == "caution"]

    if len(negatives) >= 2:
        caps.append(
            CapReason(Intensity.RECOVERY, f"several negative signals: {', '.join(negatives)}")
        )
    elif len(negatives) == 1:
        caps.append(CapReason(Intensity.ENDURANCE, f"negative signal: {negatives[0]}"))
    if cautions:
        caps.append(CapReason(Intensity.TEMPO, f"caution: {', '.join(cautions)}"))

    # 3. Data availability: never assume readiness without recovery data.
    objective = [by_name[n] for n in ("hrv", "resting_hr", "sleep")]
    usable = [f for f in objective if f.status in ("ok", "caution", "negative")]
    if not usable:
        statuses = ", ".join(f"{f.name}: {f.status}" for f in objective)
        caps.append(CapReason(Intensity.ENDURANCE, f"no usable recovery data ({statuses})"))
    elif any(
        by_name[n].status in ("missing", "stale", "insufficient_baseline")
        for n in ("hrv", "resting_hr")
    ):
        caps.append(
            CapReason(Intensity.ENDURANCE, "HRV or resting HR cannot be judged against a baseline")
        )
    if by_name["form"].status in ("missing", "not_authoritative"):
        caps.append(CapReason(Intensity.ENDURANCE, "training-load metrics are not authoritative"))

    # 4. Recent hard sessions.
    yesterday = day - dt.timedelta(days=1)
    hard = [
        s for s in data.recent_sessions if s.hard and day - dt.timedelta(days=3) <= s.date < day
    ]
    if len(hard) >= 2:
        caps.append(CapReason(Intensity.RECOVERY, f"{len(hard)} hard sessions in the last 3 days"))
    elif any(s.date == yesterday for s in hard):
        caps.append(CapReason(Intensity.ENDURANCE, "hard session yesterday"))

    # 5. Long-term plan.
    if data.plan_phase is not None:
        phase_cap = data.plan_phase.intensity_cap
        notes.append(f"plan phase: {data.plan_phase.name} ({data.plan_phase.type})")
        if phase_cap < Intensity.VO2MAX:
            caps.append(CapReason(phase_cap, f"{data.plan_phase.type} phase"))
    elif data.has_active_plan:
        notes.append("active plan has no phase covering this day")
    else:
        notes.append("no active long-term plan")

    max_intensity = cap(*(c.max_intensity for c in caps))
    # Status describes the athlete (physiology + safety + data), not the plan.
    physiological = [c for c in caps if not c.reason.endswith(" phase")]
    physio_cap = cap(*(c.max_intensity for c in physiological))
    status: ReadinessStatus
    if physio_cap <= Intensity.RECOVERY:
        status = "red"
    elif physio_cap <= Intensity.TEMPO:
        status = "amber"
    else:
        status = "green"

    judged = sum(1 for f in objective if f.status in ("ok", "caution", "negative"))
    confidence: Confidence
    if judged == 3 and by_name["form"].status not in ("missing", "not_authoritative"):
        confidence = "high"
    elif judged >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return ReadinessAssessment(day, status, max_intensity, confidence, factors, caps, notes)
