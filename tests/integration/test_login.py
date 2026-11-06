"""Web login against a migrated database: users table, CLI helpers, full sign-in."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.connection import create_session_factory, session_scope
from app.main import create_app
from app.services import users

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def client(integration_settings: Settings, test_database_url: str) -> Iterator[TestClient]:
    db = test_database_url.rsplit("/", 1)[1]
    settings = integration_settings.model_copy(update={"postgres_db": db, "auth_secret_key": None})
    with TestClient(create_app(settings)) as c:
        yield c


async def test_user_management(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as session:
        created = await users.create_user(session, " Rider ", PASSWORD)
        assert created.username == "rider" and created.is_active
    async with session_scope(factory) as session:
        with pytest.raises(users.UserExistsError):
            await users.create_user(session, "rider", PASSWORD)
    async with session_scope(factory) as session:
        await users.set_active(session, "rider", False)
        listed = await users.list_users(session)
        assert [(u["username"], u["active"]) for u in listed] == [("rider", False)]
    async with session_scope(factory) as session:
        with pytest.raises(users.UserNotFoundError):
            await users.set_password(session, "nobody", PASSWORD)
        await users.delete_user(session, "rider")
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM coach.app_users")) == 0


async def test_username_format_is_enforced_by_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO coach.app_users (username, password_hash) VALUES ('Bad Name', 'x')"
                )
            )


async def test_sign_in_with_a_stored_user(
    factory: async_sessionmaker[AsyncSession], client: TestClient
) -> None:
    async with session_scope(factory) as session:
        await users.create_user(session, "rider", PASSWORD)

    assert client.get("/api/dashboard/summary").status_code == 401
    bad = client.post("/login", data={"username": "rider", "password": "wrong password!"})
    assert bad.status_code == 401
    ok = client.post(
        "/login", data={"username": "rider", "password": PASSWORD}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert client.get("/api/session").json() == {
        "username": "rider",
        "athlete": None,
        "setup_required": True,
    }
    async with factory() as session:
        last = await session.scalar(text("SELECT last_login_at FROM coach.app_users"))
        assert last is not None

    async with session_scope(factory) as session:
        await users.set_password(session, "rider", "another long password")
    assert client.get("/api/session").status_code == 401  # old session ended
