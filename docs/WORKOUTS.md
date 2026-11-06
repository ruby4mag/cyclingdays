# Workouts: generation, validation and publishing

_Last updated: Phase 7 (2026-11-05). Generator `v1`, validator `v2`, graph
`daily_plan/v3`._

```
LLM draft / fallback ──► build_workout ──► validate_workout ──► planned_workouts row
  (% FTP steps)           (generator)        (validator)          (status validated | rejected)
                                                                        │
                       publish_workout / plan_day ◄─────────────────────┘
                        gate (re-validate, FTP check, competing workouts)
                        ──► events/bulk?upsert=true ──► read back + verify ──► published
                        every step ──► publishing_audit
```

## 1. Structured workout (`app/training/workout_generator.py`)

| Field | Content |
|---|---|
| name, description | from the planner (LLM or fallback) |
| workout_date | the planned day (athlete-local) |
| ftp_watts | the **FTP reference**: the profile FTP at planning time |
| objective | session type, target duration, primary goal, rationale |
| steps | kind (`warmup`/`steady`/`work`/`recovery`/`cooldown`), duration, target % FTP (low–high), target watts, zone, optional cadence, note |
| sections_s | seconds in warmup / main (steady + work) / recovery / cooldown |
| estimate | NP, IF, TSS and average power (see below) |

**Estimate.** Each step is treated as steady at the midpoint of its range:
NP = (Σ t·P⁴ / T)^¼, IF = NP / FTP, and TSS = hours × IF² × 100. On real
workouts Intervals.icu's own estimate agrees:

| Workout | Intervals.icu | Ours |
|---|---|---|
| Probe | TSS 42, IF 0.635 | TSS 42.3, IF 0.637 |
| Live recovery spin | TSS 9, IF 0.424 | TSS 9.2, IF 0.43 |

## 2. Validation (`app/training/workout_validator.py`, `v2`)

One validator, used in three places:

* **During planning.** The guard on the LLM's draft; violations are sent back
  as corrections.
* **In `finalize`.** On the exact stored workout.
* **In the publisher.** Again, immediately before writing to Intervals.icu.

Results are stored in `planned_workouts.validation_result` (errors,
warnings, per-group status, metrics and the full validation context).

| Group | Rules |
|---|---|
| **power** | Valid FTP (50–600 W). Targets 30–160 % FTP with low ≤ high. Range ≤ 10 percentage points (ERG holds one target). Warmup ≤ 75 %, cooldown ≤ 65 %, recovery ≤ 60 %. Stored watts must match the FTP reference (detects tampering) |
| **duration** | Steps 30 s–4 h. Session within its type's range (recovery 20–75 min, endurance 30–240, long endurance 90–360, tempo 45–180, sweet spot 45–150, threshold 45–120, VO2max 45–105). Within ±20 % of the objective. From 30 min: starts with a warmup and ends with a cooldown |
| **intervals** | Efforts at threshold or above must be `work` steps; no VO2+ step over 20 min. Recovery sessions have no work steps. Structured sessions need ≥ 1 work step, ≥ 5 min of warmup before it, and per-type limits (table below) |
| **recovery** | Hard work steps (≥ Z4) must be separated by `recovery` steps lasting at least the type's ratio of the preceding effort. The workout must not end on a work step |
| **readiness** | Session type and every step ≤ the effective cap (deterministic readiness cap, possibly lowered by the LLM). Rest cap → no workout. Red readiness → recovery only. Estimated IF ≤ the cap's ceiling (recovery 0.65, endurance 0.78, tempo 0.88, threshold 0.95, VO2max 1.00) |
| **weekly** | A hard session (threshold or above, IF ≥ 0.85 or TSS ≥ 150, the same definition readiness uses) needs at most 2 hard days in the previous 6 days (≤ 3 per 7 days) and no hard session yesterday |
| **load** | Session TSS ≤ max(80, 2 × CTL) and rolling 7-day TSS ≤ max(400, 1.5 × 7 × CTL). While CTL isn't authoritative, fixed limits apply: 100 per session and 500 per 7 days |
| **plan** (v2, Phase 7) | Session type within the plan's `max_session_type` for the day, TSS ≤ the day's `max_tss`, duration ≤ `max_minutes` (+5 %). Skipped (warning) without an active plan. See [ADAPTIVE_COACHING.md](ADAPTIVE_COACHING.md) |

**Per-type interval limits:**

| Session | Work steps | Reach | Total work | Recovery between efforts |
|---|---|---|---|---|
| tempo | 10–60 min | ≥ 76 % | ≤ 90 min | — |
| sweet spot | 6–30 min | ≥ 88 % | ≤ 75 min | ≥ 15 % of the effort |
| threshold | 4–20 min | ≥ 95 % | ≤ 60 min | ≥ 25 % |
| VO2max | 1–8 min | ≥ 106 % | ≤ 30 min | ≥ 50 % |

**History** for the weekly and load rules covers the 6 days before the plan
date:
* A day with activities counts with their TSS.
* A day not yet over (today) counts with its planned workout.
* A past day that was planned but not ridden adds nothing. Missed sessions
  are never "made up".

Without history, both groups are reported as `skipped` (a warning, never a
pass).

The objective guard applies the duration ranges and the weekly hard-session
rules before a workout is written. A fallback workout that breaks a weekly or
load rule becomes a rest day.

Thresholds are coaching conventions, like the readiness thresholds. Review
them after a few weeks of real data; a change needs a new `VALIDATOR_VERSION`.

## 3. Intervals.icu format (`app/integrations/intervals_workouts.py`)

The native text syntax, verified against the real API (see
[INTERVALS_ICU.md §3.1](INTERVALS_ICU.md)):

```
<description, sanitised>
Objective: recovery. <primary goal>
FTP reference 175 W. Estimated TSS 9, IF 0.43.
Step 1 (Warmup): <note>

- Warmup 5m 40%
- Recovery 20m 45%
- Cooldown 5m 35%
```

* Step lines are `- <Cue> <duration> <low>-<high>% [<rpm>rpm]`. The cue is a
  fixed word per kind, so **LLM text never appears on a step line**.
* Free text is sanitised: leading `-`/`*` removed, timed-prompt characters
  `^ < >` removed, and lines ending in `<n>x` (repeat headers) get a full
  stop.
* Intervals are written out step by step (no repeat blocks), so verification
  can compare steps one to one.
* Event fields:
  * `category=WORKOUT`, `type=Ride`, `indoor=true`;
  * `start_date_local=<date>T00:00:00`;
  * `external_id=cycledays:<athlete>:<date>`.

## 4. Publishing (`app/services/workout_publisher.py`)

Each `publish(athlete, day)` run:

1. Takes a per-athlete advisory lock (`locked` if a run is already active).
2. Skips rows that are `completed`, `missed` or `skipped`. A new plan never
   overwrites those either.
3. **Rest day or rejected workout:** deletes our event for that day, if any.
4. **Gate** (any failure → `blocked`, audit `skip`, nothing written):
   * the validator, re-run on the stored workout and context;
   * profile FTP = workout FTP reference = **Intervals.icu FTP** (targets are
     % FTP, so the trainer's watts come from the Intervals.icu FTP);
   * the stored description equals the regenerated one;
   * **no competing workout:** a `WORKOUT` on the day without our
     `external_id` blocks publishing. The coach never overrides a plan made
     by the athlete or another tool.
5. Deletes extra copies of our `external_id`, keeping one.
6. Leaves an identical event alone; otherwise upserts with
   `events/bulk?upsert=true`.
7. **Verify:** reads the event back and compares the name, date,
   `external_id`, category and type, and every parsed step (duration,
   % FTP, cadence, no ramp or repeat). On a mismatch the event is
   **deleted** and the result is `failed`.
8. Marks the row `published` with `intervals_event_id` and `published_at`.

`--dry-run` stops before any write and returns the payload. `--verify`
re-reads and re-checks the published event.

**Audit.** `coach.publishing_audit` is append-only (a trigger blocks
UPDATE/DELETE). Each row holds the action (`create`, `update`, `delete`,
`verify` or `skip`), success, HTTP status, request payload, a response
summary, a validation snapshot and the error. Each row is written in its own
transaction, so it survives a failure of the surrounding run.

**Re-planning a day** (`plan_day --force`) updates the same
`planned_workouts` row and the same Intervals.icu event. Rows are keyed by
the stable `external_id`, and a partial unique index allows one active
workout per athlete per day.

## 5. Commands

```bash
python -m scripts.plan_day                       # plan tomorrow and publish
python -m scripts.plan_day --no-publish          # plan only
python -m scripts.publish_workout --dry-run      # show the event, write nothing
python -m scripts.publish_workout                # publish (idempotent)
python -m scripts.publish_workout --verify       # retrieve and verify
```

Set `INTERVALS_PUBLISH_ENABLED=false` to plan without publishing.

**Exit codes:**

| Command | 0 | 1 | 3 | 4 |
|---|---|---|---|---|
| `plan_day` | ok | aborted | fallback | publish blocked or failed |
| `publish_workout` | ok | — | — | blocked or failed |
