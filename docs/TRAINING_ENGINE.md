# Training intelligence engine

_Last updated: Phase 6 (2026-11-05). Calculation version **`v2`**._

**Versions:** `v1` (Phase 3) is the initial formulas. `v2` (Phase 6) keeps
the same formulas, but a ride recorded twice (e.g. icTrainer + COROS watch)
counts once: activities with `duplicate_of_activity_id` are excluded from
load (see [SYNC.md §3](SYNC.md)). On the real data of 2026-11-05, v2 equals
v1 on every day, because the account has no activities yet.

All calculations are deterministic Python in `app/training/` (pure functions,
no I/O, no LLM). `app/services/training_metrics.py` feeds them from the
database and stores the daily results. Changing **any** formula or constant
on this page requires a new `CALCULATION_VERSION` (`app/training/__init__.py`).
Stored rows are keyed by version, so old results stay reproducible.

## 1. Power zones (`zones.py`)

This is the seven-zone model after Coggan (Allen & Coggan, *Training and Racing
with a Power Meter*):

| Zone | Name | % FTP |
|---|---|---|
| Z1 | Active recovery | < 55 |
| Z2 | Endurance | 55 – 75 |
| Z3 | Tempo | 75 – 90 |
| Z4 | Lactate threshold | 90 – 105 |
| Z5 | VO2max | 105 – 120 |
| Z6 | Anaerobic capacity | 120 – 150 |
| Z7 | Neuromuscular | ≥ 150 |

* Each zone is a half-open interval `[lower, upper)`, so a boundary value
  belongs to the higher zone.
* Watt bounds are rounded to 1 µW to remove floating-point noise: 0.55 × 175
  gives exactly 96.25 W, not 96.250000000000014.
* FTP must be 1–600 W (the same rule as the DB constraint). A missing FTP
  raises `InvalidFTPError`; the engine never guesses one.
* With the current FTP of 175 W: Z2 = 96.25–131.25 W, Z4 = 157.5–183.75 W.

`intensity.py` defines ordered levels for planning caps:
rest < recovery (Z1) < endurance (Z2) < tempo (Z3, incl. sweet spot)
< threshold (Z4) < vo2max (Z5+).

## 2. Single-activity metrics (`metrics.py`)

| Metric | Formula |
|---|---|
| NP | 30-s rolling mean of 1-Hz power → 4th power → mean → 4th root (Coggan) |
| IF | NP / FTP |
| TSS | duration_s × NP × IF / (FTP × 3600) × 100 (1 h at FTP = 100) |

How missing data is handled:

* `None` samples are dropouts. Up to 10 % dropouts count as 0 W (coasting).
  Above that, NP is **not computed**: the result is `None` with a note.
* Under 60 s of data, NP isn't computed. Under 20 min, NP is computed but
  flagged `np_reliable=False`.
* Samples outside 0–2500 W raise an error instead of being silently clipped.

**Activity load selection** (`activity_load`):

1. If there's a duration, NP and a valid FTP, use our power TSS.
   * The FTP recorded with the activity is preferred over the profile FTP.
     Zones at the time of the ride defined its intensity.
   * Duration is `moving_time`, falling back to `elapsed_time`.
   * NP is Intervals.icu's `icu_weighted_avg_watts`, because power streams
     aren't synced yet (§7).
   * Plausibility check: IF > 1.15 for ≥ 1 h is rejected (it points to a
     wrong FTP or bad data) and falls through to step 2.
2. Otherwise use the source platform's load (`icu_training_load`, e.g.
   heart-rate based), flagged `source_load`.
3. Otherwise the load is **unknown**, flagged `none`.

The method used is stored per day in `training_metrics.details.load_methods`.

## 3. Daily series: CTL / ATL / TSB

This follows the TrainingPeaks Performance Manager convention (Coggan;
impulse-response idea after Banister):

```
CTL_t = CTL_{t-1} + (TSS_t − CTL_{t-1}) / 42
ATL_t = ATL_{t-1} + (TSS_t − ATL_{t-1}) / 7
TSB_t = CTL_t − ATL_t          (end of day t = form going into day t+1)
ramp_t = CTL_t − CTL_{t−7}
rolling_7d_tss_t = Σ TSS over days t−6 … t
```

* Values are end-of-day. Readiness for day *D* uses the row for *D − 1*.
* Seeds are 0 at the start of the series, because there's no prior
  knowledge. Intervals.icu's own CTL isn't used as a seed: it's a different
  implementation, and mixing the two would make our numbers unreproducible.
* Intervals.icu shows its own CTL/ATL values. They can differ from ours
  (different constants, seeding, and load per activity). Ours are the ones
  the coach uses.

### 3.1 Known, rest and unknown days

| Day | TSS used |
|---|---|
| inside synced history, no activities | 0 (rest), **known** |
| inside synced history, all activities have a load | sum, **known** |
| any activity with unknown load | known part only, day **unknown** |
| before synced history (`coverage_start`) | **unknown** |

`coverage_start` is the earliest day the activity sync has ever covered. It's
kept in `synchronization_state.cursor`; incremental windows always overlap,
so history is contiguous from there. In the exponential recursions an unknown
day contributes its known part (often 0), and it **lowers completeness**.

### 3.2 When metrics are authoritative

The unknown starting value decays as (1 − 1/τ)ⁿ. A metric is authoritative
only when **all** of these hold:

| | CTL (τ = 42) | ATL (τ = 7) |
|---|---|---|
| contiguous covered days (seed weight ≤ 10 %) | ≥ **96** | ≥ **15** |
| known days in the last τ days | ≥ 90 % | ≥ 90 % |
| at least one activity in the last τ days | required | — |

The last rule exists because zero activities in 42 days can't be told apart
from uploads that never arrived. (That's the current state of the real
account.) TSB is authoritative only if both CTL and ATL are.

Every stored row carries `data_completeness`, plus `ctl_authoritative`,
`atl_authoritative`, `tsb_authoritative` and the human-readable `reasons` in
`details`. Non-authoritative values are stored but must **never be presented
as authoritative**: readiness treats them as uncertain (§5).

`INTERVALS_HISTORY_DAYS` now defaults to 180 so a `--full` sync gives CTL
enough history.

### 3.3 Weekly load

Weeks are ISO weeks (Monday to Sunday, local date). Each week reports the sum
of known TSS, the number of known days, the number of activities, and
`complete` (all 7 days known).

## 4. Power distribution and planned versus actual

### 4.1 Power distribution

`time_in_zones(samples, ftp)` gives seconds per zone (Z1–Z7) plus missing
seconds, with fractions over the recorded seconds. Distributions can be
added together.

### 4.2 Planned versus actual (`performance.py`)

* **Inputs:** planned steps (duration plus an optional `[low, high]` W
  target) and 1-Hz power.
* **Alignment:** the recording starts at the first step, which holds for ERG
  workouts started from icTrainer. An optional `start_offset_s` shifts it.

| Output | Definition |
|---|---|
| step deviation % | (mean power − target midpoint) / midpoint × 100, over seconds with data |
| step status | `not_started` (no data), `incomplete` (< 90 % of planned seconds), `completed` (\|dev\| ≤ 5 %), `under_target` / `over_target` |
| completion % | recorded seconds inside the plan / planned seconds × 100 (cap 200) |
| work completion % | actual kJ / planned kJ at target midpoints (cap 200) |
| power deviation % | step deviations weighted by planned duration |
| extra_recorded_s | data after the planned end (not counted) |

## 5. Readiness (`readiness.py`)

Readiness is rule-based. Every factor reports its value, measurement date,
**personal baseline** (mean, SD, n), z-score and verdict. Every intensity
cap reports its reason. The output is:

* `status`: green / amber / red. This describes the athlete: physiology,
  safety and data quality.
* `max_intensity`: the most restrictive of all caps, including the plan
  phase and recent hard sessions.
* `confidence`: high / medium / low.

The Phase 4 LLM may choose at or below `max_intensity`. It can never go
above it.

### 5.1 Personal baselines

* Window: the 42 days before the measurement, excluding the measurement day.
  At least 14 values are needed, and the SD must be > 0.
* **HRV** (rMSSD) is analysed as ln(rMSSD) (Plews et al.; Buchheit 2014):
  * Single day: z ≤ −1.5 is **negative**, z ≤ −1.0 is **caution**.
  * The 7-day rolling mean of ln(rMSSD) needs ≥ 4 values. If it's below the
    baseline mean − 0.5 SD (the "smallest worthwhile change" normal range),
    that's a **caution**.
* **Resting HR:** z ≥ 1.5 is **negative**, z ≥ 1.0 is **caution**.
* **Sleep:**
  * < 80 % of the personal mean is **negative**; < 90 % is **caution**.
  * A safety floor of **< 5 h is always negative**, even without a baseline.
    This is the one deliberately generic threshold.
* Freshness: a value from today or yesterday is fresh. A value 2–3 days old
  is `stale`. Anything older is `missing`.

### 5.2 Subjective and safety inputs

* Intervals.icu fatigue, soreness and stress: scale **1 = good … 4 = bad**,
  per the Intervals.icu developer
  ([forum](https://forum.intervals.icu/t/api-access-to-intervals-icu/609/34)).
  The worst of the three from today or yesterday counts: 4 is **negative**,
  3 is **caution**.
* Intervals.icu `injury`: **1 none, 2 niggle, 3 poor, 4 injured**. This comes
  from a community API client and is **not yet confirmed** by an official
  source, so it's interpreted conservatively:
  * 2 caps intensity at **endurance**.
  * ≥ 3 means **rest**.
* `illness_reported` or `pain_reported` today or yesterday means **rest**.

### 5.3 Load factors

These are evaluated only if TSB is authoritative; otherwise the status is
`not_authoritative`.

* Form, when CTL ≥ 15: TSB/CTL ≤ −30 % is **negative**; ≤ −20 % is **caution**.
* Form, when CTL < 15: TSB ≤ −20 is **caution**.
* CTL ramp > 8 points in 7 days is **caution**.

### 5.4 Combining factors into caps

| Condition | Cap |
|---|---|
| illness, pain, or injury ≥ 3 | rest |
| ≥ 2 negative factors (hrv, resting_hr, sleep, subjective, form) | recovery |
| ≥ 2 hard sessions in the last 3 days | recovery |
| 1 negative factor | endurance |
| injury niggle (2) | endurance |
| hard session yesterday | endurance |
| no usable recovery data (HRV, RHR and sleep all missing, stale or without a baseline) | endurance |
| HRV or RHR can't be judged against a baseline | endurance |
| load metrics missing or not authoritative | endurance |
| plan phase `recovery` or `transition` | endurance |
| any caution | tempo |

* A **hard session** is IF ≥ 0.85 or TSS ≥ 150. Long brevet-style rides are
  hard by load.
* **Status** comes from the caps excluding the plan phase: cap ≤ recovery is
  red, ≤ tempo is amber, otherwise green.
* **Confidence** is high when HRV, RHR and sleep can all be judged and load
  is authoritative; medium when 2 of the 3 can be judged; otherwise low.

The readiness result isn't persisted in Phase 3. Phase 4 stores it in
`coach_decisions.input_snapshot` together with the decision.

## 6. Long-term plan (`periodization.py`)

`training_plans.phases` is shaped `{"phases": [{name, type, start_date,
end_date, weekly_tss_target?, notes?}]}`:

* `type` is one of base, build, peak, taper, recovery, transition.
* Phases are validated by Pydantic: no overlaps, and `end_date ≥ start_date`.
* An invalid plan is reported in the notes, not trusted.

Readiness uses the phase covering the planning day. Creating plans is Phase 7.

## 7. Assumptions and limits

1. **NP comes from Intervals.icu.** Power streams aren't synced, so activity
   TSS uses `icu_weighted_avg_watts` as NP. The NP function itself is
   implemented and tested for when streams are available. Since Phase 7,
   `app/training/evaluation.py` fetches the power and HR streams of each new
   ride and feeds them to `performance.evaluate_workout` (planned versus
   actual), adding aerobic decoupling. Load still uses the Intervals.icu NP
   (see [ADAPTIVE_COACHING.md](ADAPTIVE_COACHING.md)).
2. **Activities of every sport count toward load.** Non-power sports use the
   source load.
3. **`icu_intensity` isn't used.** We compute IF ourselves.
4. **Thresholds are conventions, not universal truths.** They're documented
   here, versioned (`v1`), and should be reviewed against the athlete's
   response over time.
5. **The real account has no activities yet,** so load metrics are not
   authoritative. On real data today, readiness is amber with an
   endurance cap.
