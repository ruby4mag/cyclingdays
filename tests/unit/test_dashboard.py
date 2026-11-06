"""Dashboard (Phase 8): pure aggregations, the page's security properties, zone evaluation."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import STATIC_DIR, WEB_DIR
from app.main import create_app
from app.services.dashboard import (
    aggregate_zones,
    calendar_days,
    icu_zone_seconds,
    plan_progress,
    week_start,
    weekly_volume,
    workout_steps,
)
from app.training.evaluation import EVALUATION_VERSION, evaluate_ride
from app.training.plan_builder import build_plan
from tests.conftest import make_unit_settings
from tests.unit.test_auth import signed_in_client

MON = dt.date(2026, 11, 2)
PLAN = build_plan(start=MON, weekly_hours=(6, 9))


def test_week_start_is_monday() -> None:
    assert week_start(dt.date(2026, 11, 8)) == MON
    assert week_start(MON) == MON


def test_weekly_volume_uses_metrics_for_load_and_plan_targets() -> None:
    prev = MON - dt.timedelta(weeks=1)
    today = MON + dt.timedelta(days=3)
    daily = {prev + dt.timedelta(days=i): 40.0 for i in range(7)}
    daily.update({MON + dt.timedelta(days=i): 50.0 for i in range(4)})
    rides = [
        {"date": prev + dt.timedelta(days=1), "moving_s": 3600, "distance_m": 30000},
        {"date": MON + dt.timedelta(days=1), "moving_s": 5400, "distance_m": 45000},
        {"date": MON + dt.timedelta(days=2), "moving_s": 1800, "distance_m": None},
    ]
    weeks = weekly_volume(mondays=[prev, MON], today=today, daily_tss=daily, rides=rides, plan=PLAN)
    last, current = weeks
    assert last["tss"] == 280.0 and last["finished"] is True and last["tss_complete"] is True
    assert last["target_tss"] is None and last["compliance_pct"] is None  # before the plan
    assert current["tss"] == 200.0 and current["finished"] is False
    assert current["hours"] == 2.0 and current["rides"] == 2 and current["distance_km"] == 45.0
    assert current["target_tss"] == 300.0 and current["target_hours"] == 6.0
    assert current["plan_phase"] == "Block 1 week 1"
    # Compliance only for finished weeks: an unfinished week isn't "behind".
    assert current["compliance_pct"] is None


def test_weekly_volume_missing_metrics_stay_missing() -> None:
    weeks = weekly_volume(
        mondays=[MON], today=MON + dt.timedelta(days=10), daily_tss={}, rides=[], plan=PLAN
    )
    assert weeks[0]["tss"] is None  # not 0: no metrics is not "no training"
    assert weeks[0]["tss_complete"] is False
    assert weeks[0]["compliance_pct"] is None


def test_weekly_volume_compliance_for_finished_plan_week() -> None:
    daily = {MON + dt.timedelta(days=i): 30.0 for i in range(7)}
    weeks = weekly_volume(
        mondays=[MON], today=MON + dt.timedelta(days=7), daily_tss=daily, rides=[], plan=PLAN
    )
    assert weeks[0]["compliance_pct"] == 70.0  # 210 of 300


def test_icu_zone_seconds_parses_power_zones_only() -> None:
    raw = [
        {"id": "Z1", "secs": 100},
        {"id": "Z2", "secs": 200.0},
        {"id": "SS", "secs": 50},
        {"id": "Z9", "secs": 5},
        {"id": "Z3", "secs": -1},
        "junk",
    ]
    assert icu_zone_seconds(raw) == {1: 100, 2: 200}
    assert icu_zone_seconds(None) is None
    assert icu_zone_seconds([{"id": "SS", "secs": 10}]) is None


def test_aggregate_zones_prefers_coach_zones_and_reports_sources() -> None:
    coach = {"seconds_by_zone": {"1": 600, "2": 3000, "3": 0, "4": 0, "5": 0, "6": 0, "7": 0}}
    result = aggregate_zones(
        [
            # Both sources present: only the coach's zones count.
            {"coach_zones": coach, "icu_zone_times": [{"id": "Z5", "secs": 9999}]},
            {"coach_zones": None, "icu_zone_times": [{"id": "Z2", "secs": 400}]},
            {"coach_zones": None, "icu_zone_times": None},
        ]
    )
    by_zone = {z["zone"]: z for z in result["zones"]}
    assert by_zone[2]["seconds"] == 3400
    assert by_zone[2]["coach_seconds"] == 3000 and by_zone[2]["intervals_icu_seconds"] == 400
    assert by_zone[5]["seconds"] == 0
    assert result["total_seconds"] == 4000
    assert (result["rides_coach"], result["rides_intervals_icu"]) == (1, 1)
    assert result["rides_without_zones"] == 1 and result["rides"] == 3
    assert sum(z["fraction"] for z in result["zones"]) == pytest.approx(1.0)


def test_aggregate_zones_empty_has_no_fractions() -> None:
    result = aggregate_zones([])
    assert result["total_seconds"] == 0
    assert all(z["fraction"] is None for z in result["zones"])
    assert [z["zone"] for z in result["zones"]] == [1, 2, 3, 4, 5, 6, 7]


def test_plan_progress_states_and_compliance() -> None:
    today = MON + dt.timedelta(days=9)  # Wednesday of week 2
    daily = {MON + dt.timedelta(days=i): 30.0 for i in range(10)}
    minutes = {MON: 60.0, MON + dt.timedelta(days=8): 90.0}
    p = plan_progress(PLAN, today=today, daily_tss=daily, minutes_by_day=minutes)
    w1, w2, w3 = p["weeks"][:3]
    assert w1["state"] == "past" and w1["tss"] == 210.0 and w1["compliance_pct"] == 70.0
    assert w1["minutes"] == 60
    assert w2["state"] == "current" and w2["tss"] == 90.0 and w2["compliance_pct"] is None
    assert w2["minutes"] == 90
    assert w3["state"] == "future" and w3["tss"] is None
    assert p["current_index"] == 1 and p["weeks_done"] == 1
    assert p["weeks_total"] == len(PLAN.phases)
    assert p["completed_weeks_compliance_pct"] == 70.0


def test_calendar_days_states() -> None:
    today = MON + dt.timedelta(days=3)  # Thursday
    planned = [
        {"date": MON + dt.timedelta(days=1), "type": "tempo", "status": "published", "name": "A"},
        {"date": MON + dt.timedelta(days=2), "type": "endurance", "status": "missed", "name": "B"},
        {
            "date": MON + dt.timedelta(days=4),
            "type": "recovery",
            "status": "published",
            "name": "C",
        },
    ]
    rides = [{"date": MON + dt.timedelta(days=1), "name": "ride", "outcome": "as_planned"}]
    events = [{"date": MON + dt.timedelta(days=5), "name": "Club ride", "category": "NOTE"}]
    days = calendar_days(
        start=MON,
        end=MON + dt.timedelta(days=6),
        today=today,
        plan=PLAN,
        planned=planned,
        rides=rides,
        events=events,
    )
    states = [d["state"] for d in days]
    assert states == ["rest", "completed", "missed", None, "planned", None, None]
    assert [d["plan_role"] for d in days] == [
        "rest",
        "key",
        "endurance",
        "key",
        "recovery",
        "long",
        "endurance",
    ]
    assert days[3]["is_today"] is True
    assert days[5]["events"][0]["name"] == "Club ride"
    assert days[1]["workout"]["date"] == "2026-11-03"  # dates serialized


def test_calendar_without_plan_has_no_roles() -> None:
    days = calendar_days(start=MON, end=MON, today=MON, plan=None, planned=[], rides=[], events=[])
    assert days[0]["plan_role"] is None and days[0]["state"] is None


def test_workout_steps_keeps_drawing_fields_only() -> None:
    structure = {
        "workout": {
            "steps": [
                {"index": 1, "kind": "warmup", "duration_s": 600, "low_pct": 55, "secret": "x"}
            ]
        }
    }
    (step,) = workout_steps(structure)
    assert step["duration_s"] == 600 and step["low_pct"] == 55
    assert "secret" not in step and step["high_pct"] is None
    assert workout_steps({}) == []


def test_ride_evaluation_records_power_zones() -> None:
    power = [100.0] * 600 + [150.0] * 300 + [None] * 10
    result = evaluate_ride(
        planned=None,
        activity={"moving_time_s": 910},
        power=power,
        heartrate=None,
        ftp_watts=175,
    )
    assert EVALUATION_VERSION == "evaluation/v2"
    zones = result["zones"]
    assert zones["ftp_watts"] == 175
    assert zones["seconds_by_zone"]["2"] == 600  # 100 W = 57 % FTP
    assert zones["seconds_by_zone"]["3"] == 300  # 150 W = 86 % FTP
    assert zones["missing_seconds"] == 10
    no_ftp = evaluate_ride(planned=None, activity={}, power=power, heartrate=None)
    assert no_ftp["zones"] is None


# --------------------------------------------------------------------------
# Page and static files (no database needed)
# --------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return signed_in_client(create_app(make_unit_settings(), athlete_slug="primary"))


def test_root_redirects_to_dashboard(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "dashboard"


def test_dashboard_page_has_strict_csp(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.head("/dashboard").status_code == 200


def test_static_assets_served(client: TestClient) -> None:
    for path, kind in (("dashboard.js", "javascript"), ("dashboard.css", "text/css")):
        response = client.get(f"/static/{path}")
        assert response.status_code == 200
        assert kind in response.headers["content-type"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-cache"  # always revalidated


def test_docs_page_keeps_working_without_page_csp(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


def test_page_loads_nothing_external_and_nothing_inline() -> None:
    html = (WEB_DIR / "index.html").read_text()
    # Every <script> has a same-origin src; no inline handlers or styles.
    scripts = re.findall(r"<script\b([^>]*)>", html)
    assert scripts and all('src="static/' in attrs for attrs in scripts)
    assert not re.search(r"\son[a-z]+=", html)
    assert "style=" not in html
    for name in (
        "index.html",
        "login.html",
        "static/dashboard.js",
        "static/dashboard.css",
        "static/login.css",
    ):
        text = (WEB_DIR / name).read_text()
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), name


def test_script_never_parses_api_strings_as_html() -> None:
    js = (STATIC_DIR / "dashboard.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js, sink
    # Inline style attributes would be blocked by the CSP; CSSOM is used instead.
    assert 'setAttribute("style"' not in js
