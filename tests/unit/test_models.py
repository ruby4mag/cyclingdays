"""Static checks on the ORM metadata (no database required)."""

from __future__ import annotations

from sqlalchemy import Index, UniqueConstraint

from app.database.connection import expected_head_revision
from app.database.models import APP_SCHEMA, Base

REQUIRED_TABLES = {
    "athlete_profile",
    "activities",
    "daily_recovery",
    "training_plans",
    "planned_workouts",
    "workout_performance",
    "training_metrics",
    "coach_decisions",
    "synchronization_state",
    "webhook_events",
    "publishing_audit",
    "sync_checks",
    "calendar_events",  # Phase 2: mirror of the Intervals.icu calendar
    "app_users",  # web login
}


def _unique_column_sets(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[f"{APP_SCHEMA}.{table_name}"]
    sets = {
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
    }
    sets |= {
        tuple(c.name for c in idx.columns)
        for idx in table.indexes
        if isinstance(idx, Index) and idx.unique
    }
    return sets


def test_all_required_tables_exist_in_app_schema() -> None:
    names = {t.name for t in Base.metadata.sorted_tables}
    assert names == REQUIRED_TABLES
    assert all(t.schema == APP_SCHEMA for t in Base.metadata.sorted_tables)


def test_deduplication_keys_are_unique() -> None:
    assert ("source", "source_activity_id") in _unique_column_sets("activities")
    assert ("athlete_id", "recovery_date", "source") in _unique_column_sets("daily_recovery")
    assert ("external_id",) in _unique_column_sets("planned_workouts")
    assert ("athlete_id", "workout_date") in _unique_column_sets("planned_workouts")
    assert ("activity_id",) in _unique_column_sets("workout_performance")
    assert ("source", "dedup_key") in _unique_column_sets("webhook_events")
    assert ("athlete_id", "source", "resource") in _unique_column_sets("synchronization_state")


def test_source_payloads_use_jsonb() -> None:
    for table, column in [
        ("activities", "raw_payload"),
        ("daily_recovery", "raw_payload"),
        ("planned_workouts", "structure"),
        ("coach_decisions", "output"),
        ("webhook_events", "payload"),
    ]:
        col = Base.metadata.tables[f"{APP_SCHEMA}.{table}"].c[column]
        assert col.type.__class__.__name__ == "JSONB", (table, column)


def test_migration_head_is_discoverable() -> None:
    assert expected_head_revision() == "0006"
