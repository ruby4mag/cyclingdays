"""multi user

Phase 10: each web user coaches one athlete.

* ``app_users``: ``athlete_id`` (unique, SET NULL when the athlete is
  deleted), ``email`` and ``notify_daily_summary``.
* ``intervals_connections``: one per athlete, the encrypted Intervals.icu API
  key. Existing data isn't touched; link users and store credentials with
  ``scripts.manage_users link`` and ``scripts.connect_intervals``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06 12:00:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_users", sa.Column("athlete_id", sa.BigInteger(), nullable=True), schema="coach"
    )
    op.add_column("app_users", sa.Column("email", sa.Text(), nullable=True), schema="coach")
    op.add_column(
        "app_users",
        sa.Column(
            "notify_daily_summary", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        schema="coach",
    )
    op.create_foreign_key(
        op.f("fk_app_users_athlete_id_athlete_profile"),
        "app_users",
        "athlete_profile",
        ["athlete_id"],
        ["id"],
        source_schema="coach",
        referent_schema="coach",
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_app_users_athlete_id"), "app_users", ["athlete_id"], schema="coach"
    )
    op.create_check_constraint(
        op.f("ck_app_users_email_format"),
        "app_users",
        "email IS NULL OR email ~ '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$'",
        schema="coach",
    )

    op.create_table(
        "intervals_connections",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("athlete_id", sa.BigInteger(), nullable=False),
        sa.Column("intervals_athlete_id", sa.Text(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'invalid')", name=op.f("ck_intervals_connections_status")
        ),
        sa.ForeignKeyConstraint(
            ["athlete_id"],
            ["coach.athlete_profile.id"],
            name=op.f("fk_intervals_connections_athlete_id_athlete_profile"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intervals_connections")),
        sa.UniqueConstraint("athlete_id", name=op.f("uq_intervals_connections_athlete_id")),
        sa.UniqueConstraint(
            "intervals_athlete_id", name=op.f("uq_intervals_connections_intervals_athlete_id")
        ),
        schema="coach",
    )
    op.execute(
        "CREATE TRIGGER trg_intervals_connections_updated_at BEFORE UPDATE ON coach.intervals_connections "
        "FOR EACH ROW EXECUTE FUNCTION coach.set_updated_at()"
    )


def downgrade() -> None:
    op.drop_table("intervals_connections", schema="coach")
    op.drop_constraint(
        op.f("ck_app_users_email_format"), "app_users", schema="coach", type_="check"
    )
    op.drop_constraint(op.f("uq_app_users_athlete_id"), "app_users", schema="coach", type_="unique")
    op.drop_constraint(
        op.f("fk_app_users_athlete_id_athlete_profile"),
        "app_users",
        schema="coach",
        type_="foreignkey",
    )
    op.drop_column("app_users", "notify_daily_summary", schema="coach")
    op.drop_column("app_users", "email", schema="coach")
    op.drop_column("app_users", "athlete_id", schema="coach")
