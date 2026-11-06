"""Phase 9: host watchdog logic (deploy/watchdog; pure parts, no podman)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "cycledays_watchdog", ROOT / "deploy" / "watchdog" / "cycledays_watchdog.py"
)
assert _spec and _spec.loader
wd: Any = importlib.util.module_from_spec(_spec)
sys.modules["cycledays_watchdog"] = wd
_spec.loader.exec_module(wd)

IST = ZoneInfo("Asia/Kolkata")
T0 = dt.datetime(2026, 11, 6, 5, 0, tzinfo=dt.UTC)


def container(
    service: str, state: str = "running", status: str = "Up 1 hour (healthy)"
) -> dict[str, Any]:
    return {
        "Names": [f"cycling-coach_{service}_1"],
        "State": state,
        "Status": status,
        "Labels": {"com.docker.compose.service": service},
    }


def test_read_env(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# comment\nSMTP_HOST=smtp.gmail.com\nATHLETE_PRIMARY_OBJECTIVE=1200 km brevet\n"
        "SMTP_PASSWORD='ab cd'\nEMPTY=\nnot a pair\n"
    )
    env = wd.read_env(f)
    assert env["SMTP_HOST"] == "smtp.gmail.com"
    assert env["ATHLETE_PRIMARY_OBJECTIVE"] == "1200 km brevet"
    assert env["SMTP_PASSWORD"] == "ab cd" and env["EMPTY"] == ""
    assert wd.read_env(tmp_path / "missing") == {}


def test_email_settings_check() -> None:
    assert wd.email_problems({}) == ["NOTIFY_EMAIL_ENABLED is false"]
    env = {
        "NOTIFY_EMAIL_ENABLED": "true",
        "SMTP_HOST": "h",
        "NOTIFY_EMAIL_TO": "a@b.c, d@e.f",
        "SMTP_USERNAME": "u@b.c",
    }
    assert wd.email_problems(env) == [] and wd.recipients(env) == ["a@b.c", "d@e.f"]


def test_container_problems() -> None:
    healthy = [container(s) for s in ("db", "app", "scheduler")]
    problems, running = wd.container_problems(healthy)
    assert problems == [] and set(running) == {"db", "app", "scheduler"}

    broken = [
        container("db"),
        container("app", status="Up 2 minutes (unhealthy)"),
        container("scheduler", state="exited", status="Exited (1) 3 minutes ago"),
    ]
    problems, running = wd.container_problems(broken)
    assert [p[0] for p in problems] == ["container.app", "container.scheduler"]
    assert set(running) == {"db", "app"}  # unhealthy still counts as running

    problems, _ = wd.container_problems([container("db")])
    assert [p[1] for p in problems] == [
        "Container 'app' is missing",
        "Container 'scheduler' is missing",
    ]
    problems, _ = wd.container_problems(None, "podman: not found")
    assert problems[0][0] == "podman"


def test_expected_plan_date() -> None:
    day = dt.date(2026, 11, 6)

    def at(hour: int, minute: int) -> dt.datetime:
        return dt.datetime(2026, 11, 6, hour, minute, tzinfo=IST)

    assert wd.expected_plan_date(at(10, 0), "20:00") == day
    assert wd.expected_plan_date(at(21, 29), "20:00") == day  # still in the grace period
    assert wd.expected_plan_date(at(21, 30), "20:00") == day + dt.timedelta(days=1)


def test_decide_threshold_repeat_resolve() -> None:
    state: dict[str, Any] = {}
    p = [("api.ready", "API not ready", "HTTP 503")]
    repeat = dt.timedelta(hours=12)
    assert wd.decide(state, p, T0, repeat) == ([], [], [])  # first sighting: quiet
    new, repeated, resolved = wd.decide(state, p, T0 + dt.timedelta(minutes=10), repeat)
    assert new == p and not repeated and not resolved
    assert wd.decide(state, p, T0 + dt.timedelta(hours=1), repeat) == ([], [], [])
    _, repeated, _ = wd.decide(state, p, T0 + dt.timedelta(hours=12, minutes=20), repeat)
    assert repeated == p
    assert wd.decide(state, [], T0 + dt.timedelta(hours=13), repeat) == ([], [], ["API not ready"])
    assert state["problems"] == {}
    # A blip that was never emailed disappears silently.
    wd.decide(state, p, T0, repeat)
    assert wd.decide(state, [], T0, repeat) == ([], [], [])


def test_compose_messages() -> None:
    assert wd.compose([], [], [], None, []) is None
    p = ("api.ready", "API not ready", "HTTP 503")
    subject, body = wd.compose([p], [], [], None, [p])
    assert subject == "Problem: API not ready" and "HTTP 503" in body
    subject, body = wd.compose([], [], [], "up since 2026-11-06 10:00:00", [])
    assert subject == "Host restarted, coach running" and "all checks pass" in body
    subject, body = wd.compose([], [], [], "new boot", [p])
    assert subject == "Host restarted, checking problems" and "API not ready" in body
    subject, _ = wd.compose([], [], ["API not ready"], None, [])
    assert subject == "Resolved: API not ready"


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(wd, "collect", lambda env, now: [])
    state = tmp_path / "state.json"
    assert wd.main(["--env-file", str(tmp_path / "none"), "--state", str(state), "--dry-run"]) == 0
    assert not state.exists()
    assert wd.main(["--env-file", str(tmp_path / "none"), "--state", str(state)]) == 0
    assert state.exists()


def test_plan_problems_per_athlete_in_their_timezone() -> None:
    # 16:30 UTC = 22:00 in Kolkata (past 20:00 + 90 min: tomorrow is due),
    # 17:30 in Paris (still today).
    now = dt.datetime(2026, 11, 6, 16, 30, tzinfo=dt.UTC)
    rows = [
        ["anna", "Asia/Kolkata", "2026-10-01", "2026-11-06,2026-11-07", "9000"],
        ["ben", "Asia/Kolkata", "2026-10-01", "2026-11-05,2026-11-06", "9000"],
        ["cara", "Europe/Paris", "2026-10-01", "2026-11-06", "9000"],
        ["dev", "Asia/Kolkata", "", "", "30"],  # goal just set: onboarding pending
        ["eve", "Asia/Kolkata", "", "", "500"],  # goal set hours ago, never planned
        ["finn", "Asia/Kolkata", "2026-11-08", "2026-11-08", "60"],  # joined after
        ["bad row"],
    ]
    problems = wd.plan_problems_from_rows(rows, now, "20:00")
    assert [p[0] for p in problems] == ["plan.missing.ben", "plan.first.eve"]
    assert problems[0][1] == "ben: no daily plan for 2026-11-07"
