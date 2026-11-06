"""Shared test fixtures.

Unit tests never touch the network or the developer's ``.env``.
Integration tests (``-m integration``) use a throwaway database
``<POSTGRES_DB>_test`` on the server configured in ``.env``; it is dropped and
recreated per test session. If the server is unreachable they are skipped
with an explicit reason (set ``REQUIRE_DB=1`` to make that a failure).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.database.connection import create_db_engine
from app.database.models import APP_SCHEMA, Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_unit_settings(**overrides: Any) -> Settings:
    """Settings isolated from the environment and any .env file."""
    values: dict[str, Any] = {
        "postgres_password": "unit-test-password",
        "postgres_host": "127.0.0.1",
        "postgres_port": 1,  # nothing listens here: connection refused immediately
        "app_env": "test",
        "log_format": "json",
        "health_db_timeout_seconds": 2.0,
        "db_connect_timeout_seconds": 1,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def unit_settings() -> Settings:
    return make_unit_settings()


# --------------------------------------------------------------------------
# Integration fixtures
# --------------------------------------------------------------------------


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    return cfg


def recreate_database(settings: Settings, name: str) -> str:
    admin_url = _libpq(settings.sqlalchemy_url(database="postgres"))
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return settings.sqlalchemy_url(database=name)


def drop_database(settings: Settings, name: str) -> None:
    admin_url = _libpq(settings.sqlalchemy_url(database="postgres"))
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    try:
        settings = Settings(app_env="test", database_url=None)
    except Exception as exc:  # missing .env / POSTGRES_PASSWORD
        _skip_or_fail(f"integration settings unavailable: {exc}")
        raise
    try:
        with psycopg.connect(
            _libpq(settings.sqlalchemy_url(database="postgres")), connect_timeout=3
        ):
            pass
    except psycopg.OperationalError as exc:
        _skip_or_fail(
            f"PostgreSQL not reachable at {settings.postgres_host}:{settings.postgres_port} "
            f"({exc.__class__.__name__}); run `docker compose up -d db`"
        )
    return settings


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("REQUIRE_DB") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def test_database_url(integration_settings: Settings) -> Iterator[str]:
    """Fresh database migrated to head, shared by the integration session."""
    name = f"{integration_settings.postgres_db}_test"
    url = recreate_database(integration_settings, name)
    command.upgrade(alembic_config(url), "head")
    yield url
    drop_database(integration_settings, name)


@pytest.fixture
async def engine(
    integration_settings: Settings, test_database_url: str
) -> AsyncIterator[AsyncEngine]:
    eng = create_db_engine(integration_settings, url=test_database_url)
    try:
        yield eng
    finally:
        # Leave the shared database empty for the next test. DELETE in reverse FK
        # order: TRUNCATE costs ~3 s per call on this VM's storage (fsync of new
        # relation files). publishing_audit is append-only (DELETE blocked by a
        # trigger), so it is truncated, and only when it actually has rows.
        async with eng.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                name = f"{APP_SCHEMA}.{table.name}"
                if table.name == "publishing_audit":
                    if await conn.scalar(text(f"SELECT EXISTS (SELECT 1 FROM {name})")):
                        await conn.exec_driver_sql(f"TRUNCATE {name}")
                else:
                    await conn.exec_driver_sql(f"DELETE FROM {name}")
        await eng.dispose()
