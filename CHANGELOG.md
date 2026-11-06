# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Phase 9, steps 1–2 (the rest of Phase 9 is on hold). See
  [docs/OPERATIONS.md](docs/OPERATIONS.md).
  - Email notifications (`app/notifications.py`, standard-library SMTP with
    STARTTLS/SSL; sending never raises). Settings `NOTIFY_EMAIL_ENABLED`,
    `NOTIFY_EMAIL_TO`, `NOTIFY_EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`,
    `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
    `SMTP_TIMEOUT_SECONDS`, `NOTIFY_DAILY_SUMMARY`, `NOTIFY_REPEAT_HOURS`,
    `NOTIFY_POLL_FAILURE_THRESHOLD`.
  - Alert rules and tracker (`app/alerts.py`): poll and daily-plan problems,
    consecutive-failure threshold for polls, repeat interval, "resolved"
    messages, and a daily summary that is quiet on unchanged re-runs.
  - Scheduler: `run_job` / `JobRun` (ok, locked, failed), `JobNotifier`, and a
    `heartbeat` job writing `/tmp/cycledays-scheduler.heartbeat` every
    minute. `run_locked` keeps its old behaviour.
  - `scripts/notify_test.py`: send a test email.
  - Host watchdog `deploy/watchdog/cycledays_watchdog.py` (Python 3.9+,
    standard library) with `deploy/systemd/cycledays-watchdog.{service,timer}`:
    containers, `/health/ready`, daily plan present, reboot notice.
- Web login. Every page and API except `/`, `/login`, `/logout`,
  `/static/*` and the health checks needs a signed-in user.
  - `coach.app_users` (migration 0006): username, scrypt password hash,
    active flag, last login.
  - `app/auth.py`: password hashing, HMAC-signed session cookies
    (`HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS) and login throttling.
  - `app/api/auth.py`: `/login` form, `/logout`, `/api/session`, and the
    gate in front of the app (redirect to the login page or 401).
  - `scripts/manage_users.py`: add, passwd, enable, disable, delete, list.
  - Settings `AUTH_SECRET_KEY`, `AUTH_SESSION_HOURS`, `AUTH_COOKIE_SECURE`.
  - The dashboard header shows the user and a "Sign out" button; the page
    goes to the login page when its session ends.

### Changed
- `docker-compose.yml`: the `scheduler` service has a healthcheck (heartbeat
  file younger than 5 minutes).
- The scheduler logs APScheduler's per-run "Running job" lines at WARNING
  only; `scheduler_job_finished` / `scheduler_job_failed` cover each run.
- `deploy/nginx/cyclingdays.conf` passes `/login`, `/logout` and
  `/api/session` to the app and no longer uses HTTP basic auth.
- The dashboard CSP allows `form-action 'self'` (for the sign-out form).
- `deploy/nginx/root-redirect.conf`: the bare host (`/`) redirects to
  `/cyclingdays/`.
- HTTPS on https://cyclingdays.in (Let's Encrypt, `cyclingdays.in` and
  `www.cyclingdays.in`, renewed by certbot's timer; nginx reloads through a
  deploy hook). `deploy/nginx/cyclingdays-tls.conf`: HTTP → HTTPS, www →
  apex, HSTS for 30 days. Plain-HTTP requests by IP for `/` and
  `/cyclingdays` go to the HTTPS site. `.env`: `AUTH_COOKIE_SECURE=true`.
- New look for both pages. Login: split layout with a road-cycling photo
  (Urban Vintage on Unsplash, Unsplash License; self-hosted as
  `static/login-hero.jpg` and a smaller `login-hero-small.jpg` for phones)
  under the headline, in `static/login.css`. Dashboard: sticky translucent top
  bar with a chainring logo, segmented range picker, avatar and sign-out
  button; a hero band with a mountain-route drawing; rounded cards and tiles
  with soft shadows and gradient accents; two-column tiles on phones. All
  artwork is inline SVG, so the CSP is unchanged and nothing loads from
  other sites.
- Pages send `Referrer-Policy: same-origin` (was `no-referrer`, which made
  browsers send `Origin: null` on the login form and broke sign-in).

## [0.8.0] — 2026-11-05 — Phase 8: dashboard

### Added
- A read-only web dashboard at `/dashboard` (`/` redirects there). It
  shows:
  - athlete profile;
  - current FTP (W/kg, zones, Intervals.icu match);
  - fitness, fatigue and form trends (provisional days marked);
  - readiness;
  - the upcoming workout with its power profile;
  - a 5-week training calendar;
  - completed workouts with their evaluation;
  - weekly volume against plan targets;
  - power-zone distribution;
  - AI coaching assessments;
  - long-term plan progress.
- `app/web/`: static HTML, one JS and one CSS file. Hand-built SVG charts
  with tooltips, keyboard focus and table views; light and dark mode;
  phone layout. No framework, build step, CDN or new dependency.
- `app/api/dashboard.py`: JSON API `/api/dashboard/{summary, fitness,
  weekly, zones, calendar, workouts, assessments, plan}` with range limits,
  404/422 errors and `Cache-Control: no-store`.
- `app/services/dashboard.py` (`dashboard/v1`): read-only queries plus
  pure aggregations (weekly volume, zone totals with their sources, plan
  progress, calendar day states).
- Security headers on every response (`nosniff`, `no-referrer`,
  `X-Frame-Options: DENY`), and a strict same-origin CSP for the page.
- `docs/DASHBOARD.md`; 20 new tests (18 unit, 2 integration).

### Changed
- Ride evaluation `evaluation/v2`: stores seconds per power zone from the
  power stream at the ride's FTP.
- `training_metrics._load_for` is now public as `load_for` (reused by the
  dashboard).

## [0.7.0] — 2026-11-05 — Phase 7: adaptive coaching

### Added
- `app/training/plan_builder.py` (v1): a deterministic long-term plan in
  4-week blocks (3 load weeks at +8 % plus a recovery week), base → build,
  with weekly TSS and time targets for a weekly-hours range. It's
  open-ended, or ends with peak and taper weeks before an event date.
  `scripts/create_plan.py`.
- `app/training/progression.py` (v1): the day's prescription (maximum session
  type, target and maximum TSS, maximum minutes) from the plan week and the
  day role. The week is capped at 110 % and missed load is never moved.
- `app/training/adaptation.py` (v1): lower-only adaptation from recent
  evaluations (key-session struggle, high RPE, decoupling, LLM review) and
  last week's compliance.
- `app/training/evaluation.py` (v1): evaluates a completed ride against its
  planned workout (power stream step by step or totals, HR, aerobic
  decoupling, RPE versus expected, wellness) and assigns an outcome.
- `app/graph/guidance.py`: the strictest of readiness, plan and adaptation.
- `app/agents/performance_evaluator.py` and prompt `performance_review/v1`:
  an LLM review limited to keep, reduce or recover.
- `app/services/activity_processor.py`: detection, an event inbox
  (`activity:<id>:<revision>`, `SKIP LOCKED`), evaluation, and marking
  missed workouts. `scripts/process_activities.py`.
- `app/services/daily_planner.py` and `app/scheduler.py` (APScheduler 3,
  advisory job locks), with a `scheduler` service in docker-compose.
- `IntervalsClient.get_activity_streams`.
- Migration `0005`: `workout_performance.outcome` (CHECK), `decoupling_pct`,
  `session_date`, `activity_revision`.
- Settings `SCHEDULER_POLL_MINUTES`, `SCHEDULER_PLAN_TIME`,
  `ACTIVITY_LOOKBACK_DAYS` and `PLAN_HORIZON_DAYS`.
- Dependency: `apscheduler==3.11.3`.
- `docs/ADAPTIVE_COACHING.md`; 46 new tests.

### Changed
- Workout validator v2: a new `plan` group (session type, TSS and minutes
  within the plan's day limits), re-run before publishing.
- Graph `daily_plan/v3`: `context.guidance` and `recent_evaluations`. Rest
  days follow the plan. The objective guard checks the plan.
- Prompts: `objective_selection/v3` and `workout_planning/v3`.
- The fallback workout respects the plan's maximum minutes.
- `scripts/plan_day.py` uses `DailyPlanner`; `--sync` also evaluates rides.

## [0.6.0] — 2026-11-05 — Phase 6: icTrainer and COROS integration

### Added
- `app/services/sync_verification.py`: one check per synchronization
  direction:
  - COROS link;
  - Intervals.icu → icTrainer;
  - icTrainer ERG execution;
  - icTrainer → Intervals.icu;
  - COROS → Intervals.icu activities and wellness;
  - Intervals.icu → COROS;
  - duplicate recordings.

  Each check is evaluated from synchronized data, with manual confirmation for
  directions the API can't observe.
- `app/training/activity_dedup.py` (v1): detects the same ride recorded twice
  and picks one primary recording deterministically.
- Migration `0004`:
  - activity provenance (`origin`, `origin_client`, `device_name`,
    `origin_external_id`, `paired_event_id`; back-filled from
    `raw_payload`);
  - `duplicate_of_activity_id`;
  - the `sync_checks` table (stores a row only when a result changes).
- `scripts/check_sync.py` (`--sync`, `--days`, `--confirm … --note …`).
- `docs/SYNC.md`: verified directions, limitations, athlete checklist.
- Tests: 24 new (22 unit, 2 integration). 241 in total.

### Changed
- `CALCULATION_VERSION` is now `v2`: duplicate recordings are excluded from
  training load. The formulas are unchanged; on real data v2 equals v1.
- The activity sync links duplicate recordings after each run;
  `ActivityRepository.list_until` excludes them unless asked.

### Verified live (2026-11-05)
- COROS wellness → Intervals.icu: resting HR, HRV (rMSSD), sleep duration,
  average sleeping HR and steps.
- Not yet observable: icTrainer pick-up and upload, ERG execution and COROS
  activities. The account has no ride yet.

## [0.5.0] — 2026-11-05 — Phase 5: ERG workout generation and publishing

### Added
- `app/training/sessions.py`: session types, per-type duration and
  interval rules (moved here from the agent schemas).
- `app/training/workout_generator.py`: `StructuredWorkout` with name, date,
  FTP reference, objective, sections, per-step % FTP and watts, and an
  NP/IF/TSS estimate.
- `app/training/workout_validator.py` (v1): power, duration, intervals,
  recovery, readiness, weekly distribution and recent-load rules, each group
  reported as passed, failed or skipped.
- `app/integrations/intervals_workouts.py`: native Intervals.icu workout
  text (verified against the real API), event payload, step-by-step
  verification of the parsed `workout_doc`.
- `IntervalsClient.upsert_events`, `get_event`, `delete_event`.
- `app/services/workout_publisher.py`: lock, gate (re-validation, FTP vs
  profile and Intervals.icu, no competing workouts), duplicate repair,
  bulk upsert keyed by `external_id`, read-back verification (deleting the
  event on mismatch), and an append-only audit of every action.
- `PlannedWorkoutRepository`, `PublishingAuditRepository`.
- `scripts/publish_workout.py` (`--dry-run`, `--verify`).
- `docs/WORKOUTS.md`.
- Tests: 34 new: 20 unit, 12 publisher integration (fake Intervals.icu
  calendar), 2 graph end-to-end. 217 in total (163 unit, 54 integration).

### Changed
- The graph (`daily_plan/v2`): the workout guard is the full validator; the
  objective guard checks duration ranges and hard-session spacing;
  `finalize` stores the day's `planned_workouts` row (one per day, updated on
  re-planning); an invalid fallback workout becomes rest.
- Prompts `objective_selection/v2` and `workout_planning/v2` (rule text
  generated from the validator constants). The context gains `today` and
  `recent_planned`.
- `scripts/plan_day.py` publishes after planning (`--no-publish`; exit code
  4 when publishing is blocked or fails).
- Config: `INTERVALS_PUBLISH_ENABLED` (default `true`).
- Version 0.5.0.

## [0.4.0] — 2026-11-05 — Phase 4: LangGraph coaching graph (OpenAI)

### Added
- `app/agents/llm.py`: `OpenAIStructuredLLM` (Responses API structured
  outputs, own retry with backoff and jitter for timeouts, connection
  errors, 408/409/429/5xx; typed `LLMUnavailableError` / `LLMOutputError`;
  records the reported model, tokens and latency).
- `app/agents/schemas.py`: strict structured-output schemas
  (`TrainingAnalysis`, `RecoveryAssessment`, `TrainingObjective`,
  `WorkoutDraft`).
- `app/agents/base.py`: agent runner with a correction loop and a
  `CallRecord` for every LLM call.
- Agents: training analyst, recovery analyst, objective selector, workout
  planner.
- `app/graph/`: shared `CoachState`, versioned prompts (`*/v1`, SHA-256
  stored), deterministic guards (`effective_cap`, objective and workout
  checks), conservative fallback plans, explicit conditional routing, the
  node set and `DailyPlanRunner`.
- PostgreSQL checkpointing (`AsyncPostgresSaver`, schema `langgraph`),
  thread per athlete and day: completed runs aren't repeated, interrupted
  runs resume.
- `coach_decisions` audit: one row per LLM call (model, prompt version,
  input, output, violations, usage) plus one `daily_plan` row per run.
- `scripts/plan_day.py` (`--date`, `--force`, `--sync`).
- `docs/COACHING_GRAPH.md`.
- Tests: 34 new (22 unit, 11 integration and 1 logging; 183 in total).

### Changed
- Config: `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-6.1-sol`),
  `OPENAI_BASE_URL`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_ATTEMPTS`,
  `OPENAI_BACKOFF_*`, `LLM_MAX_CORRECTIONS`.
- Dependencies: `openai`, `langgraph`, `langgraph-checkpoint-postgres`.
- Log redaction no longer masks LLM token counts.
- Version 0.4.0.

## [0.3.0] — 2026-11-05 — Phase 3: training intelligence engine

### Added
- `app/training/zones.py`: Coggan 7-zone FTP power zones; FTP validation.
- `app/training/intensity.py`: ordered intensity levels used for caps.
- `app/training/metrics.py`: NP (with dropout and duration rules), IF, TSS,
  activity load selection (power → source load → unknown), daily loads with
  coverage, CTL/ATL/TSB (τ 42/7), ramp, rolling 7-day TSS, completeness and
  authoritative flags, ISO-week load, time in zones.
- `app/training/performance.py`: planned-versus-actual step status,
  completion %, work completion %, power deviation %.
- `app/training/readiness.py`: readiness from personal baselines (ln-rMSSD
  z-score and 7-day trend, resting HR, sleep), subjective fatigue,
  injury/illness/pain, form and ramp, recent hard sessions and plan phase.
  Output: status, max intensity, confidence, factors and caps with reasons.
- `app/training/periodization.py`: validated plan-phase schema.
- `app/services/training_metrics.py` and `scripts/training_report.py`.
- Repositories: `TrainingMetricRepository`, `TrainingPlanRepository`, plus
  query helpers.
- Migration `0003`: `daily_recovery.subjective_injury`.
- `docs/TRAINING_ENGINE.md`: formulas, thresholds, sources, assumptions.
- Tests: 66 new unit tests, 5 new integration tests (154 total).

### Changed
- Sync cursor records `coverage_start` (earliest day ever synced).
- `INTERVALS_HISTORY_DAYS` default 90 → 180 (CTL needs 96 days of history).
- Wellness mapping stores `injury`.
- Version 0.3.0.

## [0.2.0] — 2026-11-05 — Phase 2: Intervals.icu integration

### Added
- `app/integrations/intervals_client.py`: async Intervals.icu client (HTTP
  Basic with API key). Endpoints: athlete, activities, single activity,
  wellness, events and planned workouts (`category=WORKOUT`). Retries with
  exponential backoff and jitter for 429/5xx/network errors, honours
  `Retry-After`, raises typed errors, logs structured errors without secrets.
- `app/integrations/intervals_mappers.py`: documented field mapping for
  activities, wellness, calendar events and the athlete. Detects "Hidden"
  activity stubs and redacts secrets from stored payloads.
- `app/services/intervals_sync.py`: incremental, idempotent sync (date-window
  cursor with overlap, `--full` re-sync, change detection, calendar soft
  delete, per-athlete advisory lock, per-resource failure tracking).
- `scripts/sync_intervals.py`: CLI (`--full`, `--resource`, `--status`). JSON
  report on stdout, logs on stderr.
- Repositories: `ActivityRepository`, `DailyRecoveryRepository`,
  `CalendarEventRepository`, `SynchronizationStateRepository` and the
  advisory-lock helper.
- Migration `0002`: table `coach.calendar_events`, plus
  `athlete_profile.intervals_snapshot` and `intervals_synced_at`.
- `docs/INTERVALS_ICU.md`: endpoints, auth, mapping, sync strategy,
  verification results.
- Tests: client (mock transport), mappers, end-to-end sync against
  PostgreSQL. 78 tests in total.

### Changed
- `httpx2` moved from dev to runtime requirements (no new dependency).
- `configure_logging()` accepts an output stream (CLI scripts log to stderr)
  and silences the HTTP library's per-request INFO logs.
- `logging_config.redact_secrets()` is now public.
- Version 0.2.0.

### Configuration
- New variables: `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`,
  `INTERVALS_BASE_URL`, `INTERVALS_TIMEOUT_SECONDS`, `INTERVALS_MAX_RETRIES`,
  `INTERVALS_BACKOFF_BASE_SECONDS`, `INTERVALS_BACKOFF_MAX_SECONDS`,
  `INTERVALS_HISTORY_DAYS`, `INTERVALS_SYNC_OVERLAP_DAYS`,
  `INTERVALS_CALENDAR_FUTURE_DAYS`.

## [0.1.0] — 2026-11-04 — Phase 1: infrastructure and foundation

### Added
- Python 3.12 project layout (`app/`, `migrations/`, `tests/`, `scripts/`, `docs/`).
- FastAPI application factory with lifespan-managed async SQLAlchemy engine.
- `GET /health` (liveness) and `GET /health/ready` (DB + migration revision).
- Request-ID middleware (`X-Request-ID`) and request logging.
- Structured JSON / console logging with recursive secret redaction.
- `pydantic-settings` configuration; `.env.example`.
- PostgreSQL 16 via Docker Compose (Podman-compatible), host port 5434.
- Alembic setup (version table in `coach` schema, autogenerate scoped to `coach`).
- Migration `0001` — 11 application tables in schema `coach`, `updated_at`
  trigger, append-only `publishing_audit`, reserved empty `langgraph` schema.
- `scripts/seed_athlete_profile.py` (idempotent initial athlete profile).
- `scripts/wait_for_db.py` and container entrypoint (wait → migrate → serve).
- Dockerfile (non-root) and compose `app` service with readiness healthcheck.
- Unit and integration test suites; ruff and mypy (strict) configuration.
- Documentation: README, ARCHITECTURE, IMPLEMENTATION_STATUS.

### Configuration
- New variables: `APP_ENV`, `APP_TIMEZONE`, `LOG_LEVEL`, `LOG_FORMAT`,
  `API_PORT`, `API_BIND_ADDRESS`, `POSTGRES_HOST`, `POSTGRES_PORT`,
  `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (required),
  `DATABASE_URL` (optional override), `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`,
  `DB_ECHO`, `RUN_MIGRATIONS_ON_START`, `ATHLETE_*` seed values.
