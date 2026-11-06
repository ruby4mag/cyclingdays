from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.config import Settings
from app.database.models import APP_SCHEMA, LANGGRAPH_SCHEMA, Base
from tests.conftest import alembic_config, drop_database, recreate_database
from tests.unit.test_models import REQUIRED_TABLES

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch_db(integration_settings: Settings) -> Iterator[str]:
    """A private database for tests that move the schema up and down."""
    name = f"{integration_settings.postgres_db}_test_migrations"
    url = recreate_database(integration_settings, name)
    yield url
    drop_database(integration_settings, name)


def _sync_engine(url: str):  # type: ignore[no-untyped-def]
    return create_engine(url, connect_args={"options": "-c search_path=public"})


def test_upgrade_creates_all_tables_in_app_schema(scratch_db: str) -> None:
    command.upgrade(alembic_config(scratch_db), "head")
    engine = _sync_engine(scratch_db)
    try:
        insp = inspect(engine)
        assert set(insp.get_table_names(schema=APP_SCHEMA)) == REQUIRED_TABLES | {"alembic_version"}
        assert insp.get_table_names(schema="public") == []
        assert LANGGRAPH_SCHEMA in insp.get_schema_names()
        assert insp.get_table_names(schema=LANGGRAPH_SCHEMA) == []
    finally:
        engine.dispose()


def test_models_and_migrations_have_no_drift(scratch_db: str) -> None:
    command.upgrade(alembic_config(scratch_db), "head")
    engine = _sync_engine(scratch_db)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={
                    "version_table_schema": APP_SCHEMA,
                    "include_schemas": True,
                    "include_name": lambda name, type_, _p: type_ != "schema" or name == APP_SCHEMA,
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            diff = compare_metadata(ctx, Base.metadata)
        assert diff == [], diff
    finally:
        engine.dispose()


def test_downgrade_to_base_and_upgrade_again(scratch_db: str) -> None:
    cfg = alembic_config(scratch_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = _sync_engine(scratch_db)
    try:
        insp = inspect(engine)
        assert set(insp.get_table_names(schema=APP_SCHEMA)) == {"alembic_version"}
        assert LANGGRAPH_SCHEMA not in insp.get_schema_names()
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
    engine = _sync_engine(scratch_db)
    try:
        with engine.connect() as conn:
            rev = conn.execute(text(f"SELECT version_num FROM {APP_SCHEMA}.alembic_version"))
            assert rev.scalar_one() == "0006"
    finally:
        engine.dispose()


def test_downgrade_refuses_to_drop_nonempty_langgraph_schema(scratch_db: str) -> None:
    cfg = alembic_config(scratch_db)
    command.upgrade(cfg, "head")
    engine = _sync_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE {LANGGRAPH_SCHEMA}.checkpoints (id int)"))
    finally:
        engine.dispose()
    with pytest.raises(Exception, match="depend"):
        command.downgrade(cfg, "base")
