"""Web-login users in ``coach.app_users`` (see ``app.auth``)."""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import UserRecord, hash_password, validate_password, validate_username
from app.database.models import AppUser, AthleteProfile


def _record(user: AppUser, athlete_slug: str | None = None) -> UserRecord:
    return UserRecord(
        user.id,
        user.username,
        user.password_hash,
        user.is_active,
        athlete_id=user.athlete_id,
        athlete_slug=athlete_slug,
    )


# Each user with their athlete's slug (None before onboarding).
_WITH_SLUG = select(AppUser, AthleteProfile.slug).outerjoin(
    AthleteProfile, AthleteProfile.id == AppUser.athlete_id
)


class SqlUserStore:
    """The ``UserStore`` the app uses at runtime."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def get_by_username(self, username: str) -> UserRecord | None:
        async with self._factory() as session:
            row = (await session.execute(_WITH_SLUG.where(AppUser.username == username))).first()
            return _record(row[0], row[1]) if row else None

    async def get_by_id(self, user_id: int) -> UserRecord | None:
        async with self._factory() as session:
            row = (await session.execute(_WITH_SLUG.where(AppUser.id == user_id))).first()
            return _record(row[0], row[1]) if row else None

    async def record_login(self, user_id: int) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                update(AppUser)
                .where(AppUser.id == user_id)
                .values(last_login_at=dt.datetime.now(dt.UTC))
            )


class UserExistsError(ValueError):
    pass


class UserNotFoundError(LookupError):
    pass


# --- management (scripts/manage_users.py) ------------------------------------


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(email: str | None) -> str | None:
    if email is None or not email.strip():
        return None
    value = email.strip()
    if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
        raise ValueError(f"not an email address: {value!r}")
    return value


async def create_user(
    session: AsyncSession, username: str, password: str, email: str | None = None
) -> UserRecord:
    name = validate_username(username)
    if await session.scalar(select(AppUser.id).where(AppUser.username == name)):
        raise UserExistsError(f"user {name!r} already exists")
    user = AppUser(
        username=name,
        password_hash=hash_password(validate_password(password)),
        email=validate_email(email),
    )
    session.add(user)
    await session.flush()
    return _record(user)


async def _get(session: AsyncSession, username: str) -> AppUser:
    user = await session.scalar(
        select(AppUser).where(AppUser.username == validate_username(username))
    )
    if user is None:
        raise UserNotFoundError(f"no user {username!r}")
    return user


async def set_password(session: AsyncSession, username: str, password: str) -> None:
    """Also ends every existing session of the user."""
    user = await _get(session, username)
    user.password_hash = hash_password(validate_password(password))


async def set_active(session: AsyncSession, username: str, active: bool) -> None:
    user = await _get(session, username)
    user.is_active = active


async def set_email(session: AsyncSession, username: str, email: str | None) -> None:
    user = await _get(session, username)
    user.email = validate_email(email)


async def link_athlete(session: AsyncSession, username: str, athlete_slug: str) -> None:
    """Make ``username`` the coach user of an existing athlete (admin only)."""
    user = await _get(session, username)
    athlete_id = await session.scalar(
        select(AthleteProfile.id).where(AthleteProfile.slug == athlete_slug)
    )
    if athlete_id is None:
        raise UserNotFoundError(f"no athlete {athlete_slug!r}")
    owner = await session.scalar(
        select(AppUser.username).where(AppUser.athlete_id == athlete_id, AppUser.id != user.id)
    )
    if owner is not None:
        raise UserExistsError(f"athlete {athlete_slug!r} already belongs to user {owner!r}")
    user.athlete_id = athlete_id


async def delete_user(session: AsyncSession, username: str) -> None:
    await session.delete(await _get(session, username))


async def list_users(session: AsyncSession) -> list[dict[str, object]]:
    rows = (await session.execute(_WITH_SLUG.order_by(AppUser.username))).all()
    return [
        {
            "username": u.username,
            "active": u.is_active,
            "athlete": slug,
            "email": u.email,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat(),
        }
        for u, slug in rows
    ]
