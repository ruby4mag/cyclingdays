"""Alembic environment.

* Uses the synchronous psycopg 3 driver (same URL as the app).
* The version table lives in the ``coach`` schema alongside application tables.
* Only objects in the ``coach`` schema are considered by autogenerate, so
  LangGraph's checkpoint tables (``langgraph`` schema) are never touched.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.database.models import APP_SCHEMA, Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Tests and tooling may inject a URL explicitly.
    url = config.attributes.get("database_url") or config.get_main_option("sqlalchemy.url")
    if url:
        return str(url)
    from app.config import get_settings

    return get_settings().sqlalchemy_url()


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    if type_ == "table":
        return bool(obj.schema == APP_SCHEMA)
    return True


def _include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "schema":
        return name == APP_SCHEMA
    return True


_CONTEXT_OPTS: dict[str, Any] = {
    "target_metadata": target_metadata,
    "version_table_schema": APP_SCHEMA,
    "include_schemas": True,
    "include_object": _include_object,
    "include_name": _include_name,
    "compare_type": True,
    "compare_server_default": True,
}


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), literal_binds=True, **_CONTEXT_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Pin search_path: if the DB role is named like the app schema ("coach"),
    # PostgreSQL's default "$user" search_path would make that schema the default
    # and break autogenerate's schema filtering.
    engine = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args={"options": "-c search_path=public -c timezone=UTC"},
    )
    with engine.connect() as connection:
        # The version table lives in the app schema, so it must exist first.
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{APP_SCHEMA}"'))
        connection.commit()
        context.configure(connection=connection, **_CONTEXT_OPTS)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
