# Intervals.icu integration

_Last updated: Phase 6 (2026-11-05)._

## 1. Sources and verification

* **Endpoints and schemas:** the official OpenAPI 3.0.1 document served at
  `https://intervals.icu/api/v1/docs` (title "Intervals.icu API", v1.0.0),
  downloaded 2026-11-05. No endpoint or field is used that isn't in that
  document.
* **Real responses:** checked on 2026-11-05 against athlete `i780524` (the
  project owner's account). Verified: authentication, the athlete object,
  wellness records, a calendar event, error responses (401, 403), and
  repeated syncs. **No activity responses could be verified**, because the
  account had no activities at all (see §6).

## 2. Authentication

OpenAPI security scheme `APIKey`: HTTP Basic, **username `API_KEY`**, password
= personal API key (intervals.icu → Settings → Developer Settings). Bearer
tokens (`AccessToken`, OAuth) aren't used.

| Variable | Notes |
|---|---|
| `INTERVALS_API_KEY` | secret (`SecretStr`), never logged |
| `INTERVALS_ATHLETE_ID` | e.g. `i780524`. Required: the OpenAPI document defines no "current athlete" shortcut |

Observed: a bad key gives `401 {"error":"Auth failed"}`. Another athlete's id
gives `403 {"error":"Access denied"}`.

## 3. Endpoints used

| Purpose | Request | Notes |
|---|---|---|
| Athlete + sport settings | `GET /api/v1/athlete/{id}` | Response contains **secrets** (`icu_api_key`, `icu_friend_invite_token`) and email |
| Activities | `GET /api/v1/athlete/{id}/activities?oldest=` | Filters on **local** start date, newest first. `newest` defaults to "now" and is deliberately omitted (a bare date would mean midnight and drop today's rides) |
| One activity | `GET /api/v1/activity/{id}` | client method only, not used by the sync yet |
| Wellness | `GET /api/v1/athlete/{id}/wellness?oldest=&newest=` | `{ext}` path suffix empty = JSON. `newest` inclusive |
| Calendar | `GET /api/v1/athlete/{id}/events?oldest=&newest=` | `{format}` empty = JSON. Without `newest` the API returns only oldest + 6 days, so both are always sent |
| Planned workouts | same, `&category=WORKOUT` | |
| **Publish** (Phase 5) | `POST /api/v1/athlete/{id}/events/bulk?upsert=true` | Body: array of `EventEx`. Upserts on `external_id` (see §3.1) |
| Read one event (Phase 5) | `GET /api/v1/athlete/{id}/events/{eventId}` | Includes the parsed `workout_doc`, used for verification |
| Delete one event (Phase 5) | `DELETE /api/v1/athlete/{id}/events/{eventId}` | Only our own events (matched by `external_id`) are ever deleted |
| Activity streams (Phase 7) | `GET /api/v1/activity/{id}/streams?types=time,watts,heartrate` | Returns `ActivityStream` objects (`type`, `data`). **Not yet verified on a real ride**; parsed tolerantly, and the evaluation falls back to totals on any error |

**Webhooks** aren't used. Intervals.icu delivers them only to OAuth
applications, which you request from Intervals.icu by email. The payload
carries a `secret` and an `events` list (e.g. `ACTIVITY_UPLOADED`), according
to the developer's API cookbook on the forum. A personal API key can't
register one, so new rides are found by polling (Phase 7 scheduler).

### 3.1 Publishing: verified behaviour (2026-11-05)

Checked with a probe event on 2027-12-31, deleted straight afterwards:

* A plain `POST /events` with an `external_id` that already exists **creates
  a duplicate**.
* `POST /events/bulk?upsert=true` **updates** the existing event with that
  `external_id`. The OpenAPI document says "created by the same OAuth
  application", but it also works with API-key auth (`oauth_client_id` is
  null for both). The publisher therefore always uses the bulk upsert, and
  retrying it is safe.
* The `description` text is parsed into `workout_doc.steps`. Each step has
  `duration` in seconds and `power` of either `{value, units:"%ftp"}` or
  `{start, end, units:"%ftp"}`. Steps can also have `cadence` (`{value,
  units:"rpm"}`), `text` (the cue) and `ramp: true`. Text before the steps
  becomes `workout_doc.description`.
* `POST /download-workout{ext}` only converts `workout_doc` JSON; it
  returns HTTP 500 for description text, so it can't be used as a dry-run
  parser.
* Intervals.icu computes `moving_time`, `icu_training_load` and
  `icu_intensity` itself. For the first real workout it reported TSS 9 and
  IF 42.4 %, against our estimate of 9.2 and 0.43.

Text syntax source: forum guide "Workout Builder Syntax Quick Guide"
(forum.intervals.icu/t/123701). Details in [WORKOUTS.md](WORKOUTS.md).

There is **no "modified since" parameter** on any list endpoint, so
incremental sync works on date windows (§5).

## 4. Field mapping

Code: `app/integrations/intervals_mappers.py`. The full source object is always
kept in `raw_payload` (secrets redacted), so more fields can be derived later
without re-fetching.

### 4.1 Activity → `coach.activities`

| Column | Source field | Note |
|---|---|---|
| `source_activity_id` | `id` | unique with `source='intervals_icu'`: **no duplicate imports** |
| `start_time` | `start_date` | UTC |
| `start_date_local` | `start_date_local` | athlete-local date |
| `activity_type` | `type` | e.g. `Ride`, `VirtualRide` |
| `is_indoor` | `trainer` | |
| `moving_time_s` / `elapsed_time_s` | `moving_time` / `elapsed_time` | |
| `distance_m` | `distance`, else `icu_distance` | |
| `elevation_gain_m` | `total_elevation_gain` | |
| `avg_power_watts` | `icu_average_watts` | 0 → NULL (no power) |
| `normalized_power_watts` | `icu_weighted_avg_watts` | Intervals.icu's NP equivalent |
| `avg_hr_bpm` / `max_hr_bpm` | `average_heartrate` / `max_heartrate` | 0 → NULL |
| `ftp_at_activity_watts` | `icu_ftp` | |
| `source_training_load` | `icu_training_load` | the source's value. Our own goes to `training_metrics` (Phase 3) |
| `source_intensity_factor` | `icu_intensity` ÷ 100 | **assumption: percent**. Unverified (no real activities yet) |
| `rpe` | `icu_rpe`, else `perceived_exertion` | |
| `feel` | `feel` | source scale |
| `origin` (0004) | `source` | enum: `STRAVA`, `UPLOAD`, `MANUAL`, `GARMIN_CONNECT`, `OAUTH_CLIENT`, `DROPBOX`, `POLAR`, `SUUNTO`, `COROS`, `WAHOO`, `ZWIFT`, `ZEPP`, `CONCEPT2`, `HUAWEI` |
| `origin_client` (0004) | `oauth_client_name` | e.g. the app that uploaded it (icTrainer, unverified) |
| `device_name` (0004) | `device_name` | |
| `origin_external_id` (0004) | `external_id` | the uploader's id |
| `paired_event_id` (0004) | `paired_event_id` | planned event Intervals.icu paired the ride with |
| `duplicate_of_activity_id` (0004) | (derived) | same ride recorded twice; see [SYNC.md §3](SYNC.md) |

**Hidden activities.** The API returns a `Hidden` stub (`id`,
`icu_athlete_id`, `start_date_local`, `source`, `_note`) for activities it
can't share, e.g. Strava imports. These are counted as `skipped` and not stored.

### 4.2 Wellness → `coach.daily_recovery`

| Column | Source field | Real data (30 days) |
|---|---|---|
| `recovery_date` | `id` (ISO date) | 28 records |
| `resting_hr_bpm` | `restingHR` | 13 |
| `hrv_rmssd_ms` | `hrv` (rMSSD. SDNN is `hrvSDNN`) | 13 |
| `sleep_duration_s` | `sleepSecs` | 13 |
| `sleep_score` | `sleepScore` | 0 |
| `subjective_fatigue` / `_soreness` / `_stress` | `fatigue` / `soreness` / `stress` (1 good … 4 bad) | 0 |
| `subjective_injury` (0003) | `injury` (1 none … 4 injured, unconfirmed) | 0 |
| `notes` | `comments` | 0 |

Also present in real data but kept only in `raw_payload`: `steps` (28),
`avgSleepingHR` (13).

A record is stored only if it holds a recorded value. Computed fields
(`ctl`, `atl`, `rampRate`, …) and boolean flags (`tempWeight`, `tempRestingHR`,
which real responses carry as `false` on every day) don't count as data.

`illness_reported` / `pain_reported` are **not** derived. Intervals.icu has
no illness field. `injury` is stored and interpreted by the readiness rules
(see TRAINING_ENGINE.md §5.2).

### 4.3 Event → `coach.calendar_events` (new in migration 0002)

`id` (integer in the API, stored as text) → `source_event_id`. Other columns:
`category` (e.g. `WORKOUT`, `NOTE`, `SEASON_START`), `type`, `start_date_local`
(date + local time), `end_date_local`, `name`, `description`, `external_id`,
`indoor`, `moving_time` → `planned_duration_s`, `icu_training_load` →
`planned_training_load`, and `updated` → `source_updated_at`. All verified
against a real `SEASON_START` event.

### 4.4 Athlete → `coach.athlete_profile`

* `intervals_athlete_id` is linked on the first sync. If it's already linked
  and the API key returns a different athlete, the sync **stops** rather than
  mix data.
* `intervals_snapshot` stores the athlete object **with secrets redacted and
  `email` removed**.
* FTP (from the `sportSettings` entry whose `types` include `Ride`), weight
  (`icu_weight`) and timezone are **compared and reported, never applied**.
  The coach's profile stays authoritative. FTP changes must be deliberate
  because they change every power target. Observed: Ride FTP 175 W (matches),
  weight 85.0 kg vs profile 86.0 kg (reported).

## 5. Synchronization

Code: `app/services/intervals_sync.py`. CLI: `python -m scripts.sync_intervals`.

* **Order:** athlete → activities → wellness → calendar. If the athlete step
  fails, the run stops (the data could land on the wrong profile).
* **Windows:**
  * First run, or `--full`: `today − INTERVALS_HISTORY_DAYS` (default 180
    since Phase 3; min 30).
  * Later runs: `cursor.newest − INTERVALS_SYNC_OVERLAP_DAYS` (default 7). The
    overlap picks up late wearable uploads and later edits (RPE, feel, wellness).
  * Calendar: same start, end `today + INTERVALS_CALENDAR_FUTURE_DAYS`
    (default 28).
  * "today" is in the athlete profile's timezone.
* **Idempotency:** `INSERT … ON CONFLICT` on natural keys. A row is rewritten
  only when its payload changed, so the results are inserted / updated /
  unchanged counts. Verified with real data: a repeat run and a `--full` run
  both reported only `unchanged`, and the database had no duplicate keys.
* **Deletions:** calendar events that disappear from a re-fetched window are
  soft-deleted (`deleted_at`) and revived if they reappear. Deleted
  activities aren't detected yet (outstanding issue).
* **Concurrency:** each resource runs in one transaction holding
  `pg_try_advisory_xact_lock(hashtext('intervals_sync'), athlete_id)`. A
  concurrent run reports `locked` and skips.
* **State:** `coach.synchronization_state` per resource holds the cursor
  (`oldest`, `newest`, `last_full_sync`, and `coverage_start`: the earliest
  day ever covered, used by the training engine), `last_attempt_at`, `last_success_at`,
  `last_error` and `consecutive_failures`. A success resets the failure
  counter.

## 6. Retries, rate limits and errors

* Retried: transport errors and timeouts, plus HTTP 429, 500, 502, 503 and 504.
  Up to `INTERVALS_MAX_RETRIES` (4) times, with exponential backoff and full
  jitter (`INTERVALS_BACKOFF_BASE_SECONDS` 1, cap
  `INTERVALS_BACKOFF_MAX_SECONDS` 60).
* `Retry-After` (in seconds) is honoured on 429 and 503, capped at the
  backoff maximum.
* **The OpenAPI document publishes no rate limits**, so no fixed request
  budget is assumed. A sync makes 4 requests.
* Other 4xx responses aren't retried. They map to typed errors:
  `IntervalsAuthError` (401/403), `IntervalsNotFoundError`,
  `IntervalsRateLimitError`, `IntervalsServerError`,
  `IntervalsTransportError` and `IntervalsResponseError` (unexpected status
  or body shape).
* Logging: `intervals_request_retry` (WARNING) and `intervals_request_failed`
  (ERROR) include method, path, status, attempts, duration and up to 500
  characters of the response body. The `Authorization` header and the key
  are never logged. The HTTP library's own per-request INFO logs are
  silenced.

## 7. Known gaps

* **No activity in the account yet.** The activity mapping is checked against
  the OpenAPI schema and synthetic tests only. In particular,
  `icu_intensity`-as-percent and the `Hidden` stub shape still need to be
  confirmed on a real ride (re-run `--full` once one exists).
* COROS is linked with activity sync, wellness download and workout
  upload all enabled. Wellness is verified; activity sync waits for the first
  recorded ride. Per-direction status is in [SYNC.md](SYNC.md)
  (`scripts.check_sync`).
* Activities deleted in Intervals.icu aren't removed or flagged locally.
* No scheduler yet: sync and planning run on demand. APScheduler comes in Phase 7.
* Upsert-by-`external_id` with API-key auth is observed behaviour, not
  documented behaviour. If it changed, a duplicate could appear, but the
  next publish run lists the day's events and deletes extra copies of its
  own `external_id` (tested).
