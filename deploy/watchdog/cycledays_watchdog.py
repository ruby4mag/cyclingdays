#!/usr/bin/env python3
"""CycleDays host watchdog (Phase 9): is the coach running at all?

The scheduler emails its own job failures, but it can't report that it's
gone. This script runs on the host from a systemd timer
(``deploy/systemd/cycledays-watchdog.timer``, every 10 minutes and 5 minutes
after boot) and checks from the outside:

1. **Containers:** ``db``, ``app`` and ``scheduler`` of compose project
   ``cycling-coach`` exist, are running and aren't unhealthy. The scheduler's
   healthcheck is its heartbeat file, so a stuck scheduler shows up here.
2. **API:** ``GET /health/ready`` answers 200.
3. **Daily plans (Phase 10: every coached athlete):** for each athlete with
   an active Intervals.icu connection and plan, a ``daily_plan`` decision
   exists for the day that should be planned by now in *their* timezone
   (tomorrow once ``SCHEDULER_PLAN_TIME`` + 90 min has passed, otherwise
   today). A new athlete must get a first plan within 2 hours of setting a
   goal.
4. **Reboots:** after the host restarts (new ``boot_id``), one email says the
   host came back and whether the checks above pass.

A problem is emailed once it's seen in two runs in a row (so a redeploy
doesn't alert), again every ``NOTIFY_REPEAT_HOURS``, and a "resolved" email
follows. State lives in ``/var/lib/cycledays-watchdog/state.json``.

Email settings come from the project's ``.env`` (the same ``SMTP_*`` and
``NOTIFY_*`` variables as the app). Standard library only; Python 3.9+, so
it runs with the host's ``/usr/bin/python3`` and needs nothing from the app
image, which may be the thing that's broken.

    cycledays_watchdog.py              # run the checks (what the timer does)
    cycledays_watchdog.py --dry-run    # print results; no email, no state
    cycledays_watchdog.py --test-email # send a test email
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import smtplib
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

PROJECT = "cycling-coach"
SERVICES = ("db", "app", "scheduler")
DEFAULT_ENV = Path("/opt/cycledays/.env")
DEFAULT_STATE = Path("/var/lib/cycledays-watchdog/state.json")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
PLAN_GRACE = dt.timedelta(minutes=90)
THRESHOLD = 2  # runs in a row before a problem is emailed
SUBJECT_PREFIX = "[CycleDays watchdog]"
# The SQL is fixed text on stdin; nothing user-supplied is interpolated.
PSQL = 'psql -X -q -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

Problem = Tuple[str, str, str]  # key, title, detail


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def read_env(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines like compose reads them; quotes are stripped."""
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def email_problems(env: Dict[str, str]) -> List[str]:
    if not truthy(env.get("NOTIFY_EMAIL_ENABLED")):
        return ["NOTIFY_EMAIL_ENABLED is false"]
    missing = []
    if not env.get("SMTP_HOST"):
        missing.append("SMTP_HOST is not set")
    if not recipients(env):
        missing.append("NOTIFY_EMAIL_TO is not set")
    if not (env.get("NOTIFY_EMAIL_FROM") or env.get("SMTP_USERNAME")):
        missing.append("NOTIFY_EMAIL_FROM (or SMTP_USERNAME) is not set")
    return missing


def recipients(env: Dict[str, str]) -> List[str]:
    return [a.strip() for a in env.get("NOTIFY_EMAIL_TO", "").split(",") if a.strip()]


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def run(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr or p.stdout)


def list_containers() -> Tuple[Optional[List[Dict[str, Any]]], str]:
    code, out = run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--format",
            "json",
        ]
    )
    if code != 0:
        return None, out.strip()
    try:
        return json.loads(out or "[]"), ""
    except ValueError as exc:
        return None, f"unreadable podman output: {exc}"


def container_problems(
    containers: Optional[List[Dict[str, Any]]], error: str = ""
) -> Tuple[List[Problem], Dict[str, str]]:
    """Problems, and service -> container name for the running ones."""
    if containers is None:
        return [("podman", "Podman could not list the containers", error)], {}
    problems: List[Problem] = []
    running: Dict[str, str] = {}
    by_service: Dict[str, Dict[str, Any]] = {}
    for c in containers:
        service = (c.get("Labels") or {}).get("com.docker.compose.service")
        if service:
            by_service[service] = c
    for service in SERVICES:
        c = by_service.get(service)
        if c is None:
            problems.append(
                (
                    f"container.{service}",
                    f"Container '{service}' is missing",
                    "Start it: cd /opt/cycledays && podman-compose up -d",
                )
            )
            continue
        name = (c.get("Names") or [service])[0]
        state = str(c.get("State") or "")
        status = str(c.get("Status") or "")
        if state != "running":
            problems.append(
                (
                    f"container.{service}",
                    f"Container '{service}' is not running",
                    f"{name}: {state} ({status})",
                )
            )
            continue
        running[service] = name
        if "unhealthy" in status:
            problems.append(
                (f"container.{service}", f"Container '{service}' is unhealthy", f"{name}: {status}")
            )
    return problems, running


def api_problem(env: Dict[str, str]) -> Optional[Problem]:
    host = env.get("API_BIND_ADDRESS") or "127.0.0.1"
    if host in {"0.0.0.0", "::", ""}:  # noqa: S104 (a comparison, not a bind)
        host = "127.0.0.1"
    url = f"http://{host}:{env.get('API_PORT') or '8010'}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            if resp.status == 200:
                return None
            detail = f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}: {exc.read(300).decode('utf-8', 'replace')}"
    except (OSError, ValueError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    return ("api.ready", "The API is not ready (/health/ready)", f"{url} -> {detail}")


def expected_plan_date(now_local: dt.datetime, plan_time: str) -> dt.date:
    """The day that must already have a daily plan at ``now_local``."""
    hour, minute = (int(x) for x in plan_time.split(":"))
    due = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0) + PLAN_GRACE
    today = now_local.date()
    return today + dt.timedelta(days=1) if now_local >= due else today


# Phase 10: every athlete with an active Intervals.icu connection and plan.
# One row per athlete: slug | timezone | first daily-plan date ('' if none)
# | daily-plan dates of the last 3 days (comma-separated) | plan age in minutes.
PLANS_SQL = (
    "SELECT a.slug, a.timezone, "
    "COALESCE((SELECT min(d.decision_date)::text FROM coach.coach_decisions d "
    "  WHERE d.athlete_id = a.id AND d.decision_type = 'daily_plan'), ''), "
    "COALESCE((SELECT string_agg(DISTINCT d.decision_date::text, ',') FROM coach.coach_decisions d "
    "  WHERE d.athlete_id = a.id AND d.decision_type = 'daily_plan' "
    "  AND d.decision_date >= current_date - 3), ''), "
    "floor(extract(epoch FROM now() - p.created_at) / 60)::int "
    "FROM coach.athlete_profile a "
    "JOIN coach.intervals_connections c ON c.athlete_id = a.id AND c.status = 'active' "
    "JOIN coach.training_plans p ON p.athlete_id = a.id AND p.status = 'active' "
    "ORDER BY a.id"
)
FIRST_PLAN_GRACE_MIN = 120  # onboarding plans within minutes; allow for retries


def plan_problems_from_rows(
    rows: List[List[str]], now: dt.datetime, plan_time: str
) -> List[Problem]:
    """Missing daily plans, one problem per athlete (pure; see PLANS_SQL)."""
    problems: List[Problem] = []
    for row in rows:
        if len(row) != 5:
            continue
        slug, tz_name, first, recent, age = row
        try:
            tz = ZoneInfo(tz_name)
        except (ValueError, KeyError):
            tz = ZoneInfo("UTC")
        day = expected_plan_date(now.astimezone(tz), plan_time)
        if not first:
            if age.isdigit() and int(age) > FIRST_PLAN_GRACE_MIN:
                problems.append(
                    (
                        "plan.first." + slug,
                        f"{slug}: no first plan since the goal was set",
                        "Onboarding should plan within minutes. "
                        "Check: podman logs --since 6h cycling-coach_scheduler_1",
                    )
                )
            continue
        if first > day.isoformat():
            continue  # joined after that day; their first plan is a later day
        if day.isoformat() in recent.split(","):
            continue
        problems.append(
            (
                "plan.missing." + slug,
                f"{slug}: no daily plan for {day.isoformat()}",
                "The 20:00 planning run didn't happen or didn't finish. "
                "Check: podman logs --since 6h cycling-coach_scheduler_1",
            )
        )
    return problems


def plan_problems(env: Dict[str, str], db_container: str, now: dt.datetime) -> List[Problem]:
    code, out = _psql(db_container, PLANS_SQL)
    if code != 0:
        return [
            ("plan.query", "Could not check the daily plans in the database", out.strip()[:300])
        ]
    rows = [line.split("|") for line in out.splitlines() if line.strip()]
    return plan_problems_from_rows(rows, now, env.get("SCHEDULER_PLAN_TIME") or "20:00")


def _psql(db_container: str, sql: str) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            ["podman", "exec", "-i", db_container, "sh", "-c", PSQL],
            input=sql + ";\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr or p.stdout)


def collect(env: Dict[str, str], now: dt.datetime) -> List[Problem]:
    containers, error = list_containers()
    problems, running = container_problems(containers, error)
    if "app" in running:
        p = api_problem(env)
        if p:
            problems.append(p)
    if "db" in running:
        problems.extend(plan_problems(env, running["db"], now))
    return problems


# --------------------------------------------------------------------------
# State and decisions
# --------------------------------------------------------------------------


def load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def decide(
    state: Dict[str, Any],
    problems: List[Problem],
    now: dt.datetime,
    repeat: dt.timedelta,
    threshold: int = THRESHOLD,
) -> Tuple[List[Problem], List[Problem], List[str]]:
    """Update ``state`` in place; return (new, repeated, resolved titles)."""
    open_: Dict[str, Any] = state.setdefault("problems", {})
    seen = {key for key, _, _ in problems}
    resolved: List[str] = []
    for key in [k for k in open_ if k not in seen]:
        gone = open_.pop(key)
        if gone.get("notified_at"):
            resolved.append(gone["title"])
    new: List[Problem] = []
    repeated: List[Problem] = []
    for key, title, detail in problems:
        entry = open_.setdefault(key, {"first_seen": now.isoformat(), "count": 0})
        entry.update(title=title, detail=detail, count=int(entry.get("count", 0)) + 1)
        if entry["count"] < threshold:
            continue
        notified = entry.get("notified_at")
        if not notified:
            entry["notified_at"] = now.isoformat()
            new.append((key, title, detail))
        elif now - dt.datetime.fromisoformat(notified) >= repeat:
            entry["notified_at"] = now.isoformat()
            repeated.append((key, title, detail))
    return new, repeated, resolved


def compose(
    new: List[Problem],
    repeated: List[Problem],
    resolved: List[str],
    rebooted: Optional[str],
    current: List[Problem],
) -> Optional[Tuple[str, str]]:
    lines: List[str] = []
    if rebooted:
        lines += [
            f"The host restarted ({rebooted}).",
            "Current state: " + ("all checks pass." if not current else "problems below."),
            "",
        ]
        if current and not (new or repeated):
            lines += [
                "Current problems (emailed if they persist):",
                *(f"[!] {t}\n    {d}" for _, t, d in current),
                "",
            ]
    if new:
        lines += ["New problems:", *(f"[!] {t}\n    {d}" for _, t, d in new), ""]
    if repeated:
        lines += ["Still unresolved:", *(f"[!] {t}\n    {d}" for _, t, d in repeated), ""]
    if resolved:
        lines += ["Resolved:", *(f"[OK] {t}" for t in resolved), ""]
    if not lines:
        return None
    if new or repeated:
        first = (new or repeated)[0][1]
        more = len(new) + len(repeated) - 1
        subject = f"Problem: {first}" + (f" (+{more} more)" if more else "")
    elif rebooted:
        subject = "Host restarted, " + ("coach running" if not current else "checking problems")
    else:
        subject = f"Resolved: {resolved[0]}"
    lines += [f"Host: {socket.gethostname()}", "Runbook: /opt/cycledays/docs/OPERATIONS.md"]
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def send_email(env: Dict[str, str], subject: str, body: str) -> Tuple[bool, str]:
    missing = email_problems(env)
    if missing:
        return False, "; ".join(missing)
    msg = EmailMessage()
    msg["Subject"] = f"{SUBJECT_PREFIX} {subject}"
    msg["From"] = env.get("NOTIFY_EMAIL_FROM") or env.get("SMTP_USERNAME", "")
    msg["To"] = ", ".join(recipients(env))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="cycledays.local")
    msg.set_content(body)
    host = env["SMTP_HOST"]
    port = int(env.get("SMTP_PORT") or 587)
    security = (env.get("SMTP_SECURITY") or "starttls").lower()
    timeout = float(env.get("SMTP_TIMEOUT_SECONDS") or 20)
    context = ssl.create_default_context()
    try:
        smtp = (
            smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            if security == "ssl"
            else smtplib.SMTP(host, port, timeout=timeout)
        )
        with smtp:
            if security == "starttls":
                smtp.starttls(context=context)
            if env.get("SMTP_USERNAME") and env.get("SMTP_PASSWORD"):
                smtp.login(env["SMTP_USERNAME"], env["SMTP_PASSWORD"])
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "sent"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def current_boot_id() -> str:
    try:
        return BOOT_ID.read_text().strip()
    except OSError:
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--dry-run", action="store_true", help="print only; no email, no state")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = ap.parse_args(argv)

    env = read_env(args.env_file)
    if args.test_email:
        ok, info = send_email(
            env, "Test email", f"The CycleDays watchdog on {socket.gethostname()} can send email."
        )
        print(json.dumps({"ok": ok, "info": info}))
        return 0 if ok else 1

    now = dt.datetime.now(dt.timezone.utc)
    problems = collect(env, now)
    state = load_state(args.state)
    boot_id = current_boot_id()
    rebooted = None
    if state.get("boot_id") and boot_id and state["boot_id"] != boot_id:
        code, out = run(["uptime", "-s"])
        rebooted = f"up since {out.strip()}" if code == 0 else "new boot"
    repeat = dt.timedelta(hours=float(env.get("NOTIFY_REPEAT_HOURS") or 12))
    new, repeated, resolved = decide(state, problems, now, repeat)
    message = compose(new, repeated, resolved, rebooted, problems)
    report: Dict[str, Any] = {
        "time": now.isoformat(),
        "problems": [{"key": k, "title": t, "detail": d} for k, t, d in problems],
        "rebooted": rebooted,
        "email": None,
    }
    if message is not None:
        if args.dry_run:
            report["email"] = {"subject": message[0], "body": message[1], "sent": False}
        else:
            ok, info = send_email(env, *message)
            report["email"] = {"subject": message[0], "sent": ok, "info": info}
    if not args.dry_run:
        state["boot_id"] = boot_id
        state["last_run"] = now.isoformat()
        save_state(args.state, state)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
