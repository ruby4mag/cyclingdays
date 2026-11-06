"""Web-login users in ``coach.app_users`` (see ``app.auth``)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import UserRecord, hash_password, validate_password, validate_username
from app.database.models import AppUser


def _record(user: AppUser) -> UserRecord:
    return UserRecord(user.id, user.username, user.password_hash, user.is_active)


class SqlUserStore:
    """The ``UserStore`` the app uses at runtime."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def get_by_username(self, username: str) -> UserRecord | None:
        async with self._factory() as session:
            user = await session.scalar(select(AppUser).where(AppUser.username == username))
            return _record(user) if user else None

    async def get_by_id(self, user_id: int) -> UserRecord | None:
        async with self._factory() as session:
            user = await session.get(AppUser, user_id)
            return _record(user) if user else None

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


async def create_user(session: AsyncSession, username: str, password: str) -> UserRecord:
    name = validate_username(username)
    if await session.scalar(select(AppUser.id).where(AppUser.username == name)):
        raise UserExistsError(f"user {name!r} already exists")
    user = AppUser(username=name, password_hash=hash_password(validate_password(password)))
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


async def delete_user(session: AsyncSession, username: str) -> None:
    await session.delete(await _get(session, username))


async def list_users(session: AsyncSession) -> list[dict[str, object]]:
    users = (await session.scalars(select(AppUser).order_by(AppUser.username))).all()
    return [
        {
            "username": u.username,
            "active": u.is_active,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]
