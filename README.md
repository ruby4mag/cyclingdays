# AI Cycling Coach

An autonomous, adaptive endurance-cycling coach. It reads training and
recovery data from **Intervals.icu** (the source of truth), computes training
metrics deterministically in Python, uses **OpenAI** (via **LangGraph**) only
for interpretation and planning, validates every workout deterministically, and
publishes structured ERG workouts back to Intervals.icu for **icTrainer**.

> **Status:** Phases 1–8 are approved; the dashboard is at
> http://127.0.0.1:8010/dashboard. Phase 9 (production hardening): steps 1–2
> (email notifications, restart after reboot) are done; the rest is on hold.
> The scheduler service is running and publishes the next day's workout at
> 20:00 Asia/Kolkata (`docker compose stop scheduler` pauses it). See
> [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) and
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What works today

**Phase 1:**
- FastAPI service with `/health` (liveness) and `/health/ready` (database plus migration revision).
- PostgreSQL 16 in Docker Compose (Podman-compatible).
- SQLAlchemy 2 (async, psycopg 3) and Alembic, with 13 tables in schema `coach` (migrations 0001–0005).
- Structured JSON logging with request IDs and secret redaction.
- Idempotent athlete-profile seeding.

**Phase 2 (Intervals.icu):**
- Async API client with API-key auth, retries, backoff, `Retry-After` handling and typed errors.
- Incremental, duplicate-free sync of the athlete profile, activities (≥ 30 days of history), wellness and the calendar (including planned workouts).
- Sync state and failure tracking per resource. A CLI runs syncs and shows their status.
- Details: [docs/INTERVALS_ICU.md](docs/INTERVALS_ICU.md).

**Phase 3 (training engine):**
- Deterministic FTP zones, NP/IF/TSS, CTL/ATL/TSB, weekly load, power distribution, workout completion and power deviation.
- Daily metrics stored per calculation version, with completeness and "authoritative" flags.
- Readiness assessment against personal HRV, resting-HR and sleep baselines, with safety caps (illness, pain, injury, missing or stale data, recent hard sessions, plan phase).
- Details: [docs/TRAINING_ENGINE.md](docs/TRAINING_ENGINE.md).

**Phase 4 (coaching graph):**
- LangGraph workflow: context → training analysis → recovery assessment → objective → workout, with explicit conditional routing.
- OpenAI structured outputs with strict schemas, retries, and a correction loop.
- Deterministic guards the LLM can't bypass, and a conservative fallback when the LLM is unavailable or keeps producing invalid output.
- PostgreSQL checkpointing: a day is planned once; interrupted runs resume.
- Every LLM call and every daily plan is stored in `coach_decisions` (model, prompt version, input, output).
- Details: [docs/COACHING_GRAPH.md](docs/COACHING_GRAPH.md).

**Phase 5 (workouts and publishing):**
- Structured ERG workout: name, date, duration, FTP reference, objective, warmup/main/recovery/cooldown, target power (% FTP and watts) per step, estimated NP/IF/TSS.
- Deterministic validator: power, duration, interval and recovery structure, readiness, weekly distribution, recent load. The LLM is corrected against the same rules.
- Published to the Intervals.icu calendar (native workout text) with a stable `external_id`: re-runs never duplicate, and each event is read back and verified step by step.
- Never publishes over a workout planned by someone else, or when the FTP differs from Intervals.icu. Every action goes to the append-only `publishing_audit`.
- Details: [docs/WORKOUTS.md](docs/WORKOUTS.md).

**Phase 6 (icTrainer and COROS):**
- Each synchronization direction (Intervals.icu → icTrainer, ERG execution, icTrainer → Intervals.icu, COROS activities and wellness, Intervals.icu → COROS) is checked separately against real data. Directions the API can't see can be confirmed by hand and are never reported as verified without data.
- Activity provenance (source, OAuth client, device, paired planned event).
- The same ride recorded twice (icTrainer + watch) is detected and counted once in training load.
- Details: [docs/SYNC.md](docs/SYNC.md).

**Phase 7 (adaptive coaching):**
- A deterministic long-term plan: 4-week base → build blocks for 6-9 h/week, with weekly TSS and time targets and a weekly template. Open-ended, or ending in peak and taper weeks for an event date.
- Each completed ride is evaluated against its planned workout: step-by-step power from the stream, HR, aerobic decoupling, RPE versus expected, and wellness. It gets an outcome class and an optional LLM review (keep, reduce or recover).
- The next workouts follow the plan and are only ever *lowered* by struggle signals. Missed days are marked, never crammed in later; low compliance holds progression.
- Each ride revision is processed exactly once (event inbox with dedup keys and `SKIP LOCKED`).
- A scheduler service polls every 30 minutes and plans and publishes tomorrow at 20:00.
- Details: [docs/ADAPTIVE_COACHING.md](docs/ADAPTIVE_COACHING.md).

**Phase 8 (dashboard):**
- A read-only web dashboard at `/dashboard`: athlete profile, current FTP and zones, fitness, fatigue and form trends, readiness, the upcoming workout with its power profile, a 5-week training calendar, completed workouts with their evaluation, weekly volume against plan targets, power-zone distribution, AI coaching assessments and long-term plan progress.
- Plain HTML, JS and SVG served by FastAPI. No build step, no CDN, a strict CSP. Light and dark mode, phone width, and a table view for every chart.
- A JSON API per section under `/api/dashboard/`.
- Details: [docs/DASHBOARD.md](docs/DASHBOARD.md).

**Phase 9, steps 1–2 (operations):**
- Email alerts from the scheduler: job crashes, Intervals.icu or sync failures, blocked or failed publishing, fallback plans, with thresholds, a repeat interval and "resolved" messages. Plus a daily summary of tomorrow's workout.
- A host watchdog (systemd timer) that catches what the scheduler can't report itself: containers down or unhealthy (the scheduler has a heartbeat healthcheck), API not ready, no daily plan, and host restarts.
- Restart after a VM reboot through Podman's `podman-restart.service`, verified by simulation.
- Details: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Requirements

- Python 3.12+
- Docker Compose v2 **or** Podman plus podman-compose. On the lab VM, CycleDays runs under Podman and `docker` is a separate Docker CE engine: use `podman-compose`.

## Installation

```bash
cd /root/projects/CycleDays

# 1. Configuration (never commit .env)
cp .env.example .env
chmod 600 .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
#    Review the ATHLETE_* seed values in .env.
#    Set INTERVALS_API_KEY and INTERVALS_ATHLETE_ID (e.g. i12345) from
#    intervals.icu -> Settings -> Developer Settings. Don't swap the two.

# 2. Python environment
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # runtime only: requirements.txt
```

`.env` is read by pydantic-settings and Docker Compose. It is **not** a shell
script, because some values contain spaces, so don't `source` it.

## Running

> **On the lab VM (`labserver1`), use `podman-compose`, not `docker compose`.**
> CycleDays runs under Podman there, and Docker CE is also installed for other
> projects. `docker compose` would talk to the Docker daemon, which has no
> CycleDays image ("localhost/cycling-coach:latest: not found"). The commands
> below work unchanged with `podman-compose` in place of `docker compose`.

### Option A: everything in containers

```bash
docker compose up -d --build          # db + app; app migrates the DB on start
docker compose ps                     # both services should become "healthy"
docker compose exec app python -m scripts.seed_athlete_profile
curl -s http://127.0.0.1:8010/health/ready
docker compose logs -f app
docker compose down                   # keeps data (volume pgdata)
```

After changing code, rebuild **and recreate**. podman-compose doesn't replace
a running container just because its image changed:

```bash
docker compose build app && docker compose up -d --force-recreate --no-deps app
```

### Option B: database in a container, app from the venv (development)

```bash
docker compose up -d db
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_athlete_profile
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010
```

Don't run options A and B together: both use port 8010.

## Intervals.icu synchronization

```bash
python -m scripts.sync_intervals                  # incremental: all resources
python -m scripts.sync_intervals --full           # re-fetch INTERVALS_HISTORY_DAYS (default 90)
python -m scripts.sync_intervals --resource wellness --resource calendar
python -m scripts.sync_intervals --status         # cursors, last success, last error (no API calls)
# inside the container: docker compose exec app python -m scripts.sync_intervals
```

The JSON report goes to stdout and logs go to stderr. Run `--full` once after
upgrading to Phase 3: it fetches 180 days and records the history coverage the
training engine relies on. Exit code: 0 OK,
1 a resource failed, 2 Intervals.icu not configured. Re-running is always safe:
records are upserted on their Intervals.icu ids. The coach's athlete profile
is never overwritten from Intervals.icu. Differences such as FTP or weight are
listed in the report's `notes`.

### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness. 200 if the process is up |
| GET | `/health/ready` | 200 only if the DB is reachable **and** the schema is at the Alembic head, otherwise 503 |
| GET | `/docs` | OpenAPI UI |

## Training metrics and readiness

```bash
python -m scripts.training_report                  # recompute metrics, readiness for today
python -m scripts.training_report --sync           # Intervals.icu sync first
python -m scripts.training_report --date 2026-11-05 --no-store
```

The output is JSON with the latest CTL/ATL/TSB (and whether each is
authoritative), the last 8 ISO weeks, and the readiness assessment: status,
`max_intensity`, confidence, every factor against your baseline, and every
cap with its reason. Re-running is idempotent.

## Daily plan and publishing

```bash
python -m scripts.plan_day                     # plan tomorrow (athlete's timezone) and publish it
python -m scripts.plan_day --sync              # Intervals.icu sync first
python -m scripts.plan_day --date 2026-11-06   # a specific day
python -m scripts.plan_day --force             # re-plan (updates the same calendar event)
python -m scripts.plan_day --no-publish        # plan only

python -m scripts.publish_workout --dry-run    # show the Intervals.icu event, write nothing
python -m scripts.publish_workout              # publish the planned day (idempotent)
python -m scripts.publish_workout --verify     # retrieve the event and verify it
```

The output is JSON with the outcome (`planned` / `rest` / `fallback` /
`aborted`), the deterministic and effective intensity caps, the objective,
the workout steps (% FTP) and every LLM call (model, acceptance, violations,
tokens and latency), the validation result and the publish result.
Re-running a planned day returns `already_planned` without calling the LLM,
and re-publishing leaves an identical event untouched. Exit codes: 0 ok,
1 aborted, 3 fallback, 4 publishing blocked or failed. Requires
`OPENAI_API_KEY`; without it the run uses the conservative fallback.

## Dashboard

Open **http://127.0.0.1:8010/dashboard** while the `app` container (or
uvicorn) is running. The page is read-only and reloads its data when you
reload it. Sign in with a user created by
`python -m scripts.manage_users add <username>` (in the app container:
`podman exec -it cycling-coach_app_1 python -m scripts.manage_users add <username>`).
Set `AUTH_SECRET_KEY` in `.env` so sign-ins survive restarts. The JSON
endpoints are listed at http://127.0.0.1:8010/docs and in
[docs/DASHBOARD.md](docs/DASHBOARD.md).

## Adaptive coaching and the scheduler

```bash
python -m scripts.create_plan --weekly-hours 6-9 --dry-run   # preview the long-term plan
python -m scripts.create_plan --weekly-hours 6-9             # create it (once)
python -m scripts.create_plan --weekly-hours 6-9 --event-date 2027-06-12 --replace
                                                             # replace it with an event-date plan
python -m scripts.process_activities           # sync, evaluate new rides, mark missed days
python -m scripts.process_activities --no-sync # evaluate already synced rides only

docker compose up -d scheduler                 # automatic coaching (poll every 30 min, plan at 20:00)
docker compose stop scheduler                  # pause it
```

Settings: `SCHEDULER_POLL_MINUTES`, `SCHEDULER_PLAN_TIME`,
`ACTIVITY_LOOKBACK_DAYS` and `PLAN_HORIZON_DAYS`. See
[docs/ADAPTIVE_COACHING.md](docs/ADAPTIVE_COACHING.md).

## Notifications and the watchdog

```bash
podman-compose exec scheduler python -m scripts.notify_test   # test email (scheduler)
python3 deploy/watchdog/cycledays_watchdog.py --test-email     # test email (host watchdog)
python3 deploy/watchdog/cycledays_watchdog.py --dry-run        # run the checks, send nothing
```

Settings: `NOTIFY_EMAIL_ENABLED`, `NOTIFY_EMAIL_TO`, `SMTP_*`,
`NOTIFY_DAILY_SUMMARY`, `NOTIFY_REPEAT_HOURS`,
`NOTIFY_POLL_FAILURE_THRESHOLD`. Installing the watchdog timer and the
reboot behaviour are covered in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Synchronization checks (icTrainer, COROS)

```bash
python -m scripts.check_sync --sync            # sync, then check every direction (last 28 days)
python -m scripts.check_sync --confirm intervals_to_ictrainer.planned \
    --note "workout listed in icTrainer"       # confirm what the API can't see
```

Each check reports `verified`, `confirmed`, `configured`, `not_observed`,
`not_configured` or `attention` with its evidence. Results are stored in
`coach.sync_checks` when they change. Exit code 4 means a check needs
attention. See [docs/SYNC.md](docs/SYNC.md), including the one-time athlete
checklist.

## Database migrations

```bash
.venv/bin/alembic upgrade head                       # apply
.venv/bin/alembic current                            # show applied revision
.venv/bin/alembic check                              # models vs. DB drift (should report none)
.venv/bin/alembic revision --autogenerate -m "..."   # new migration (review before committing)
```

Application tables live in schema `coach`. Schema `langgraph` is reserved for
LangGraph checkpoints (tables created by the checkpointer's `setup()`) and
is never managed by Alembic.

## Tests and quality checks

```bash
docker compose up -d db                    # integration tests need PostgreSQL
REQUIRE_DB=1 .venv/bin/pytest -W error     # all tests; fail (not skip) if DB missing
.venv/bin/pytest tests/unit                # unit tests only, no DB needed
.venv/bin/pytest -m integration            # integration tests only
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                             # strict, app + scripts + tests
```

Integration tests create and drop throwaway databases
(`<POSTGRES_DB>_test`, `<POSTGRES_DB>_test_migrations`) and never touch your
real data.

## Configuration

All settings come from environment variables or `.env`. See
[.env.example](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `development` / `test` / `production` |
| `APP_TIMEZONE` | `Asia/Kolkata` | IANA name, validated |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `API_PORT` | `8010` | |
| `API_BIND_ADDRESS` | `127.0.0.1` | Host interface for compose. Keep local until API auth exists |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `localhost` / `5434` | Compose overrides these to `db:5432` inside the app container |
| `POSTGRES_DB` / `POSTGRES_USER` | `cycling_coach` / `coach` | |
| `POSTGRES_PASSWORD` | **required** | No default. The app refuses to start without it |
| `DATABASE_URL` | — | Optional full SQLAlchemy URL that overrides the above |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | |
| `DB_ECHO` | `false` | Log SQL |
| `HEALTH_DB_TIMEOUT_SECONDS` | `3.0` | Readiness check timeout |
| `RUN_MIGRATIONS_ON_START` | `true` | Container entrypoint runs `alembic upgrade head` |
| `INTERVALS_API_KEY` | — | Secret. Basic auth with username `API_KEY` |
| `INTERVALS_ATHLETE_ID` | — | e.g. `i780524` |
| `INTERVALS_BASE_URL` | `https://intervals.icu` | |
| `INTERVALS_TIMEOUT_SECONDS` | `30` | per request |
| `INTERVALS_MAX_RETRIES` | `4` | for 429, 5xx and network errors |
| `INTERVALS_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `1` / `60` | exponential backoff with jitter. `Retry-After` is honoured up to the max |
| `INTERVALS_HISTORY_DAYS` | `180` | first or `--full` sync. Minimum 30. CTL needs 96 days |
| `INTERVALS_SYNC_OVERLAP_DAYS` | `7` | re-fetched days on incremental syncs |
| `INTERVALS_CALENDAR_FUTURE_DAYS` | `28` | calendar look-ahead |
| `INTERVALS_PUBLISH_ENABLED` | `true` | `plan_day` publishes validated workouts automatically |
| `OPENAI_API_KEY` | — | Secret. Without it, plans use the deterministic fallback |
| `OPENAI_MODEL` | `gpt-6.1-sol` | Any model with structured outputs. A dated snapshot pins behaviour |
| `OPENAI_BASE_URL` | — | optional |
| `OPENAI_TIMEOUT_SECONDS` | `300` | per request |
| `OPENAI_MAX_ATTEMPTS` | `3` | for timeouts, network errors, 408/409/429/5xx |
| `OPENAI_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `2` / `60` | |
| `LLM_MAX_CORRECTIONS` | `2` | correction rounds before falling back |
| `SCHEDULER_POLL_MINUTES` | `30` | activity poll interval |
| `SCHEDULER_PLAN_TIME` | `20:00` | when tomorrow is planned (athlete timezone) |
| `ACTIVITY_LOOKBACK_DAYS` | `14` | how far back new rides are detected |
| `PLAN_HORIZON_DAYS` | `28` | the plan is extended to cover this many days ahead |
| `ATHLETE_*` | FTP 175 W, 86 kg, 172 cm, 46 y, … | Seed values only. The DB profile is authoritative after seeding |

## Project layout

```
app/
  main.py              FastAPI factory, middleware
  config.py            settings
  logging_config.py    structured logging
  api/routes.py        health endpoints
  api/dashboard.py     dashboard page and JSON API
  web/                 index.html, static/dashboard.js, static/dashboard.css
  database/            engine/sessions, models, repositories
  integrations/        intervals_client.py (REST client), intervals_mappers.py, intervals_workouts.py
  scheduler.py         scheduler process (python -m app.scheduler), job alerts, heartbeat
  notifications.py     email notifier (SMTP)
  alerts.py            alert rules, tracker, email texts
  services/            intervals_sync.py, training_metrics.py, workout_publisher.py,
                       sync_verification.py, activity_processor.py, daily_planner.py,
                       dashboard.py
  training/            zones, metrics, performance, readiness, periodization, sessions,
                       workout_generator, workout_validator, activity_dedup,
                       plan_builder, progression, adaptation, evaluation
  agents/              OpenAI wrapper, structured-output schemas, agents
  graph/               LangGraph state, prompts, guards, fallback, routing, nodes, workflow
migrations/            Alembic environment + versions
scripts/               entrypoint, wait_for_db, seed_athlete_profile, sync_intervals, training_report, plan_day, publish_workout, check_sync,
                       create_plan, process_activities, manage_users, notify_test
deploy/                nginx/, systemd/ (watchdog timer), watchdog/ (host watchdog script)
tests/unit, tests/integration
docs/                  ARCHITECTURE.md, IMPLEMENTATION_STATUS.md, INTERVALS_ICU.md, TRAINING_ENGINE.md, COACHING_GRAPH.md, WORKOUTS.md, SYNC.md,
                       ADAPTIVE_COACHING.md, DASHBOARD.md, OPERATIONS.md
```

## Security notes

- `.env` is git-ignored and excluded from the image (`.dockerignore`).
- The container runs as a non-root user.
- PostgreSQL and the API are bound to `127.0.0.1` by default.
- Log records redact secret-like keys.
- Stored Intervals.icu payloads are redacted too (the athlete object contains
  your API key). Email isn't stored.
- Publishing writes only events carrying our `external_id` and deletes only
  those; every write is audited.
- LLM prompts contain training and recovery data only (no credentials, no
  email). LangSmith tracing is off.
