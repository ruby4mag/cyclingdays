# Operations: notifications, restarts, watchdog

> **This VM runs CycleDays under Podman.** Docker CE is also installed and
> running here for other projects, so `docker` / `docker compose` talk to a
> different engine that has no CycleDays image or containers. Always use
> `podman-compose` / `podman` for CycleDays.

_Phase 9, steps 1–2 (2026-11-06). The rest of Phase 9 is on hold._

CycleDays runs unattended: the scheduler plans and publishes tomorrow's
workout at 20:00. This page covers how you find out when that stops working,
and how the coach comes back after the VM restarts.

## 1. Email notifications

Two senders, the same `.env` settings:

| Sender | Runs in | Reports | Subject prefix |
|---|---|---|---|
| Scheduler (`app/scheduler.py`, `app/alerts.py`) | `scheduler` container | job failures, sync errors, blocked publishing, fallback plans, the daily summary | `[CycleDays]` |
| Watchdog (`deploy/watchdog/cycledays_watchdog.py`) | host, systemd timer | containers down or unhealthy, API not ready, no daily plan, host restarted | `[CycleDays watchdog]` |

The scheduler can't report its own death, so the watchdog checks it from
outside.

### Setup

Add to `.env` (Gmail shown; any SMTP server works):

```bash
NOTIFY_EMAIL_ENABLED=true
NOTIFY_EMAIL_TO=you@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<16-character app password>
```

For Gmail, `SMTP_PASSWORD` must be an **app password** (Google account →
Security → 2-Step Verification → App passwords), not the account password.
Then recreate the scheduler so it reads the new `.env`, and send test emails:

```bash
cd /opt/cycledays
podman-compose up -d --force-recreate --no-deps scheduler
podman-compose exec scheduler python -m scripts.notify_test      # scheduler path
python3 deploy/watchdog/cycledays_watchdog.py --test-email        # watchdog path
```

`notify_test` exits 0 when sent, 1 when the server refused, and 2 when
settings are missing (it lists them). Until email is configured, the
scheduler logs `notifications_disabled` (ERROR) at start and each message it
would have sent as `notification_not_sent`.

### What the scheduler sends

| Situation | Severity | When |
|---|---|---|
| Daily plan job crashed | error | immediately |
| Planning aborted (no workout) | error | immediately |
| Workout not published (`blocked`, `failed`, `locked`, `nothing_planned`) | error | immediately |
| Workout already completed/missed (`skipped`) | warning | immediately |
| AI coach failed, fallback workout used | warning | immediately |
| Poll crashed, Intervals.icu unreachable, a sync resource failed | error | after `NOTIFY_POLL_FAILURE_THRESHOLD` (2) failed polls in a row, so about 1 h |
| Ride evaluations failed (retried automatically) | warning | same threshold |
| **Daily summary**: tomorrow's workout, load, readiness, publish status | — | after each 20:00 run (`NOTIFY_DAILY_SUMMARY=true`) |

* A problem is emailed once, then again every `NOTIFY_REPEAT_HOURS` (12)
  while it lasts, and a **Resolved** email (or a "Resolved" section in the
  daily summary) follows when it clears.
* One job run sends at most one email. A problem on the daily plan is the
  subject of that evening's email, with the plan below it.
* **No summary email at 20:00 means something is wrong.** A re-run that
  changes nothing (scheduler restarted after 20:00, day already planned and
  unchanged) stays quiet, so restarts don't duplicate the summary.
* State is kept in memory. After a scheduler restart, a lasting problem is
  emailed once more.
* Sending never breaks a job: a mail-server error is logged as
  `notification_failed`.

### What the watchdog sends

It runs 5 minutes after boot and then every 10 minutes
(`cycledays-watchdog.timer`) and checks:

1. **Containers** `db`, `app`, `scheduler` (compose project `cycling-coach`)
   exist, are running and aren't `unhealthy`. The scheduler's healthcheck is
   its heartbeat file (`/tmp/cycledays-scheduler.heartbeat`, touched every
   minute, unhealthy after 5 minutes), so a stuck scheduler is caught.
2. **API**: `GET http://127.0.0.1:8010/health/ready` returns 200.
3. **Daily plan**: a `daily_plan` decision exists for the day that should be
   planned by now. That's tomorrow once `SCHEDULER_PLAN_TIME` + 90 min has
   passed, otherwise today.
4. **Host restart**: on a new boot, one email reports the host came back and
   whether the checks pass.

A problem must be seen in **2 runs in a row** (10–20 min) before it's
emailed, so a redeploy doesn't trigger alerts. After that, the same
repeat and resolve rules apply. State: `/var/lib/cycledays-watchdog/state.json`.

```bash
python3 deploy/watchdog/cycledays_watchdog.py --dry-run   # checks + the email it would send
systemctl list-timers cycledays-watchdog.timer
journalctl -u cycledays-watchdog.service -n 20            # one JSON line per run
```

Pausing the scheduler on purpose (`podman-compose stop scheduler`) makes the
watchdog report it as not running every 12 h. Stop the timer too while it's
paused: `systemctl stop cycledays-watchdog.timer`.

### Installing the watchdog timer

```bash
cd /opt/cycledays
\cp -f deploy/systemd/cycledays-watchdog.service deploy/systemd/cycledays-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cycledays-watchdog.timer
systemctl start cycledays-watchdog.service   # first run now
journalctl -u cycledays-watchdog.service -n 5 -o cat
```

(`\cp` because `cp` is aliased to `cp -i` for root on this VM.)

## 2. Restart after a VM reboot

**Containers come back by themselves.** Podman has no daemon, but Podman
5.x ships `podman-restart.service` (enabled on this VM). At boot it runs
`podman start --all --filter should-start-on-boot=true`, which includes
containers with `restart: unless-stopped`, as all three CycleDays services
have. Start order follows the compose dependencies (podman-compose creates
the scheduler with `--requires` on `db` and `app`), and the app's entrypoint
waits for PostgreSQL.

Verified on 2026-11-06 by simulating a reboot with
`systemctl restart podman-restart.service`, which runs the same shutdown stop
(`podman stop --service --all`) and boot start. All three containers were
back within 2 s, `/health/ready` returned 200 after 36 s, and the scheduler
started and completed a poll. A real reboot hasn't been tested.

```bash
systemctl is-enabled podman-restart.service                       # enabled
podman ps -a --filter should-start-on-boot=true --format '{{.Names}}'
# cycling-coach_db_1, cycling-coach_app_1, cycling-coach_scheduler_1
```

Points to know:

* **A container you stopped stays stopped.** `podman-compose stop scheduler`
  marks it user-stopped and it won't start on boot, which is intended for a
  pause. `podman-compose start scheduler` undoes it.
* **Missed 20:00 run.** If the host was down at 20:00 and comes back
  **before midnight**, the scheduler's catch-up plans and publishes tomorrow
  at start. If it comes back **after midnight**, nothing plans that day
  automatically. The catch-up only covers "tomorrow" on the same evening.
  The watchdog then reports `No daily plan for <today>`; plan it by hand:
  `podman-compose exec scheduler python -m scripts.plan_day --date <today>`
  (see `--help`).
* **Redeploys** (`podman-compose up -d --force-recreate …`) create new
  containers with the same restart policy, so they stay boot-safe.
* nginx (`systemctl is-enabled nginx`: enabled) and the watchdog timer start
  with the host.
* The watchdog's boot email confirms each real reboot.

Quadlet or `podman generate systemd` units were considered and not used.
They would take the containers away from podman-compose, so every
`podman-compose` command in the README would conflict with systemd's.
