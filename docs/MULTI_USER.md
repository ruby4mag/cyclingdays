# Multi-user coaching (Phase 10)

_Built and deployed 2026-11-06 (v0.10.0). Phase 9 beyond steps 1–2 is on hold._

CycleDays coaches several riders. Each person signs in, connects their own
Intervals.icu account, checks their profile and sets a goal. From then on
the same coaching runs for them as for the original athlete: poll every 30
minutes, ride evaluations, adaptation, and tomorrow's workout planned,
validated and published to **their** Intervals.icu calendar at 20:00 in
**their** time zone.

## How a new rider gets started

Accounts are **invite-only**: there's no public sign-up.

1. **The administrator creates the login** (in the app container):

   ```bash
   podman exec -it cycling-coach_app_1 python -m scripts.manage_users add <username> --email rider@example.com
   ```

   Then send the rider the address, username and password.

2. **The rider signs in.** With no athlete yet, the dashboard sends them to
   **Account** (`/account`), a three-step setup:
   1. **Connect Intervals.icu**: athlete ID and personal API key (Intervals.icu → Settings →
      Developer Settings). The key is checked with `GET /athlete/{id}` before anything is
      stored. This creates their athlete, prefilled from Intervals.icu (name, FTP from the
      Ride sport settings, weight, time zone).
   2. **Profile**: FTP (required), weight, height, age, experience, time zone, equipment.
   3. **Goal and plan**: objective, optional event date, weekly hours (min–max), optional
      start, and their week (which days are rest, recovery, endurance, key or long).
      **Preview plan** shows the weeks; saving builds the long-term plan
      (`app/training/plan_builder.py`, same as `scripts.create_plan`).
3. **The scheduler picks them up within 2 minutes** (`onboarding` job): first
   the 180-day history sync, then, once the goal exists, tomorrow's plan and
   publish. After that they follow the normal 30-minute poll and 20:00 plan.

Riders can change any of this later on the same page. A new goal rebuilds
the plan from today (the old one is archived). Email: each rider can set an
address and turn the daily workout email on or off.

## Design

| Concern | How |
|---|---|
| Who sees what | `app_users.athlete_id` (unique) links one login to one athlete. Every dashboard and account endpoint takes the athlete from the **session**, never from the request. A user without an athlete gets `409 setup_required`. |
| Intervals.icu credentials | `coach.intervals_connections` (one per athlete; the Intervals.icu athlete id is unique across all athletes). API key encrypted with Fernet (`CREDENTIALS_ENCRYPTION_KEY`), decrypted only to build a client (`app/services/connections.py: open_client`). Never returned by the API or logged. |
| Taking over someone's data | Refused: an Intervals.icu athlete already connected to another account can't be connected again (409), and an athlete can't switch to a different Intervals.icu athlete. |
| Revoked keys | A 401/403 from Intervals.icu marks the connection `invalid`; the scheduler skips the athlete and the Account page asks for a new key. |
| Scheduler | `CoachScheduler` (`app/scheduler.py`): every job runs per athlete under its own advisory lock (`poll:<slug>`, `daily_plan:<slug>`), at most `SCHEDULER_MAX_PARALLEL` (2) at once. One athlete's failure doesn't affect the others. `daily_plan` checks every minute and plans each athlete once per local day after `SCHEDULER_PLAN_TIME` in their own time zone. Athletes without a goal aren't planned. |
| Emails | Problems go to the operator (`NOTIFY_EMAIL_TO`), with the athlete in the subject. The daily workout summary goes to the athlete's own address (`app_users.email`, if `notify_daily_summary`). |
| Watchdog | Checks the daily plan of every coached athlete, in their time zone; a new athlete must get a first plan within 2 hours of setting a goal. |
| Writes | Same-origin JSON only (`Origin` check, `Content-Type: application/json`, unknown fields refused), so cross-site pages can't submit them. |

Not changed: the coaching itself (metrics, readiness, LLM graph, validator,
publishing gates) — it was already per athlete.

## Administration

```bash
A="podman exec -it cycling-coach_app_1 python -m"
$A scripts.manage_users list                       # users, their athlete and email
$A scripts.manage_users email <user> <address|->   # set/clear the summary address
$A scripts.manage_users disable <user>             # sign-in off (coaching continues)
$A scripts.manage_users link <user> <athlete-slug> # attach an existing athlete
$A scripts.connect_intervals --athlete-slug <slug> --status
$A scripts.plan_day --athlete-slug <slug>          # every CLI takes --athlete-slug
```

To stop coaching someone, they (or you, in SQL) disconnect Intervals.icu;
their data stays. Deleting an athlete row removes all their data (cascade)
and unlinks the login.

**Back up `CREDENTIALS_ENCRYPTION_KEY`** with the database backups. Without
it, stored keys can't be decrypted and every rider has to reconnect.

## Migration of the original single-user install (done 2026-11-06)

1. Migration `0007` (adds `app_users.athlete_id/email/notify_daily_summary`,
   `intervals_connections`). DB backup taken first: `coach-pre-0007.dump`
   (git-ignored, contains personal data).
2. `CREDENTIALS_ENCRYPTION_KEY` generated into `.env`.
3. `scripts.connect_intervals --athlete-slug primary --from-env` moved the
   `.env` key into the database (verified: decrypts and works; the
   plaintext isn't stored).
4. `scripts.manage_users link magingeorge primary` and set the summary
   email to the address that `NOTIFY_EMAIL_TO` already used.

`INTERVALS_API_KEY` / `INTERVALS_ATHLETE_ID` in `.env` are no longer read by
the app or scheduler (only by `connect_intervals --from-env`); they can be
removed. `ATHLETE_*` are only seed values for `seed_athlete_profile`.

## Limits and next steps

* Invite-only. Self sign-up (with email verification), password reset by
  email and OAuth with Intervals.icu are the steps before opening it up.
* OpenAI cost grows with riders: roughly one planning call per rider per day,
  plus one evaluation per ride.
* The safety limits are tuned for adult endurance riders; there's no medical
  screening. Riders you invite should know the workouts are automated.
* Health data (HRV, sleep, resting HR) of several people now sits in one
  database: backups, retention and data export/deletion (Phase 9 items on
  hold) matter more than before.
