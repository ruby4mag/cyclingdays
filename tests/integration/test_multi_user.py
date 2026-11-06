"""Phase 10: several users, each with their own athlete, against a real database.

Covers onboarding through the Account API (connect Intervals.icu, profile,
goal, email), tenant isolation of the dashboard API, encrypted key storage,
the scheduler's athlete queries, and admin linking.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.connection import create_session_factory, session_scope
from app.database.models import AthleteProfile, IntervalsConnection
from app.integrations.intervals_client import IntervalsClient
from app.main import create_app
from app.scheduler import coached_athletes, summary_recipients
from app.services import connections, users
from app.services.connections import IntervalsNotConnectedError

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
KEY = Fernet.generate_key().decode()
# Intervals.icu as seen by the fake: athlete id -> (API key, athlete object)
REMOTE: dict[str, tuple[str, dict[str, Any]]] = {
    "i100": (
        "anna-key",
        {
            "id": "i100",
            "name": "Anna Rider",
            "timezone": "Europe/Paris",
            "icu_weight": 61.5,
            "icu_api_key": "must-not-be-stored",
            "sportSettings": [{"types": ["Ride", "VirtualRide"], "ftp": 230}],
        },
    ),
    "i200": ("ben-key", {"id": "i200", "name": "Ben", "timezone": "Not/AZone"}),
}


def fake_client(settings: Settings, athlete_id: str, api_key: str) -> IntervalsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        key, athlete = REMOTE.get(athlete_id, ("", {}))
        expected = "Basic " + base64.b64encode(f"API_KEY:{key}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json=athlete)

    return IntervalsClient(
        api_key=api_key,
        athlete_id=athlete_id,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def settings(integration_settings: Settings, test_database_url: str) -> Settings:
    db = test_database_url.rsplit("/", 1)[1]
    return integration_settings.model_copy(
        update={
            "postgres_db": db,
            "auth_secret_key": None,
            "credentials_encryption_key": SecretStr(KEY),
        }
    )


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def app_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> Iterator[TestClient]:
    monkeypatch.setattr(connections, "client_for", fake_client)
    with TestClient(create_app(settings)) as c:
        yield c


def _sign_in(client: TestClient, username: str) -> TestClient:
    # Shares the app (and its started lifespan) but has its own cookies.
    other = TestClient(client.app)
    response = other.post(
        "/login", data={"username": username, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return other


JSON_HEADERS = {"Origin": "http://testserver"}


async def _make_users(factory: async_sessionmaker[AsyncSession], *names: str) -> None:
    async with session_scope(factory) as session:
        for name in names:
            await users.create_user(session, name, PASSWORD)


def _onboard(client: TestClient, athlete_id: str, key: str) -> dict[str, Any]:
    r = client.put(
        "/api/account/intervals",
        json={"athlete_id": athlete_id, "api_key": key},
        headers=JSON_HEADERS,
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


GOAL = {
    "objective": "1200 km brevet",
    "event_date": None,
    "weekly_hours_min": 6,
    "weekly_hours_max": 9,
    "start_date": None,
    "week_template": None,
}


async def test_onboarding_flow(
    app_client: TestClient, factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    await _make_users(factory, "anna")
    anna = _sign_in(app_client, "anna")

    # Before setup: the dashboard API says so, the account page shows step 1.
    assert anna.get("/api/session").json()["setup_required"] is True
    assert anna.get("/api/dashboard/summary").status_code == 409
    account = anna.get("/api/account").json()
    assert account["athlete"] is None and account["setup"]["complete"] is False
    assert anna.put("/api/account/profile", json={}, headers=JSON_HEADERS).status_code == 422

    # A wrong key is refused before anything is stored.
    bad = anna.put(
        "/api/account/intervals",
        json={"athlete_id": "i100", "api_key": "wrong"},
        headers=JSON_HEADERS,
    )
    assert bad.status_code == 422 and "refused" in bad.json()["detail"]

    account = _onboard(anna, "i100", "anna-key")
    athlete = account["athlete"]
    assert athlete["slug"] == "anna" and athlete["display_name"] == "Anna Rider"
    assert athlete["ftp_watts"] == 230 and athlete["weight_kg"] == 61.5
    assert athlete["timezone"] == "Europe/Paris"
    assert account["connection"]["status"] == "active"
    assert account["setup"] == {
        "connected": True,
        "profile": True,
        "goal": False,
        "complete": False,
    }
    assert "anna-key" not in anna.get("/api/account").text

    # Stored encrypted; the raw snapshot has no secrets.
    async with factory() as session:
        row = await session.scalar(select(IntervalsConnection))
        assert row is not None and "anna-key" not in row.api_key_encrypted
        profile = await session.scalar(select(AthleteProfile).where(AthleteProfile.slug == "anna"))
        assert profile is not None and "must-not-be-stored" not in str(profile.intervals_snapshot)

    # The session now carries the athlete (no re-login needed).
    assert anna.get("/api/session").json() == {
        "username": "anna",
        "athlete": "anna",
        "setup_required": False,
    }

    profile_body = {
        "display_name": "Anna",
        "ftp_watts": 235,
        "weight_kg": 61,
        "height_cm": 168,
        "age_years": 38,
        "experience_level": "advanced",
        "timezone": "Europe/Paris",
        "secondary_objective": "",
        "indoor_platform": "Zwift",
        "wearable": None,
    }
    r = anna.put("/api/account/profile", json=profile_body, headers=JSON_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["athlete"]["ftp_watts"] == 235
    assert r.json()["athlete"]["secondary_objective"] is None
    bad_tz = anna.put(
        "/api/account/profile", json={**profile_body, "timezone": "Mars/Base"}, headers=JSON_HEADERS
    )
    assert bad_tz.status_code == 422

    # Goal: preview stores nothing, save builds the plan.
    preview = anna.post("/api/account/goal/preview", json=GOAL, headers=JSON_HEADERS)
    assert preview.status_code == 200 and preview.json()["dry_run"] is True
    assert anna.get("/api/account").json()["goal"] is None
    week = {"mon": "rest", "tue": "key", "wed": "endurance", "thu": "recovery",
            "fri": "rest", "sat": "long", "sun": "long"}  # fmt: skip
    saved = anna.put(
        "/api/account/goal", json={**GOAL, "week_template": week}, headers=JSON_HEADERS
    )
    assert saved.status_code == 200, saved.text
    account = saved.json()["account"]
    assert account["setup"]["complete"] is True
    assert account["goal"]["objective"] == "1200 km brevet"
    assert account["goal"]["weekly_hours"] == [6, 9]
    assert account["goal"]["week_template"] == week
    too_soon = anna.put(
        "/api/account/goal",
        json={**GOAL, "event_date": account["goal"]["start_date"]},
        headers=JSON_HEADERS,
    )
    assert too_soon.status_code == 422
    two_keys = anna.put(
        "/api/account/goal",
        json={**GOAL, "week_template": {**week, "wed": "key", "sun": "endurance"}},
        headers=JSON_HEADERS,
    )
    assert two_keys.status_code == 422 and "in a row" in two_keys.json()["detail"]

    r = anna.put(
        "/api/account/notifications",
        json={"email": "anna@example.com", "daily_summary": True},
        headers=JSON_HEADERS,
    )
    assert r.status_code == 200 and r.json()["user"]["email"] == "anna@example.com"
    bad_mail = anna.put("/api/account/notifications", json={"email": "nope"}, headers=JSON_HEADERS)
    assert bad_mail.status_code == 422

    # Dashboard now works for anna.
    assert anna.get("/api/dashboard/summary").json()["athlete"]["slug"] == "anna"

    # Scheduler queries see her.
    coached = await coached_athletes(engine)
    assert [(a.slug, a.has_plan, a.never_synced, a.ever_planned) for a in coached] == [
        ("anna", True, True, False)
    ]
    assert await summary_recipients(engine, "anna") == ["anna@example.com"]

    # Disconnect: coaching stops, data stays.
    r = anna.delete("/api/account/intervals", headers=JSON_HEADERS)
    assert r.status_code == 200 and r.json()["connection"] is None
    assert r.json()["athlete"]["slug"] == "anna"
    assert await coached_athletes(engine) == []


async def test_users_only_ever_see_their_own_athlete(
    app_client: TestClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await _make_users(factory, "anna", "ben", "mallory")
    anna, ben, mallory = (_sign_in(app_client, n) for n in ("anna", "ben", "mallory"))
    _onboard(anna, "i100", "anna-key")
    ben_account = _onboard(ben, "i200", "ben-key")
    assert ben_account["athlete"]["timezone"] == settings.app_timezone  # remote tz invalid
    assert ben_account["athlete"]["ftp_watts"] is None
    assert ben_account["setup"]["profile"] is False  # FTP still needed

    assert anna.get("/api/dashboard/summary").json()["athlete"]["slug"] == "anna"
    assert ben.get("/api/dashboard/summary").json()["athlete"]["slug"] == "ben"
    # Query parameters can't pick another athlete.
    assert ben.get("/api/dashboard/summary?athlete=anna").json()["athlete"]["slug"] == "ben"

    # Nobody can take over an Intervals.icu athlete that's already connected,
    # even with the right key.
    stolen = mallory.put(
        "/api/account/intervals",
        json={"athlete_id": "i100", "api_key": "anna-key"},
        headers=JSON_HEADERS,
    )
    assert stolen.status_code == 409
    assert mallory.get("/api/account").json()["athlete"] is None
    # ...and an athlete can't be switched to another person's data.
    switch = ben.put(
        "/api/account/intervals",
        json={"athlete_id": "i100", "api_key": "anna-key"},
        headers=JSON_HEADERS,
    )
    assert switch.status_code == 409

    # Each athlete's client uses their own key.
    sessions = create_session_factory(app_client.app.state.engine)  # type: ignore[attr-defined]
    async with await connections.open_client(settings, sessions, "ben") as client:
        assert client.athlete_id == "i200"
    await connections.mark_invalid(sessions, "ben", "401 from Intervals.icu")
    with pytest.raises(IntervalsNotConnectedError, match="invalid"):
        await connections.open_client(settings, sessions, "ben")
    assert await connections.connected_slugs(sessions) == ["anna"]
    assert ben.get("/api/account").json()["connection"]["status"] == "invalid"
    # Reconnecting with a working key reactivates it.
    assert _onboard(ben, "i200", "ben-key")["connection"]["status"] == "active"


async def test_writes_need_same_origin_json(
    app_client: TestClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    await _make_users(factory, "anna")
    anna = _sign_in(app_client, "anna")
    body = {"athlete_id": "i100", "api_key": "anna-key"}
    cross = anna.put(
        "/api/account/intervals", json=body, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    form = anna.put(
        "/api/account/intervals",
        content="athlete_id=i100&api_key=anna-key",
        headers={**JSON_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    extra = anna.put(
        "/api/account/intervals", json={**body, "athlete": "ben"}, headers=JSON_HEADERS
    )
    assert extra.status_code == 422  # unknown fields are refused
    anon = TestClient(app_client.app)
    assert anon.get("/api/account").status_code == 401
    assert anon.get("/account", follow_redirects=False).status_code == 303


async def test_admin_link_and_email(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as session:
        await users.create_user(session, "owner", PASSWORD, email="owner@example.com")
        await users.create_user(session, "other", PASSWORD)
        session.add(
            AthleteProfile(
                slug="primary", display_name="P", experience_level="intermediate",
                timezone="Asia/Kolkata",
            )
        )  # fmt: skip
    async with session_scope(factory) as session:
        await users.link_athlete(session, "owner", "primary")
    async with session_scope(factory) as session:
        with pytest.raises(users.UserExistsError, match="already belongs"):
            await users.link_athlete(session, "other", "primary")
        with pytest.raises(users.UserNotFoundError):
            await users.link_athlete(session, "other", "nobody")
        with pytest.raises(ValueError, match="not an email"):
            await users.set_email(session, "other", "no-at-sign")
    async with session_scope(factory) as session:
        listed = {u["username"]: u for u in await users.list_users(session)}
        assert listed["owner"]["athlete"] == "primary"
        assert listed["owner"]["email"] == "owner@example.com"
        assert listed["other"]["athlete"] is None
    store = users.SqlUserStore(factory)
    record = await store.get_by_username("owner")
    assert record is not None and record.athlete_slug == "primary"
    async with factory() as session:
        # Deleting the athlete unlinks the user instead of deleting them.
        await session.execute(text("DELETE FROM coach.athlete_profile WHERE slug = 'primary'"))
        await session.commit()
    record = await store.get_by_username("owner")
    assert record is not None and record.athlete_slug is None
