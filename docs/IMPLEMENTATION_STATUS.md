# Implementation status

| Phase | Title | Status |
|---|---|---|
| 1 | Infrastructure and application foundation | ✅ Approved 2026-11-05 |
| 2 | Intervals.icu integration | ✅ Approved 2026-11-05 |
| 3 | Training intelligence engine | ✅ Approved 2026-11-05 |
| 4 | LangGraph and OpenAI integration | ✅ Approved 2026-11-05 |
| 5 | ERG workout generation and publishing | ✅ Approved 2026-11-05 |
| 6 | icTrainer and COROS integration | ✅ Approved 2026-11-05 (icTrainer and COROS-activity checks still wait for the first real ride) |
| 7 | Adaptive coaching | ✅ Approved 2026-11-05 (live ride evaluation waits for the first real ride) |
| 8 | Dashboard | ✅ Approved 2026-11-05 (http://127.0.0.1:8010/dashboard) |
| 9 | Production hardening | 🔶 Steps 1–2 done 2026-11-06 (email notifications, restart after reboot); the rest on hold at the user's request |
| 10 | Multi-user coaching | 🔶 Built and deployed 2026-11-06 (v0.10.0); awaiting approval |

## Phase 10 (multi-user) — acceptance criteria

Requested 2026-11-06: users sign in, set goal, timeline and plan, connect
Intervals.icu, then get the existing coaching. Details: [MULTI_USER.md](MULTI_USER.md).

| Criterion | Status | Evidence |
|---|---|---|
| Each login coaches its own athlete; data never crosses users | ✅ | athlete from the session only (`app/api/dashboard.py:_slug`, `app/api/account.py`); `test_multi_user.py::test_users_only_ever_see_their_own_athlete` |
| Riders connect their own Intervals.icu account; keys stored encrypted | ✅ | `intervals_connections` (migration 0007), Fernet; key checked with `GET /athlete/{id}` first; `test_connections.py`; live: the original key moved into the DB, decrypts and works, plaintext not stored |
| An Intervals.icu athlete can't be taken over by another account | ✅ | 409 on a second connect / athlete switch; tested, and refused live (wrong key → 422) |
| Goal, timeline and training plan set in the web app | ✅ | Account page: objective, event date, weekly hours, week template, preview; same builder as `scripts.create_plan`; week-template rules tested |
| Same coaching for every rider | ✅ | scheduler runs poll / daily plan / onboarding per connected athlete in their own time zone, per-athlete locks and failure isolation (`test_scheduler.py`); live: `poll:primary` ran on the stored key |
| Emails: problems to the operator, workout summary to the rider | ✅ (code) / ⏳ (first live one at 20:00) | `JobNotifier`, `athlete_summary_message`; `magingeorge` linked to `primary` with their address |
| Watchdog covers every athlete | ✅ | `plan_problems_from_rows` (per-athlete time zone, onboarding grace); unit-tested |
| Onboarding UI | ✅ | `/account` checked live with a temporary user (API: setup_required, 409 before connect, cross-site 403) and screenshots (desktop light/dark, mobile; no console errors); temporary user deleted |
| A second real rider onboarded end to end | ⏳ | needs a real second Intervals.icu account |
| Tests and checks | ✅ | 369+ passed; ruff, ruff format, mypy strict clean |

## Phase 9 (steps 1–2) — acceptance criteria

Approved scope: step 1 (error notifications, by email, problems plus a daily
summary) and step 2 (restart after a VM reboot). Everything else in Phase 9
is still on hold. Details: [OPERATIONS.md](OPERATIONS.md).

| Criterion | Status | Evidence |
|---|---|---|
| Job failures, sync errors, blocked publishing and fallback plans are emailed | ✅ | `app/alerts.py`, `app/notifications.py`, `JobNotifier` in `app/scheduler.py`; `test_alerts.py`, `test_notifications.py` (fake SMTP), `test_scheduler.py`. Gmail SMTP live; test emails sent 2026-11-06 |
| Daily summary after the 20:00 plan | ✅ (code) / ⏳ (first one at 20:00 on 2026-11-06) | `daily_plan_message`; quiet on re-runs that change nothing |
| No alert storms: threshold, repeat interval, "resolved" messages | ✅ | `AlertTracker`; watchdog `decide`; unit tests |
| A dead or stuck scheduler is detected | ✅ | heartbeat job + compose healthcheck (scheduler `healthy` live); host watchdog checks containers, `/health/ready` and the daily plan (`--dry-run` clean on the live system; missing-plan query checked against the live DB) |
| Containers restart after a VM reboot | ✅ (simulated) | `podman-restart.service` (enabled, Podman 5.8.2) starts `should-start-on-boot` containers, which include all three; `systemctl restart podman-restart.service` on 2026-11-06: back in 2 s, ready in 36 s, poll ran. A real reboot is untested |
| Watchdog timer installed | ⏳ | units in `deploy/systemd/`; installing them needs root approval (commands in OPERATIONS.md) |
| Tests and checks | ✅ | 354 passed; ruff (incl. `deploy/watchdog`), ruff format, mypy strict clean; the watchdog compiles on the host's Python 3.9 |

## Phase 1 — acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Application starts successfully | ✅ | venv (`uvicorn app.main:create_app --factory`) and container (`docker compose up -d --build`, container `healthy`) |
| PostgreSQL connection works | ✅ | `/health/ready` → `database: ok`; integration tests |
| Database migrations execute | ✅ | `alembic upgrade head`, `downgrade base`, re-upgrade; `alembic check` → no drift; `test_migrations.py` |
| Health endpoint returns success | ✅ | `/health` 200, `/health/ready` 200 with `applied=expected=0001` |
| Basic tests pass | ✅ | 43 passed (26 unit, 17 integration), `-W error`; ruff + mypy strict clean |
| No credentials are committed | ✅ | `.env` git-ignored (verified with `git check-ignore`), `.env.example` holds placeholders only, `.dockerignore` keeps `.env` out of the image |

## Phase 8 — acceptance criteria

Verified 2026-11-05 with the API (integration tests on a seeded database),
the page (unit tests and headless-Chromium screenshots of the live database,
a seeded demo database, dark mode and phone width), and the rebuilt
container (all sections return 200). Design: [DASHBOARD.md](DASHBOARD.md).

| Spec item | Status | Notes |
|---|---|---|
| Simple web dashboard with FastAPI and a suitable frontend | ✅ | Static HTML, JS and CSS served by FastAPI. No framework, CDN or new dependency. Strict CSP; read-only JSON API with 8 sections |
| Athlete profile | ✅ | Profile card |
| Current FTP | ✅ | Tile: W, W/kg and the Intervals.icu match (live: 175 W, matches); zones in watts |
| Fitness and fatigue trends | ✅ | CTL/ATL chart and a form chart. Provisional days are marked (live: everything provisional, since there are no rides yet) |
| Training calendar | ✅ | 5 weeks: plan roles, coach workouts and status, rides and outcomes, other events (live: 11-06 published, 11-07 validated) |
| Upcoming workout | ✅ | Power profile, steps, TSS, IF, the reason (live: Fri 11-06 recovery spin) |
| Completed workouts | ✅ tests · ⏳ live (no ride yet) | Table with evaluation, outcome and coach recommendation; duplicates hidden |
| Weekly training volume | ✅ | TSS against the plan target; hours, rides and km |
| Power zone distribution | ✅ tests · ⏳ live (no ride yet) | Coach zones from the power stream (new: evaluation v2), with the Intervals.icu zone times as a labelled fallback |
| AI coaching assessments | ✅ | Daily plans and ride reviews from `coach_decisions` (live: 3 daily plans) |
| Long-term objective progress | ✅ | Plan weeks with target against done, compliance and CTL (live: plan 1, open-ended, week 1 of 12) |

## Phase 7 — acceptance criteria

Verified 2026-11-05 in tests (PostgreSQL, scripted LLM, fake Intervals.icu
streams) and live against the real account (real OpenAI, real Intervals.icu
sync). Design: [ADAPTIVE_COACHING.md](ADAPTIVE_COACHING.md).

| Criterion | Status | Evidence |
|---|---|---|
| Completed workouts are evaluated | ✅ tests · ⏳ live (no ride yet) | Each ride is matched to its planned workout (paired event, else date) and compared step by step from the power stream, or from totals as a fallback. Also: HR and aerobic decoupling, RPE vs expected, wellness fatigue, an outcome class, and an optional LLM review (lower-only). The planned workout becomes `completed`. Tests cover as planned, under target, partial, abandoned, unplanned second ride, stream failure, LLM down, and a failing evaluation that is retried. **Live:** the poll ran cleanly (sync ok, 0 rides to evaluate) |
| Future workouts reflect previous performance | ✅ | `guidance` = readiness ∧ plan prescription ∧ adaptation. Test: a threshold session with RPE 9 lowers the next key day from threshold to sweet spot. The LLM sees `recent_evaluations` and `plan_guidance`. The LLM review can only say keep, reduce or recover; adaptation only lowers |
| Duplicate activity events don't trigger duplicate processing | ✅ | Inbox keyed by `activity:<id>:<revision>` (unique), `SKIP LOCKED` claims, one `workout_performance` row per activity. Tests: re-detection → 0 events and no LLM call; a duplicate enqueue is ignored; an edited ride (RPE added) is re-evaluated in place; duplicate recordings (Phase 6) are skipped |
| Missed workouts are not crammed into the next day | ✅ | Past open workouts become `missed` (a late upload revives them). Day budgets are fixed shares of the week, and the week is capped at 110 %. A missed day's load is never redistributed; low compliance holds progression. Tests: unit (every session missed → same daily limits) and integration (3 missed days → Saturday's limits don't grow) |
| Training progression follows the longer-term objective | ✅ | Deterministic plan: 4-week blocks base → build, a weekly template, TSS and time targets for 6-9 h. **Live plan created** (2027-01-24 horizon, auto-extended). The validator's `plan` group plus the objective guard keep the LLM in the plan. Test: LLM proposes sweet spot on a tempo-block key day → rejected, corrected to tempo. **Live:** Saturday 2026-11-07 (long day, ≤ 120 min / 98 TSS) planned as a 90-min Z2 long ride (TSS 69), all 8 validation groups passed (not published) |

Also delivered:
* **APScheduler service** (`scheduler` in compose): a poll every 30 min and
  the daily plan at 20:00 IST, with job-level PostgreSQL advisory locks
  (tested), coalescing and a catch-up run. **Live:** the container started
  and its first poll job finished in 1.8 s; it was then **stopped**, pending
  approval.
* **Migration 0005:** `workout_performance.outcome` (CHECK),
  `decoupling_pct`, `session_date`, `activity_revision`.
* **Validator v2** (`plan` group), **graph `daily_plan/v3`**, prompts
  `objective_selection/v3`, `workout_planning/v3`, `performance_review/v1`.
* **CLIs:** `create_plan`, `process_activities`; `plan_day --sync` now also
  evaluates rides.

## Phase 6 — acceptance criteria

Checked 2026-11-05 against the **real** account (`scripts.check_sync --sync`)
and in tests. CycleDays uses the supported integrations only: icTrainer and
COROS each connect to Intervals.icu themselves. Design: [SYNC.md](SYNC.md).

| Criterion | Status | Evidence |
|---|---|---|
| Intervals.icu planned workouts appear in icTrainer | ⏳ **Pending first ride** (`not_observed`) | Not observable through the API. It's verified when an icTrainer ride paired with a CycleDays event carries the workout's name, or confirmed manually (`--confirm intervals_to_ictrainer.planned`). Tests cover verified, different name, foreign event and confirmed |
| ERG workouts can be executed | ⏳ **Pending first ride** (`not_observed`) | Check: indoor, power data, average power within ±10 % of planned, ≥ 80 % of planned duration. Tests: pass, wrong power/duration/outdoor, no power |
| Completed workouts return to Intervals.icu | ⏳ **Pending first ride** (`not_observed`) | Activity provenance is now stored (`origin`, `origin_client`, `device_name`, `paired_event_id`). icTrainer is detected by OAuth client or device name |
| COROS activity synchronization works | ⏳ **Pending first COROS activity** (`not_observed`) | COROS is linked with `coros_sync_activities = true`, but there's no activity in the account yet. Detected by `source = COROS` (tested) |
| Available COROS wellness information is synchronized | ✅ **Verified** | 26 of 28 days with device wellness, latest 2026-11-04: `restingHR`, `hrv`, `sleepSecs`, `avgSleepingHR` (13 days each), `steps` (26). Never seen: `sleepScore`, `hrvSDNN`, `spO2`, `respiration`, `readiness`, `weight`, `vo2max`. COROS is the only wellness provider enabled |
| Don't assume bidirectional sync; document directions and limitations | ✅ | [SYNC.md](SYNC.md) §1 (each direction with its status) and §4 (limitations: what the API can't observe, COROS deletions, community-sourced icTrainer behaviour) |

Also delivered:
* **Duplicate recordings:** a ride recorded by both icTrainer and the watch
  is linked and counted once in training load (`CALCULATION_VERSION` v2; on
  real data v2 equals v1 on all 181 days).
* **`coach.sync_checks` (migration 0004):** stores a row only when a result
  changes. Live: 8 rows on the first run, 0 on the re-run.
* **Manual confirmation** for directions the API can't see.
* **CLI `scripts.check_sync`.**

## Phase 5 — acceptance criteria

Verified 2026-11-05 against the **real** Intervals.icu account (athlete
`i780524`) and in tests with a fake calendar. Design: [WORKOUTS.md](WORKOUTS.md).

| Criterion | Status | Evidence |
|---|---|---|
| Workout can be generated | ✅ | Live re-plan for 2026-11-06 (`plan_day --force`, real OpenAI): "Gentle Z1 Recovery Spin", 30 min, 3 steps (5' 40 %, 20' 45 %, 5' 35 %), FTP reference 175 W, estimated TSS 9.2, IF 0.43. Stored as `planned_workouts` row 1, linked to decision 10 |
| Workout passes validation | ✅ | All 7 groups passed (power, duration, intervals, recovery, readiness, weekly, load), stored in `validation_result`. 20 unit tests cover each rule; a graph test shows the LLM being corrected on interval structure |
| Workout appears in Intervals.icu | ✅ | **Event 152154504** on 2026-11-06, `category=WORKOUT`, `external_id=cycledays:primary:2026-11-06`. Intervals.icu parsed 3 steps and computed TSS 9 / IF 42.4 % (ours: 9.2 / 0.43) |
| Repeated execution does not create duplicates | ✅ | Live: the second `publish_workout` returned `unchanged` with no write; `plan_day` re-run → `unchanged`; the calendar lists exactly one event. Upsert behaviour verified on the real API (a plain POST duplicates; the bulk upsert doesn't). Tests: repeat, re-plan updates the same event, existing duplicates repaired |
| Published workout can be retrieved and verified | ✅ | Live `publish_workout --verify`: the event was read back and every step's duration and % FTP matched. Tests: a misread workout is deleted; a tampered event fails verification |

Also delivered:
* **Safety gates, each tested:**
  * re-validation before publishing;
  * FTP must match the profile and Intervals.icu (mismatch or missing FTP blocks);
  * a competing workout on the day blocks publishing;
  * rest days remove our event;
  * completed or missed days are never overwritten.
* **Append-only `publishing_audit`:** live rows are `create` plus 4 `verify`.
* **`--dry-run`.**
* **No migration needed.**

## Phase 4 — acceptance criteria

Verified 2026-11-05: integration tests against PostgreSQL with a scripted
LLM, plus a live run with the real OpenAI API (`gpt-6.1-sol`) and real
Intervals.icu data. Design: [COACHING_GRAPH.md](COACHING_GRAPH.md).

| Criterion | Status | Evidence |
|---|---|---|
| Graph executes | ✅ | **Live run** for 2026-11-06: 4 LLM nodes, each accepted on the first attempt, 29 s in total. Readiness amber, cap endurance. The LLM chose a 30-min **recovery** ride (warmup 35 %, 20 min at 45 %, cooldown 35 %). Tests cover every route: planned, rest (illness, no LLM call), LLM-chosen rest, fallback, abort |
| State persists across executions | ✅ | `AsyncPostgresSaver` in schema `langgraph`, one thread per athlete and day. **Live:** the second run returned `already_planned` in 1.2 s with no LLM calls and no new rows. Test: a run killed after `recovery_assessment` resumes without repeating completed LLM nodes |
| Structured outputs validate | ✅ | OpenAI strict schema → Pydantic (`extra="forbid"`, bounds) → deterministic guards. Schema-invalid output is retried as a correction (test). Live: all 4 outputs parsed and passed the guards |
| API failures are handled | ✅ | Retry with backoff and jitter for timeouts, connection errors, 408/409/429/5xx. No retry for auth or bad-request errors. Exhaustion → conservative fallback plan (tests: mocked SDK errors, outage, missing key) |
| LLM cannot bypass deterministic validation | ✅ | The LLM can only lower the cap (`effective_cap`, clamped and logged). Guard violations are corrected, then fall back; they are never accepted. A rest cap skips the LLM entirely. Tests: intensity above cap, a hidden threshold step in an endurance ride, mislabelled steps, wrong duration, missing warmup, persistent violation → fallback |

Also delivered: versioned prompts (`*/v1`, SHA-256 stored with every call).
`coach_decisions` stores one row per LLM call (accepted or rejected) with the
reported model, prompt version, input, output, violations and usage, plus one
`daily_plan` row with the full context and result. Also new: the CLI
`scripts.plan_day`.

## Phase 3 — acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Unit tests validate the calculations | ✅ | 66 new unit tests checking reference values: constant-power NP; square-wave NP against the closed form (221.8 W); 1 h at FTP = 100 TSS; CTL/ATL step response vs 100·(1−(1−1/τ)ⁿ); zone boundaries for several FTPs; completion and deviation; readiness scenarios |
| Metrics are reproducible | ✅ | Pure functions, versioned (`calculation_version = v1`), upserted per (athlete, day, version). Repeat runs report `unchanged` for every row, in the integration test and on real data (181 rows) |
| Missing data is handled explicitly | ✅ | Load per day is known, rest or unknown; per-activity load method recorded; NP refuses > 10 % dropouts; readiness factors report `missing`, `stale` or `insufficient_baseline` and cap intensity instead of assuming readiness |
| Incomplete historical data is never presented as authoritative | ✅ | Every row stores `data_completeness` and `*_authoritative` with reasons (CTL needs ≥ 96 covered days, ≥ 90 % known, and ≥ 1 activity in 42 days). Non-authoritative load caps readiness at endurance. Real account: CTL = 0 is flagged "rest cannot be distinguished from missing uploads" |

Implemented: FTP zones, NP/IF/TSS, activity load selection, CTL/ATL/TSB,
ramp rate, rolling 7-day and weekly (ISO) TSS, time in zones, workout
completion and power deviation, plan phases, and a readiness assessment
(training-load trend, HRV/RHR/sleep against personal baselines, subjective
fatigue, injury/illness/pain, recent hard sessions, long-term plan phase).
Formulas: [TRAINING_ENGINE.md](TRAINING_ENGINE.md).

**Real data (2026-11-05):** readiness **amber, max endurance, confidence low**,
for three reasons: sleep 4.6 h (below the 5 h floor); HRV and RHR baselines
have 13 of the 14 required values; load not authoritative (no activities).

## Phase 2 — acceptance criteria

Verified against the real Intervals.icu API (athlete `i780524`) on 2026-11-05
unless noted.

| Criterion | Status | Evidence |
|---|---|---|
| Athlete profile can be retrieved | ✅ | `GET /athlete/{id}` → profile linked to `i780524`. Snapshot stored with `icu_api_key` redacted and email removed. FTP/weight/timezone compared (weight 85.0 vs 86.0 kg reported, not applied) |
| Historical activities are stored | ⚠️ Partly | Pipeline verified: 90-day window (≥ 30 required), upsert and dedup tested end to end against PostgreSQL (`test_intervals_sync.py`: 31 activities over 90 days). **The real account has 0 activities (all-time)**, so the activity mapping is unverified on real data. Re-run `--full` after the first COROS ride syncs |
| Calendar data can be retrieved | ✅ | `GET /athlete/{id}/events`: real `SEASON_START` event retrieved and mapped. Window today−90 … today+28 synced (0 events in it). Planned workouts = `category=WORKOUT` |
| Synchronization can be repeated without duplicates | ✅ | Real data: run 1 inserted 29 wellness days. Run 2 and `--full` reported only `unchanged`, and the DB had 0 duplicate keys. Also integration tests (repeat, `--full`, edited and late records, soft delete and revive) and DB unique constraints |
| API errors are logged appropriately | ✅ | Real 401 (bad key) and 403 (wrong athlete): `intervals_request_failed` ERROR with path, status, attempts and body. `intervals_sync_resource_failed` with stack trace. Failure stored in `synchronization_state`. Key never in logs (checked). Retry logs tested with 429/5xx/transport mocks |

Also delivered: incremental sync (date-window cursor + overlap), source
identifiers on every row, retry/backoff with `Retry-After`, a per-athlete
advisory lock, migration `0002`, and the CLI `scripts.sync_intervals`
(incl. `--status`).

Tests: **78 passed** (54 unit, 24 integration) with `-W error`. ruff and mypy
(strict) clean. `alembic check`: no drift. Container rebuilt: `/health/ready`
reports `applied=expected=0002` and the sync runs inside the container.

## Phase 1 — environment (inspected 2026-11-04)

| Item | Value |
|---|---|
| OS | Rocky Linux 10.2 (Red Quartz), kernel 6.12.0, SELinux enforcing |
| Python | 3.12.15 (`/usr/bin/python3.12`) |
| CPU / RAM / disk | 16 vCPU, 5.4 GiB RAM (+3.7 GiB swap), 52 GB free |
| Containers | Podman 5.8.2 + podman-compose (runs CycleDays). **Docker CE 29.9 is also installed and running** (other projects): `docker` is not Podman here (corrected 2026-11-06) |
| Host PostgreSQL | none installed; other projects' containers use host ports 5432, 5433 |
| Ports used by this project | 5434 (PostgreSQL), 8010 (API), both bound to 127.0.0.1 |

## Outstanding issues / notes

1. ~~**Restart after host reboot (Podman).**~~ Resolved (Phase 9 step 2,
   2026-11-06): Podman 5.8's `podman-restart.service` starts
   `unless-stopped` containers at boot (verified by simulation), and the
   watchdog reports each reboot. See [OPERATIONS.md](OPERATIONS.md#2-restart-after-a-vm-reboot).
2. ~~**API has no authentication yet.**~~ Done (2026-11-06): the app has
   its own login (users in `coach.app_users`, signed session cookies). See
   [DASHBOARD.md](DASHBOARD.md#login). `/health` stays public.
3. **Slow `fsync` on VM storage.** `TRUNCATE` takes ~3 s per call. Irrelevant
   for the app's workload, but tests avoid `TRUNCATE` for this reason.
4. ~~Intervals.icu field mapping not yet defined.~~ Done in Phase 2: see
   [INTERVALS_ICU.md](INTERVALS_ICU.md). The activity mapping is still
   unverified on real data (item 9).
5. **Athlete age** is stored as a static `age_years` seed value;
   `date_of_birth` is available if you prefer an exact value.
6. **FTP history** is not a separate table yet; each activity stores
   `ftp_at_activity_watts`. Revisit in Phase 3 if zone calculations need a
   dated FTP history.
7. ~~Repository is `git init`-ed but nothing has been committed.~~ Pushed to
   the private GitHub repo `ruby4mag/cyclingdays` (2026-11-06).
8. **`podman-compose up -d --build` doesn't recreate a running container.**
   After a rebuild, use `docker compose up -d --force-recreate --no-deps app`
   (documented in the README).
9. **No real activities in Intervals.icu yet** (Phase 2). COROS is linked
   with activity sync enabled, but only wellness was back-filled. Unverified:
   `icu_intensity` as a percentage, and the shape of the `Hidden` stub. After
   the first ride syncs, run `python -m scripts.sync_intervals --full` and
   check `coach.activities`.
10. **Profile differences are only reported.** Intervals.icu weight is
    85.0 kg against 86.0 kg in the profile. Nothing updates the profile
    automatically. A deliberate update path (API/dashboard) comes with
    authentication.
11. ~~Wellness injury/illness not interpreted.~~ Phase 3: `injury` is stored
    in `daily_recovery.subjective_injury` (migration 0003) and interpreted by
    readiness. **The injury scale (1 none … 4 injured) still needs
    confirming**: enter a niggle in Intervals.icu once and check the synced
    value. Intervals.icu has no illness field; `illness_reported` /
    `pain_reported` wait for a manual-report path.
12. **Deleted activities** aren't detected (calendar events are soft-deleted).
13. **No sync scheduling yet.** Sync runs on demand from the CLI; APScheduler
    comes in Phase 7. There's no HTTP endpoint for triggering a sync, because
    the API has no authentication (item 2).
14. Intervals.icu publishes no rate limits. The client honours `Retry-After`
    and backs off, but has no proactive request budget.
15. **Power streams aren't synced** (Phase 3). Activity TSS uses
    Intervals.icu's weighted average power as NP. Zone distribution and
    planned-versus-actual analysis are implemented and tested, and will be fed
    by streams in Phase 7 (activity evaluation).
16. **Readiness thresholds are documented conventions** (version v1). Review
    them against the athlete's actual response after a few weeks of data.
17. ~~Readiness isn't persisted.~~ Phase 4: readiness is stored in each
    `daily_plan` decision's `input_snapshot`.
18. **FTP history:** there's still no separate table. Each activity carries
    `ftp_at_activity_watts`, which the load calculation prefers, so a dated
    history isn't needed yet (resolves Phase 1 note 6).
19. **Model `gpt-6.1-sol` has no dated snapshot.** Its behaviour can change
    without notice. The reported model is stored with every decision. Pin a
    dated model (e.g. `gpt-5.5-2026-04-23`) in `OPENAI_MODEL` if
    reproducibility matters more. Latency varies: a trivial probe once took
    about 86 s, but each node took 5–8 s in the live run. The timeout is
    300 s per request.
20. ~~Nothing is published yet.~~ Phase 5: validated workouts are published
    and verified.
21. **No scheduler yet.** `scripts.plan_day` runs on demand; daily scheduling
    is Phase 7.
22. **Transitive dependencies:** LangGraph pulls in `langsmith`, but tracing
    is off (no `LANGSMITH_*` variables). openai pulls in `httpx`, alongside
    our `httpx2`.
23. **LLM rationale wording.** In the live run the model wrote "wellness is
    missing", but 7 wellness days exist. The context provides readiness
    factors (stale or insufficient baselines), not raw wellness, so the
    wording is loose but the decision is conservative and within the cap.
    Consider a prompt refinement (v2) once more data exists.
24. **Upsert by `external_id` is observed, not documented, for API-key
    auth.** The OpenAPI text says "created by the same OAuth application". It
    worked with our API key on 2026-11-05. If that changed, a duplicate could
    appear, but the next publish run deletes extra copies of our
    `external_id` (tested).
25. **Workout validator thresholds are conventions (v1).** These cover
    interval lengths, recovery ratios, IF ceilings, 3 hard sessions per 7
    days, and TSS limits (100/500 while CTL isn't authoritative). Review them
    with real data.
26. **Competing workouts block publishing.** If you plan a workout yourself
    in Intervals.icu (or another app does) on a day, the coach won't publish
    that day (`plan_day` exit code 4, audit `skip`). This is deliberate:
    there are no competing plans.
27. **Recovery rides have no "main" section.** A recovery session's steady
    spin is labelled `recovery`, so its main section is 0 s. Structured
    sessions report warmup, main, recovery and cooldown as expected.
28. ~~icTrainer pick-up isn't verified yet.~~ Phase 6 adds the checks;
    the real verification waits for the first ride (item 31).
29. **A live workout is on your calendar:** event 152154504 (2026-11-06,
    30-min recovery spin). Remove it with `DELETE
    /api/v1/athlete/i780524/events/152154504` or from the Intervals.icu UI if
    you don't want it.
30. **Two plan threads exist for 2026-11-06.** The v1 thread from Phase 4 and
    the forced v2 re-plan. `plan_day` without `--force` returns the v1
    summary (`already_planned`), but publishing always uses the stored
    `planned_workouts` row (the v2 plan).
31. **Phase 6 live verification needs a real ride.** Follow the athlete
    checklist in [SYNC.md §5](SYNC.md):
    * link Intervals.icu in icTrainer;
    * check the workout is listed (then `--confirm`);
    * ride it in ERG with icTrainer as the only recorder;
    * record one activity with the COROS watch;
    * run `python -m scripts.check_sync --sync`.

    Four of the six criteria are pending until then.
32. **icTrainer and COROS behaviour is partly community-sourced.** Neither
    vendor publishes official integration documentation that I could find.
    Facts come from Intervals.icu forum posts (including the developer's own)
    and the OpenAPI schema. The exact `source` / `oauth_client_name` of an
    icTrainer upload is unconfirmed. Detection matches "icTrainer" in the
    client or device name, and every source seen is reported.
33. **COROS workout upload is on.** Intervals.icu sends about a week of
    planned workouts to the watch, and deletions reportedly don't propagate.
    A rest-day change by CycleDays may leave a stale workout on the watch.
    Intervals.icu stays authoritative. Turn off "Upload workouts" for COROS in
    Intervals.icu if this is confusing.
34. **Double recording risk.** Starting the workout on the watch while
    icTrainer records produces two activities. They are detected and counted
    once, but HR may only be in the watch file. Recommended: icTrainer as the
    only recorder, with the HR strap paired to icTrainer.
35. **Wellness has no source field.** Attribution to COROS relies on COROS
    being the only enabled wellness provider (reported as ambiguous
    otherwise).
36. **Phase 7 needs a real ride for live evaluation.** The streams endpoint
    shape (`ActivityStream` `type`/`data`, comma-separated `types`) comes from
    the OpenAPI schema and hasn't been verified. If it fails, the evaluation
    falls back to activity totals. After the first ride, run
    `python -m scripts.process_activities` and check `coach.workout_performance`.
37. **The scheduler is stopped.** Start it with `docker compose up -d
    scheduler` once Phase 7 is approved. From then on it plans and publishes
    tomorrow at 20:00 IST, and polls every 30 minutes (each daily plan makes
    4 OpenAI calls; each new ride gets one review call).
38. **Saturday 2026-11-07 is planned but not published** (row 2, status
    `validated`, 90-min Z2 long ride). The scheduler publishes it when it
    runs, or run `python -m scripts.publish_workout --date 2026-11-07`.
39. **No webhooks:** Intervals.icu webhooks require an OAuth app and a public
    HTTPS endpoint. Polling (30 min) is used instead; a webhook receiver
    belongs with API authentication (Phase 9).
40. **Adaptive-coaching thresholds are conventions (v1):** plan ramp and
    shares, expected RPE, the struggle thresholds, decoupling and compliance.
    Review them after 3-4 weeks of rides.
41. **Week 1 of the plan has no ride data,** so progression is held at
    300 TSS in week 2 (low-compliance rule). This is intended.
42. **Step alignment** assumes the ride starts with step 1 (ERG from
    icTrainer). An offset isn't detected yet.
43. **No brevet date yet.** Add it with `python -m scripts.create_plan
    --weekly-hours 6-9 --event-date YYYY-MM-DD --replace` to get peak and
    taper weeks.
44. ~~**The dashboard has no login (Phase 9).**~~ Done (2026-11-06): app
    login, see [DASHBOARD.md](DASHBOARD.md#login). Serve it over HTTPS when
    it's reachable from the internet.
45. **The dashboard is mostly empty in real use until rides sync:** completed
    workouts, zones and weekly volume. The populated views were checked with
    seeded data (integration tests and demo screenshots).
46. **Power zones** come from the coach's evaluation only for rides evaluated
    from now on (`evaluation/v2`). The Intervals.icu `icu_zone_times`
    fallback format comes from the OpenAPI schema and hasn't been verified
    on real data.
47. **The scheduler is running** (started at the athlete's request,
    2026-11-05 15:49 UTC). The catch-up run for 11-06 returned
    `already_planned` / publish `unchanged`. Saturday 11-07 was published
    by hand (event 152311970, verified).
48. **COROS: the workout reached Training Hub, not the mobile app or DURA**
    (2026-11-05). The workarounds are in SYNC.md §4.2. COROS may also show
    single %FTP targets as 0 W (forum reports). Publishing narrow ranges
    instead is a possible fix and needs the athlete's approval.
49. **Integration tests read the live `.env`.** Since the HTTPS setup it has
    `AUTH_COOKIE_SECURE=true`, so 3 login and dashboard tests fail over the
    test client's plain HTTP. Run the suite with `-e AUTH_COOKIE_SECURE=false`
    (all pass). The tests should isolate themselves from `.env`.
50. ~~**Email isn't live yet.**~~ Live 2026-11-06: Gmail SMTP configured, and
    test emails from the scheduler (`notify_test`) and the watchdog
    (`--test-email`) were accepted by the server.
52. **`docker` on this VM is Docker CE, not Podman.** Docker CE 29.9 runs
    alongside Podman for other projects. CycleDays commands must use
    `podman-compose` / `podman`; `docker compose` fails with "image not
    found" (2026-11-06).
51. **A reboot that spans midnight loses that day's plan.** The start-up
    catch-up only plans "tomorrow" when the scheduler starts after 20:00. If
    the host is down from before 20:00 until after midnight, the watchdog
    reports the missing plan and it's planned by hand
    (`scripts.plan_day --date`). Planning "today" automatically at start-up
    would close this; it changes coaching behaviour, so it needs approval.
53. **`INTERVALS_API_KEY` / `INTERVALS_ATHLETE_ID` in `.env` are unused**
    since Phase 10 (the key lives encrypted in `intervals_connections`).
    Only `scripts.connect_intervals --from-env` reads them; they can be
    removed from `.env`. `CREDENTIALS_ENCRYPTION_KEY` must be backed up.
54. **Accounts are invite-only** (Phase 10). Self sign-up with email
    verification, password reset by email and Intervals.icu OAuth are
    needed before opening the app to the public.
55. **Pre-migration backup** `coach-pre-0007.dump` (git-ignored, mode 600,
    personal data) is in the project folder; delete it once Phase 10 is
    approved.
