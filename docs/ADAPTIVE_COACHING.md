# Adaptive coaching: plan, evaluation, adaptation, scheduler

_Last updated: Phase 7 (2026-11-05). Plan builder `plan_builder/v1`,
progression `progression/v1`, adaptation `adaptation/v1`, evaluation
`evaluation/v2`, validator `v2`, graph `daily_plan/v3`._

```
                ┌──────────── scheduler (APScheduler, own container) ────────────┐
every 30 min:   │ poll ─► sync ─► detect rides ─► inbox ─► evaluate ─► mark missed │
20:00 local:    │ daily_plan ─► poll ─► extend plan ─► graph (guidance) ─► publish │
                └──────────────────────────────────────────────────────────────────┘

guidance for a day = strictest of
  readiness (Phase 3)  ∧  plan prescription (week, day role, budget)  ∧  adaptation (recent rides)
```

## 1. Long-term plan (`app/training/plan_builder.py`)

`python -m scripts.create_plan --weekly-hours 6-9 [--event-date YYYY-MM-DD]`

* **Weekly phases, Monday to Sunday.** Each week has a type, a TSS target, a
  time target and a key session (the most demanding type allowed on key days).
* **4-week blocks:** 3 load weeks, each +8 %, then a recovery week at 60 % of
  the week before it. The next block starts one ramp step above the previous
  block's second week.

  | Block | Phase | Key session |
  |---|---|---|
  | 1 | base | tempo |
  | 2 | base | sweet spot |
  | 3 | build | threshold |
  | 4+ | build | VO2max |

* **Load:**
  * the start is 50 TSS per hour × the lower bound of weekly hours;
  * the cap is 55 TSS per hour × the upper bound;
  * the time target is TSS ÷ 50 per hour.
* **With an event date:** the plan ends with 2 peak weeks (threshold) and
  2 taper weeks (70 %, then 45 %).
* **Without a date (your current plan):** the plan is open-ended.
  `DailyPlanner` extends it by whole blocks so it always covers
  `PLAN_HORIZON_DAYS` (28) ahead. To add a date later, run
  `create_plan --event-date … --replace`; the old plan is archived.
* **Weekly template** (default, stored with the plan):

  | Mon | Tue | Wed | Thu | Fri | Sat | Sun |
  |---|---|---|---|---|---|---|
  | rest | key | endurance | key | recovery | long | endurance |

**Live plan (2026-11-05):** "Open-ended endurance plan (6-9 h/week)",
2026-11-02 to 2027-01-24. Weekly TSS:

| Block | Weeks 1-3 | Recovery week |
|---|---|---|
| 1 | 300, 324, 350 | 210 |
| 2 | 350, 378, 408 | 245 |
| 3 | 408, 441, 476 | 286 |

## 2. Day prescription (`app/training/progression.py`)

The day's **role** gives the most demanding session allowed:
* rest: rest;
* recovery: recovery;
* endurance: endurance;
* long: long endurance;
* key: the week's key session.

Session types are ordered as a ladder: recovery < endurance = long
endurance < tempo < sweet spot < threshold < VO2max.

* **Budget:** the role's share of the week's TSS target. The shares are rest 0,
  recovery 0.3, endurance 1.0, key 1.4 and long 1.8. `max_tss` is 125 % of the
  budget, never below 25. `max_minutes` is the same share of the time target
  × 125 %, at least 30 minutes (120 for the long day).
* **No cramming.** A missed day's budget is never moved to another day. The
  week is also capped at 110 % of its target. If it's already full, the day
  is limited to an easy spin.
* **Prescription for a 300-TSS week:**

  | Day | Max session | Target TSS | Max TSS | Max minutes |
  |---|---|---|---|---|
  | Tue/Thu | tempo | 61 | 76 | 99 |
  | Wed/Sun | endurance | 43 | 54 | 70 |
  | Fri | recovery | 13 | 25 | 30 |
  | Sat | long endurance | 78 | 98 | 120 |

## 3. Evaluation of a completed ride (`app/training/evaluation.py`)

| Part | How |
|---|---|
| Matching | The planned workout with the Intervals.icu `paired_event_id`. Otherwise the coach's workout on the same date, if no other ride claimed it. A second ride that day is `unplanned` |
| Power | Power stream (`GET /activity/{id}/streams`) compared step by step (`performance.evaluate_workout`: completion %, work completion, deviation per step and overall). Without a stream, the totals are compared (`method = "summary"`) |
| Heart rate | Average HR per work step. **Aerobic decoupling** (Pa:HR, first half vs second half of the main set, ≥ 20 min of paired data) |
| Power zones (v2) | Seconds per zone from the power stream at the ride's FTP; shown on the dashboard |
| Subjective | RPE against the expected RPE per session type (recovery 2, endurance 4, long 5, tempo 5, sweet spot 6, threshold 7, VO2max 8), Intervals.icu "feel", and that day's wellness fatigue and soreness |
| Outcome | `as_planned` (≥ 95 %, within ±5 %, RPE < expected + 2), `completed_with_deviation`, `partial` (50-85 %), `abandoned` (< 50 %) or `unplanned` |
| LLM review (optional) | `performance_review/v1`: summary, what went well, concerns, and a recommendation of `keep`, `reduce` or `recover`. **There is no "increase" option.** An abandoned session can't be answered with `keep` (guard). Stored in `coach_decisions` |

Results go to `workout_performance`: one row per activity (unique), with the
outcome, completion, deviation, decoupling, RPE, per-step compliance, the full
evaluation and the activity revision.

## 4. Adaptation (`app/training/adaptation.py`): lower-only

| Signal | Effect |
|---|---|
| Latest key session (≤ 10 days) struggled: completion < 85 %, power < −5 %, or RPE ≥ expected + 2 | Key days capped one rung below that session |
| Two struggled key sessions in a row | Key days capped at endurance. "Check FTP" is suggested; FTP is **never** changed automatically |
| A ride in the last 3 days with high RPE, decoupling > 8 % on an endurance ride, or the review says reduce or recover | Day budget × 0.85 |
| Review says "recover" for yesterday's ride | Today capped at recovery |
| Last week < 70 % of its target | This week held at last week's target (no ramp, no make-up) |

Completing sessions never raises anything. Progression comes only from the
plan's ramp.

## 5. In the daily plan (graph `daily_plan/v3`)

* `context.guidance`:
  * `max_intensity`: the strictest of readiness and the plan;
  * the plan limits: `max_session_type`, `max_tss`, `max_minutes`;
  * `target_tss`;
  * the prescription;
  * the adaptation;
  * `week_tss_done`;
  * `last_week_compliance`;
  * `caps`, each with its reason.

  `context.recent_evaluations` summarizes the last 14 days of evaluations.
* **A rest role** routes to rest with no LLM call, and the rationale names the
  plan.
* **Guards:** the objective guard checks the session type and duration against
  the plan. The validator's new **`plan`** group (validator v2) checks the
  session type, TSS ≤ `max_tss`, and duration ≤ `max_minutes` (+5 %). The
  publisher re-runs it before publishing.
* **Prompts:**
  * `objective_selection/v3` and `workout_planning/v3` receive `plan_guidance`
    and the recent evaluations;
  * the fallback workouts respect `max_minutes`.

## 6. Events, idempotency and missed days (`app/services/activity_processor.py`)

* **Detection** is polling. Intervals.icu webhooks need an OAuth application
  (requested from Intervals.icu) and a public HTTPS endpoint, neither of which
  exists (see §8).
* **Revisions:** each ride gets a revision, a hash of start time, moving time,
  power, NP, HR, RPE, feel and the paired event. A ride without an evaluation
  for its current revision is put into `coach.webhook_events`, used as an event
  inbox:
  * dedup key `activity:<id>:<revision>`, unique `(source, dedup_key)`;
  * detecting the same ride again → no new event;
  * an edit (e.g. RPE added later) → a new revision, evaluated again **in
    place**, since `workout_performance` is unique per activity.
* **Processing:** events are claimed with `FOR UPDATE SKIP LOCKED`, one at a
  time.
  * A failure is recorded (`failed`, `last_error`) and retried on the **next**
    run, up to 5 attempts.
  * Events left `processing` by a crash become retryable.
  * A per-athlete advisory lock keeps runs from overlapping.
* **Missed:** a coach workout on an earlier day with no ride becomes `missed`.
  It's never rescheduled. A late upload turns it back to `completed`.
  Duplicate recordings (Phase 6) are never evaluated.

## 7. Scheduler (`app/scheduler.py`, compose service `scheduler`)

| Job | When | Does |
|---|---|---|
| `poll` | every `SCHEDULER_POLL_MINUTES` (30) | sync → evaluate → mark missed → sync checks |
| `daily_plan` | `SCHEDULER_PLAN_TIME` (20:00, athlete timezone) | poll → extend plan → plan tomorrow → publish (`INTERVALS_PUBLISH_ENABLED`) |
| `daily_plan_catch_up` | once, 90 s after start, if started after the plan time | the same as `daily_plan`; idempotent |

* **Locks:** each job runs under a session advisory lock
  (`pg_try_advisory_lock`), so a second scheduler process skips instead of
  running twice.
* **Job settings:** `max_instances=1` and `coalesce=True`; the daily plan has
  a 6-hour misfire grace.
* **Errors:** logged as `scheduler_job_failed`, and the scheduler keeps
  running. Failures are emailed (Phase 9, see OPERATIONS.md).

```bash
docker compose up -d scheduler      # start automatic coaching
docker compose stop scheduler       # pause it
podman logs -f cycling-coach_scheduler_1
```

## 8. Known limitations

* **No real ride in the account yet.** The streams endpoint's response
  shape (`ActivityStream`: `type`, `data`) and the `types` parameter
  (sent comma-separated) are taken from the OpenAPI schema and parsed
  tolerantly. They will be confirmed on the first ride. If streams fail, the
  evaluation falls back to totals.
* **Webhooks:** Intervals.icu sends them only to OAuth applications, with a
  `secret` in the payload. With an API key, polling every 30 minutes is the
  supported path. A webhook receiver would also need API authentication and a
  public endpoint (Phase 9).
* **Step alignment** assumes the ride starts with the first planned step,
  which is true for ERG rides started in icTrainer. A pre-ride warm-up before
  pressing start shifts the comparison. `start_offset_s` exists in the
  calculation but isn't detected yet.
* **Thresholds are conventions:** the shares, ramp, RPE expectations,
  85 %/−5 %/+2 RPE, 8 % decoupling and 70 % compliance. Review them after
  a few weeks of rides; changes need new versions.
* **Week 1 (2026-11-02) had no training data,** so the 70 % compliance rule
  holds week 2 at 300 TSS instead of 324. This is deliberate: no load is
  assumed without data.
