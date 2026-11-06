"""Database engine/session management.

The application uses SQLAlchemy's async engine on the psycopg 3 driver.
Alembic uses the same URL with the synchronous psycopg driver.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.database.models import APP_SCHEMA

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def create_db_engine(settings: Settings, url: str | None = None) -> AsyncEngine:
    """Create the async engine. Connections default to UTC."""
    return create_async_engine(
        url or settings.sqlalchemy_url(),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # App tables are always schema-qualified; pin search_path so the
            # role name can never change which schema unqualified SQL hits.
            "options": "-c timezone=UTC -c search_path=public",
            "application_name": settings.app_name,
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def current_revision(engine: AsyncEngine) -> str | None:
    """Return the applied Alembic revision, or None if migrations never ran."""
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT to_regclass(:name)"), {"name": f"{APP_SCHEMA}.alembic_version"}
        )
        if exists is None:
            return None
        # APP_SCHEMA is a module constant, not user input.
        query = text(f"SELECT version_num FROM {APP_SCHEMA}.alembic_version")  # noqa: S608
        result = await conn.scalar(query)
        return str(result) if result is not None else None


def expected_head_revision() -> str | None:
    """Return the head revision from the migration scripts shipped with the app."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not ALEMBIC_INI.exists():
        return None
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()
