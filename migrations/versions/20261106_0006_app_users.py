"""app users

Web login: ``coach.app_users`` holds the people allowed to sign in to the
dashboard (username + scrypt password hash). Managed with
``python -m scripts.manage_users``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 00:30:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('app_users',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('username', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("username ~ '^[a-z0-9._-]{1,64}$'", name=op.f('ck_app_users_username_format')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_users')),
    sa.UniqueConstraint('username', name=op.f('uq_app_users_username')),
    schema='coach'
    )
    op.execute(
        "CREATE TRIGGER trg_app_users_updated_at BEFORE UPDATE ON coach.app_users "
        "FOR EACH ROW EXECUTE FUNCTION coach.set_updated_at()"
    )


def downgrade() -> None:
    op.drop_table('app_users', schema='coach')
