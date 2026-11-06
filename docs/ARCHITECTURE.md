# Architecture

_Last updated: Phase 9 steps 1–2 (2026-11-06)._

## 1. System context

```
            ┌──────────────┐   sync (vendor)   ┌───────────────┐
            │    COROS     │ ────────────────▶ │               │
            └──────────────┘                   │               │
            ┌──────────────┐   sync (vendor)   │ Intervals.icu │  ◀── source of truth:
            │  icTrainer   │ ◀───────────────▶ │               │      calendar, planned
            └──────────────┘                   │               │      workouts, activities,
                                               └───────▲───────┘      wellness, history
                                                       │ REST API (Phase 2+)
                                     ┌─────────────────┴──────────────────┐
                                     │        AI Cycling Coach            │
                                     │  FastAPI · APScheduler · LangGraph │
                                     │  deterministic training engine     │
                                     │  OpenAI (interpretation only)      │
                                     └─────────────────┬──────────────────┘
                                                       │
                                              PostgreSQL 16
                                    schema "coach"     │  schema "langgraph"
                                    (app data, Alembic)│  (checkpoints, Phase 4)
```

The coach never talks to COROS or icTrainer directly for planning; it reads
from and publishes to Intervals.icu only. Which data actually flows through the
vendor integrations will be verified and documented in Phase 6.

## 2. Components (current and planned)

| Package | Responsibility | Phase |
|---|---|---|
| `app/main.py` | FastAPI app factory, lifespan (engine), request-ID middleware | 1 ✅ |
| `app/config.py` | `pydantic-settings` configuration; athlete seed values | 1 ✅ |
| `app/logging_config.py` | JSON/console structured logging, secret redaction | 1 ✅ |
| `app/database/` | async engine/sessions, ORM models, repositories (idempotent upserts) | 1 ✅, 2 ✅ |
| `app/api/routes.py` | `/health`, `/health/ready` | 1 ✅ |
| `app/integrations/intervals_client.py` | async Intervals.icu REST client: auth, retries, typed errors | 2 ✅ (read), 5 ✅ (publish) |
| `app/integrations/intervals_mappers.py` | pure JSON → column mapping, secret redaction | 2 ✅ |
| `app/services/intervals_sync.py` | incremental sync (athlete, activities, wellness, calendar) | 2 ✅ |
| `scripts/sync_intervals.py` | CLI for sync and sync status | 2 ✅ |
| `app/training/activity_dedup.py` | same ride recorded twice → one counted | 6 ✅ |
| `app/services/sync_verification.py` | per-direction sync checks (icTrainer, COROS), manual confirmations, `sync_checks` history | 6 ✅ |
| `scripts/check_sync.py` | CLI: sync checks and confirmations | 6 ✅ |
| `app/training/zones.py`, `intensity.py` | FTP power zones, ordered intensity levels | 3 ✅ |
| `app/training/metrics.py` | NP/IF/TSS, activity load, CTL/ATL/TSB, weekly load, time in zones | 3 ✅ |
| `app/training/performance.py` | planned-versus-actual completion and deviation | 3 ✅ |
| `app/training/readiness.py` | readiness from personal baselines + safety caps | 3 ✅ |
| `app/training/periodization.py` | plan-phase schema and lookup | 3 ✅ |
| `app/services/training_metrics.py` | DB → engine → `training_metrics`, readiness | 3 ✅ |
| `scripts/training_report.py` | CLI: recompute metrics, print readiness | 3 ✅ |
| `app/training/sessions.py` | session types and per-type rules | 5 ✅ |
| `app/training/workout_generator.py` | structured ERG workout, NP/IF/TSS estimate | 5 ✅ |
| `app/training/workout_validator.py` | deterministic workout validation (7 rule groups) | 5 ✅ |
| `app/integrations/intervals_workouts.py` | Intervals.icu workout text, event payload, read-back verification | 5 ✅ |
| `app/services/workout_publisher.py` | gate → upsert → verify → audit | 5 ✅ |
| `scripts/publish_workout.py` | CLI: publish, dry run, verify | 5 ✅ |
| `app/agents/llm.py`, `schemas.py`, `base.py` | OpenAI structured-output wrapper (retries, typed errors), strict schemas, agent runner with a correction loop | 4 ✅ |
| `app/agents/*_analyst.py`, `workout_planner.py` | the four LLM agents | 4 ✅ |
| `app/graph/` | state, versioned prompts, guards, fallback, routing, nodes, `DailyPlanRunner` (Postgres checkpointer) | 4 ✅ |
| `scripts/plan_day.py` | CLI: plan one day | 4 ✅ |
| `app/training/plan_builder.py`, `progression.py`, `adaptation.py`, `evaluation.py` | long-term plan, day prescription, lower-only adaptation, ride evaluation | 7 ✅ |
| `app/graph/guidance.py` | readiness ∧ plan ∧ adaptation → the day's limits | 7 ✅ |
| `app/agents/performance_evaluator.py` | LLM ride review (keep, reduce or recover) | 7 ✅ |
| `app/services/activity_processor.py` | detect → inbox → evaluate → mark missed | 7 ✅ |
| `app/services/daily_planner.py` | poll and plan-day orchestration | 7 ✅ |
| `app/scheduler.py` | APScheduler process (poll, daily plan), job advisory locks | 7 ✅ |
| `scripts/create_plan.py`, `process_activities.py` | CLIs | 7 ✅ |
| `app/services/dashboard.py` | read-only dashboard queries and pure aggregations | 8 ✅ |
| `app/api/dashboard.py`, `app/web/` | dashboard JSON API, static page (HTML, JS and SVG charts, CSS), security headers | 8 ✅ |
| `app/notifications.py`, `app/alerts.py` | email notifier; alert rules, tracker and email texts; `JobNotifier` and heartbeat in `app/scheduler.py` | 9 ✅ (steps 1–2) |
| `deploy/watchdog/`, `deploy/systemd/` | host watchdog (containers, API, daily plan, reboots) and its systemd timer | 9 ✅ (steps 1–2) |
| `app/api/webhooks.py` | inbound webhooks (needs an Intervals.icu OAuth app and API auth) | 9 |

Packages for later phases currently contain only a docstring `__init__.py`.

## 3. Key design decisions

### 3.1 One PostgreSQL driver: psycopg 3
SQLAlchemy uses `postgresql+psycopg://` for both the async app engine and the
synchronous Alembic engine. LangGraph's official Postgres checkpointer
(`langgraph-checkpoint-postgres`) is built on psycopg 3, so this avoids
carrying both `asyncpg` and `psycopg`.

### 3.2 Schema separation
* `coach` — all application tables **and** `coach.alembic_version`.
* `langgraph` — reserved (created empty by migration 0001) for LangGraph's
  checkpointer, which manages its own tables. Alembic autogenerate is
  restricted to the `coach` schema (`migrations/env.py`), so it will never try
  to modify or drop checkpoint tables. Downgrading 0001 drops `langgraph` with
  `RESTRICT`, so it fails rather than destroying checkpoints.
* Every connection pins `search_path=public`. Reason: the DB role is called
  `coach`; PostgreSQL's default `"$user", public` search path would otherwise
  make `coach` the default schema, which silently broke Alembic's schema
  filtering during development. All app SQL is schema-qualified.

### 3.3 Data model (migrations `0001`–`0005`)

| Table | Purpose | Integrity / idempotency |
|---|---|---|
| `athlete_profile` | athlete settings (FTP, weight, objectives, tz, preferences JSONB) | unique `slug`, unique `intervals_athlete_id`; CHECK FTP 1–600 W, weight, height; FTP nullable (= unknown → publishing must refuse) |
| `activities` | completed activities from Intervals.icu; `raw_payload` JSONB | **unique `(source, source_activity_id)`** prevents duplicate imports |
| `daily_recovery` | per-day wellness (RHR, HRV rMSSD, sleep, subjective, illness/pain flags) | unique `(athlete_id, recovery_date, source)` |
| `training_plans` | long-term plan, `phases` JSONB | partial unique: one `active` plan per athlete |
| `planned_workouts` | coach-planned workouts, `structure` + `validation_result` JSONB | **unique `external_id`** (stable publish key), unique `intervals_event_id`, **partial unique `(athlete_id, workout_date)` for live statuses** → never two live workouts per day; status CHECK |
| `workout_performance` | planned-vs-actual evaluation | unique `activity_id` (an activity is evaluated once) |
| `training_metrics` | deterministic CTL/ATL/TSB etc. + `data_completeness` | unique `(athlete_id, metric_date, calculation_version)` → reproducible, versioned |
| `coach_decisions` | model name, prompt version, input snapshot, structured output | indexed by athlete/date and graph thread |
| `synchronization_state` | incremental sync cursor + failure tracking | unique `(athlete_id, source, resource)` |
| `webhook_events` | inbound events | **unique `(source, dedup_key)`** event deduplication |
| `publishing_audit` | every publish attempt incl. skips and failures | **append-only** (trigger blocks UPDATE/DELETE); FKs `RESTRICT` |
| `calendar_events` (0002) | mirror of the Intervals.icu calendar (planned workouts, notes, …) | **unique `(source, source_event_id)`**; soft delete `deleted_at` |
| `sync_checks` (0004) | history of synchronization check results and athlete confirmations | a row only when a result changes; status CHECK |

Migration `0005` adds `workout_performance.outcome` (CHECK), `decoupling_pct`,
`session_date` and `activity_revision`. `webhook_events` serves as the
inbound event inbox for polled activities (Phase 7).

Migration `0004` also adds activity provenance (`origin`, `origin_client`,
`device_name`, `origin_external_id`, `paired_event_id`, back-filled from
`raw_payload`) and `duplicate_of_activity_id` (self-FK, `SET NULL`).

Conventions: `BIGINT` identity PKs; `timestamptz` in UTC; training days as
`date` in the athlete's timezone; statuses as `text` + `CHECK` (easier to evolve
than PG enums); units in column names; `updated_at` maintained by a DB trigger
(`coach.set_updated_at()`) so it is correct even for raw SQL.

Migration `0002` also adds `athlete_profile.intervals_snapshot` (redacted
athlete object) and `intervals_synced_at`. The Intervals.icu field mapping is
documented in [INTERVALS_ICU.md](INTERVALS_ICU.md). The verbatim payload
(secrets redacted) is always kept in `raw_payload`.

### 3.4 Intervals.icu synchronization (Phase 2)

Details are in [INTERVALS_ICU.md](INTERVALS_ICU.md). Key decisions:

* **HTTP client: `httpx2`.** It was already installed (Starlette's TestClient
  uses it), so it moved from dev to runtime requirements and adds no new
  dependency. It's async, and `MockTransport` makes the client testable
  without network access. Retries are written by hand (≈30 lines) rather
  than adding a retry library.
* **Date-window incremental sync.** The API has no "modified since"
  filter. Each resource stores a cursor, and the next run re-fetches from
  `cursor − overlap`. This trades a few redundant rows per run for never
  missing late uploads or edits.
* **Change detection in SQL.** `ON CONFLICT … DO UPDATE … WHERE raw_payload
  IS DISTINCT FROM excluded.raw_payload`, plus `RETURNING (xmax = 0)`,
  separates inserted, updated and unchanged rows in one statement.
* **Our profile is authoritative.** Intervals.icu values (FTP, weight,
  timezone) are compared and reported, never applied automatically.
* **Fail-safe linking.** If the API key returns a different athlete than
  the linked one, the whole sync stops.
* **One transaction + advisory lock per resource.** A failure in one
  resource doesn't block the others, and its state row records the error.
* **No HTTP trigger endpoint** until the API has authentication. Sync is a
  CLI now; the scheduler comes in Phase 7.

### 3.5 Training engine (Phase 3)

Details are in [TRAINING_ENGINE.md](TRAINING_ENGINE.md). Key decisions:

* **Pure functions + a thin service.** `app/training` has no I/O, so every
  formula is unit-tested against reference values. The service only
  gathers inputs and persists outputs.
* **Versioned results.** `training_metrics` is unique per
  (athlete, day, `calculation_version`). Formula changes get a new version
  instead of rewriting history.
* **Unknown ≠ zero.** Coverage from the sync cursor separates rest days from
  days never synced. Completeness and `*_authoritative` flags travel with
  every row.
* **Readiness = caps with reasons.** The engine outputs a maximum intensity
  that the LLM can't exceed, rather than a score the LLM could reinterpret.
  Missing data lowers the cap; it never raises it.
* **No new dependencies** (`math`/`statistics` only; no numpy or pandas).

### 3.6 Coaching graph (Phase 4)

Details are in [COACHING_GRAPH.md](COACHING_GRAPH.md). Key decisions:

* **The LLM chooses; code decides what is allowed.** The deterministic
  readiness cap is an upper bound; the LLM can only lower it. Every output
  passes the strict schema, Pydantic and then the deterministic guards.
  Rejections get a correction prompt, then a conservative fallback. Invalid
  output is never accepted.
* **The OpenAI SDK directly (no LangChain).** One small wrapper over
  `responses.parse` with our own retry policy (SDK retries off), so there's
  one fewer dependency layer, and the reported model and token usage are
  recorded for every call.
* **Checkpoints in Postgres** (schema `langgraph`, official
  `AsyncPostgresSaver` on psycopg 3, sharing the existing driver). The
  thread id is the athlete and day, which gives idempotency and resume for
  free.
* **Audit in `coach_decisions`.** One row per LLM call (including rejected
  ones) and one `daily_plan` row, written in one transaction in `finalize`.
  Prompts are versioned and hashed.

### 3.7 Workouts and publishing (Phase 5)

Details are in [WORKOUTS.md](WORKOUTS.md). Key decisions:

* **One validator, three call sites:** the LLM guard, `finalize` and the
  publisher. The model is corrected against the rules that decide
  publication, and the publisher re-checks the stored workout instead of
  trusting the database.
* **Publishing sits outside the graph.** It's an idempotent side effect with
  its own audit, so retrying it never re-runs the LLM.
* **Idempotency comes from three things together:** a stable `external_id`
  per athlete and day, the bulk-upsert endpoint (verified to update rather
  than duplicate), and a list-then-repair step that deletes extra copies.
  One `planned_workouts` row per day is enforced by the existing partial
  unique index.
* **Verification against Intervals.icu's own parse.** Step lines contain no
  LLM text, and the event is read back and compared step by step. A mismatch
  removes the event.
* **No competing plans.** A workout on the calendar that wasn't created by
  the coach blocks publishing for that day.
* **No migration was needed.** `planned_workouts` (`structure`,
  `validation_result` as JSONB) and the append-only `publishing_audit` were
  designed in Phase 1.

### 3.8 icTrainer and COROS (Phase 6)

Details are in [SYNC.md](SYNC.md). Key decisions:

* **No direct icTrainer or COROS integration.** Both connect to
  Intervals.icu through their own supported integrations, so CycleDays keeps
  one source of truth and one API. Writing to them directly would risk
  competing plans.
* **Verify by evidence, per direction.** Each direction has its own check,
  based on data visible through the Intervals.icu API (activity `source`,
  OAuth client, `paired_event_id`, wellness fields, athlete settings).
  Nothing is assumed to sync both ways.
* **Unobservable directions are labelled honestly.** They are `configured`
  or `not_observed` until the athlete confirms them (`confirmed`). Data
  evidence always overrides a confirmation.
* **Change-only history.** `sync_checks` stores a row only when a result
  changes, so a future scheduler can run the checks often without filling
  the table.
* **Duplicate recordings are excluded from load,** with the primary chosen
  deterministically. Because this changes the computed totals,
  `CALCULATION_VERSION` is now `v2`.

### 3.9 Adaptive coaching (Phase 7)

Details are in [ADAPTIVE_COACHING.md](ADAPTIVE_COACHING.md). Key decisions:

* **A deterministic plan and a deterministic prescription.** The long-term
  plan and each day's limits are pure Python and versioned. The LLM chooses
  *within* them, and the validator's `plan` group enforces them up to
  publication.
* **Adaptation only lowers.** Recent evaluations, the LLM review (keep,
  reduce or recover; no increase option) and low compliance can ease the
  plan, never push it. Progression comes only from the plan's ramp.
* **No cramming by construction.** Daily budgets are fixed shares of the
  week; missed load is never redistributed.
* **An event inbox for idempotency.** Polled rides become inbox events keyed
  by activity revision (unique), claimed with `SKIP LOCKED`, and their
  evaluations are upserted per activity. The same table can receive real
  webhooks later.
* **The performance review is a plain agent call, not a graph.** It's a
  single step with its own idempotency (the inbox); a one-node LangGraph
  adds nothing. It's audited in `coach_decisions` like the graph nodes.
* **The scheduler is its own process** (compose service `scheduler`), so the
  API container stays stateless. PostgreSQL advisory locks make duplicate
  runs safe even with two scheduler instances.

### 3.10 Dashboard (Phase 8)

Details are in [DASHBOARD.md](DASHBOARD.md). Key decisions:

* **A static page plus a JSON API, not server-rendered templates or an SPA
  build.** FastAPI serves one HTML, one JS and one CSS file. There's no
  Jinja2, Node toolchain or CDN, so no new dependency and nothing to build in
  the image. The API is plain JSON, so any later frontend can reuse it.
* **One endpoint per section.** A failing section shows its own error, and
  the range filter reloads only the sections it scopes.
* **Read-only by construction.** `DashboardService` only selects; a test
  asserts that the row counts are unchanged after every section is read.
  The dashboard shows the coach's stored numbers (`training_metrics`,
  evaluations, decisions) and never recomputes its own.
* **Pure aggregations** (weekly volume, zone totals, plan progress,
  calendar) are module functions with unit tests. The service only loads
  rows.
* **Security:** a same-origin CSP without `unsafe-inline`; API strings are
  only inserted with `textContent`; responses use `no-store`. Authentication
  is part of Phase 9.
* **Power zones:** ride evaluation v2 stores seconds per zone from the power
  stream, because zones need the stream and no other place keeps it.

### 3.11 Health model
* `/health` — liveness only (process up). Used for "is it running".
* `/health/ready` — DB reachable within `HEALTH_DB_TIMEOUT_SECONDS` **and**
  applied Alembic revision equals the head shipped with the code. 503
  otherwise. The container healthcheck uses this endpoint, so a container on a
  stale schema is reported unhealthy.

### 3.12 Logging
Standard library only (no extra dependency). One JSON object per line with
`timestamp` (UTC), `level`, `logger`, `message`, `request_id` (from the
`X-Request-ID` header or generated), plus structured extras. Keys containing
`password`, `secret`, `token`, `api_key`, `authorization` are redacted
recursively (LLM token *counts* such as `input_tokens` are exempt). The same redaction (`redact_secrets`) cleans third-party payloads
before they're stored. Uvicorn logs are routed through the same handler.
CLI scripts log to stderr so stdout carries only their JSON result.

### 3.13 Configuration and secrets
All configuration via environment variables / `.env` (git-ignored, mode 600).
`POSTGRES_PASSWORD` has no default, so the app refuses to start without it.
Secrets are `SecretStr` and never appear in reprs or logs. Production secret
management is Phase 9.

### 3.13a Operations (Phase 9, steps 1–2)

Details are in [OPERATIONS.md](OPERATIONS.md). Key decisions:

* **Two senders, one config.** The scheduler emails what it sees (job
  results). A host watchdog emails what the scheduler can't report: itself
  being down or stuck, the API, a missing daily plan, a reboot. Both read the
  same `SMTP_*` / `NOTIFY_*` settings from `.env`.
* **Standard library only.** `smtplib` (no new dependency). The watchdog is a
  single host-Python script (3.9 compatible) that needs nothing from the app
  image, since the image may be what's broken. It uses `podman` and `psql`
  inside the db container.
* **Few emails.** One email per job run at most; poll problems wait for 2
  failures in a row; repeats every 12 h; "resolved" follow-ups. The daily
  summary doubles as a dead-man signal.
* **Alert state in memory (scheduler) and a JSON file (watchdog).** No
  migration. A scheduler restart can re-send a lasting problem once.
* **Heartbeat healthcheck.** The scheduler writes a file every minute and the
  compose healthcheck checks its age, so a blocked event loop shows as
  `unhealthy`.
* **Reboot restart via `podman-restart.service`**, not Quadlet or generated
  units, which would conflict with podman-compose. Podman 5's
  `should-start-on-boot` filter covers `unless-stopped`.

## 4. Deployment topology

`docker-compose.yml` (works with Docker Compose v2 and podman-compose):

* `db` — `postgres:16`, named volume `pgdata`, published on
  `127.0.0.1:5434` (5432/5433 are used by other projects on this VM).
* `app` — built from `Dockerfile` (python:3.12-slim, non-root uid 10001).
  The entrypoint waits for the DB, runs `alembic upgrade head` (unless
  `RUN_MIGRATIONS_ON_START=false`) and starts uvicorn on
  `${API_BIND_ADDRESS:-127.0.0.1}:8010`. It also serves the dashboard at
  `/dashboard`.
* Boot: Podman's `podman-restart.service` starts all three (restart policy
  `unless-stopped`); the host timer `cycledays-watchdog.timer` checks them
  every 10 minutes (Phase 9).
* `scheduler` — the same image, command `python -m app.scheduler`. It
  starts once `app` is healthy (so migrations have run) and doesn't run
  migrations itself (`RUN_MIGRATIONS_ON_START=false`). It publishes no port.

## 5. Deviations from the suggested structure

| Suggested | Actual | Reason |
|---|---|---|
| `cycling-coach/` root folder | repository root is `CycleDays/` | existing project directory |
| — | `app/logging_config.py` | structured logging needs a home outside `main.py` |
| — | `pyproject.toml`, `requirements-dev.txt` | tool config (pytest, ruff, mypy); dev deps kept out of the runtime image |
| `tests/` flat | `tests/unit`, `tests/integration` | unit tests run without a DB; integration tests use a throwaway DB |
| `scripts/` | `scripts/` is a Python package | scripts run as `python -m scripts.<name>` and reuse `app` code |
| asyncpg (common default) | psycopg 3 | see 3.1 |
| webhooks for new activities | polling every 30 min into an event inbox (`webhook_events`) | Intervals.icu webhooks need an OAuth app and a public endpoint; see 3.9 |
| performance evaluation as a graph | a deterministic evaluation plus one agent call | a single step; a graph adds nothing (see 3.9) |
| dashboard with a frontend framework | static HTML, JS and SVG served by FastAPI | no build toolchain or new dependency; see 3.10 |
| scheduler inside the API | a separate `scheduler` container | keeps the API stateless; locks make duplicate runs safe |
