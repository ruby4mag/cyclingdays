"""Per-athlete Intervals.icu connections (Phase 10).

Each athlete has its own Intervals.icu athlete id and personal API key in
``coach.intervals_connections``. The key is encrypted with
``CREDENTIALS_ENCRYPTION_KEY`` (Fernet: AES-128-CBC + HMAC-SHA256) and is
only decrypted to build an :class:`IntervalsClient`. It never appears in
API responses or logs.

* :func:`open_client` is the one way code gets a client for an athlete. It
  raises :class:`IntervalsNotConnectedError` (an ``IntervalsError``, so the
  existing error handling applies) when the athlete has no usable key.
* :func:`verify_credentials` checks a key against ``GET /athlete/{id}``
  before it's stored.
* :func:`mark_invalid` records a 401/403 so the scheduler stops using a key
  the athlete revoked, until they reconnect.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import AthleteProfile, IntervalsConnection
from app.integrations.intervals_client import (
    IntervalsAuthError,
    IntervalsClient,
    IntervalsConfigError,
    IntervalsNotFoundError,
)
from app.integrations.intervals_mappers import athlete_summary

logger = logging.getLogger(__name__)

_MAX_ERROR_LEN = 500


class CredentialsKeyError(RuntimeError):
    """``CREDENTIALS_ENCRYPTION_KEY`` is missing or not a valid Fernet key."""


class IntervalsNotConnectedError(IntervalsConfigError):
    """The athlete has no active Intervals.icu connection."""


class InvalidCredentialsError(ValueError):
    """Intervals.icu refused the athlete id / API key pair."""


def cipher(settings: Settings) -> Fernet:
    if settings.credentials_encryption_key is None:
        raise CredentialsKeyError(
            "CREDENTIALS_ENCRYPTION_KEY is not set; generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`'
        )
    try:
        return Fernet(settings.credentials_encryption_key.get_secret_value().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialsKeyError(
            "CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)"
        ) from exc


def encrypt_key(settings: Settings, api_key: str) -> str:
    return cipher(settings).encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_key(settings: Settings, token: str) -> str:
    try:
        return cipher(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialsKeyError(
            "stored Intervals.icu key can't be decrypted: CREDENTIALS_ENCRYPTION_KEY changed?"
        ) from exc


def normalize_athlete_id(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 32 or not value.replace("_", "").isalnum():
        raise InvalidCredentialsError(
            "The athlete ID looks like 'i12345' (Settings on Intervals.icu)"
        )
    return value


def normalize_api_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 256 or not value.isprintable() or " " in value:
        raise InvalidCredentialsError("Paste the API key from Intervals.icu → Settings → Developer")
    return value


def client_for(settings: Settings, intervals_athlete_id: str, api_key: str) -> IntervalsClient:
    return IntervalsClient(
        api_key=api_key,
        athlete_id=intervals_athlete_id,
        base_url=settings.intervals_base_url,
        timeout_seconds=settings.intervals_timeout_seconds,
        max_retries=settings.intervals_max_retries,
        backoff_base_seconds=settings.intervals_backoff_base_seconds,
        backoff_max_seconds=settings.intervals_backoff_max_seconds,
    )


async def verify_credentials(
    client: IntervalsClient,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``GET /athlete/{id}`` with the key; returns (raw athlete, summary).

    Raises :class:`InvalidCredentialsError` when Intervals.icu refuses the
    key or doesn't know the athlete, or when the key belongs to another
    athlete. Other ``IntervalsError`` (network, 5xx) propagate.
    """
    try:
        athlete = await client.get_athlete()
    except IntervalsAuthError as exc:
        raise InvalidCredentialsError(
            "Intervals.icu refused this API key for that athlete ID"
        ) from exc
    except IntervalsNotFoundError as exc:
        raise InvalidCredentialsError("Intervals.icu has no athlete with that ID") from exc
    summary = athlete_summary(athlete)
    if summary["intervals_athlete_id"] != client.athlete_id:
        raise InvalidCredentialsError(
            f"The API key belongs to Intervals.icu athlete {summary['intervals_athlete_id']}, "
            f"not {client.athlete_id}"
        )
    return athlete, summary


async def get_connection(session: AsyncSession, athlete_id: int) -> IntervalsConnection | None:
    result = await session.scalar(
        select(IntervalsConnection).where(IntervalsConnection.athlete_id == athlete_id)
    )
    return result


async def store_connection(
    session: AsyncSession,
    settings: Settings,
    athlete: AthleteProfile,
    intervals_athlete_id: str,
    api_key: str,
    *,
    now: dt.datetime | None = None,
) -> IntervalsConnection:
    """Create or replace the athlete's connection (status ``active``).

    The caller has verified the key. An athlete already linked to a
    different Intervals.icu athlete is refused: mixing two people's data
    would corrupt every metric.
    """
    if athlete.intervals_athlete_id and athlete.intervals_athlete_id != intervals_athlete_id:
        raise InvalidCredentialsError(
            f"This account's data comes from Intervals.icu athlete "
            f"{athlete.intervals_athlete_id}; connect that athlete, not {intervals_athlete_id}"
        )
    taken = await session.scalar(
        select(AthleteProfile.id).where(
            AthleteProfile.intervals_athlete_id == intervals_athlete_id,
            AthleteProfile.id != athlete.id,
        )
    )
    if taken is not None:
        raise InvalidCredentialsError(
            "That Intervals.icu athlete is already connected to another account"
        )
    athlete.intervals_athlete_id = intervals_athlete_id
    token = encrypt_key(settings, api_key)
    row = await get_connection(session, athlete.id)
    if row is None:
        row = IntervalsConnection(
            athlete_id=athlete.id,
            intervals_athlete_id=intervals_athlete_id,
            api_key_encrypted=token,
        )
        session.add(row)
    row.intervals_athlete_id = intervals_athlete_id
    row.api_key_encrypted = token
    row.status = "active"
    row.last_error = None
    row.last_verified_at = now or dt.datetime.now(dt.UTC)
    await session.flush()
    logger.info(
        "intervals_connected",
        extra={"athlete": athlete.slug, "intervals_athlete_id": intervals_athlete_id},
    )
    return row


async def open_client(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], slug: str
) -> IntervalsClient:
    """A client with the athlete's own credentials (use as ``async with``)."""
    async with sessions() as session:
        row = await session.scalar(
            select(IntervalsConnection)
            .join(AthleteProfile, AthleteProfile.id == IntervalsConnection.athlete_id)
            .where(AthleteProfile.slug == slug)
        )
    if row is None:
        raise IntervalsNotConnectedError(f"athlete {slug!r} has not connected Intervals.icu")
    if row.status != "active":
        raise IntervalsNotConnectedError(
            f"athlete {slug!r}: Intervals.icu key is {row.status} ({row.last_error}); "
            "the athlete must reconnect"
        )
    return client_for(
        settings, row.intervals_athlete_id, decrypt_key(settings, row.api_key_encrypted)
    )


async def mark_invalid(sessions: async_sessionmaker[AsyncSession], slug: str, error: str) -> None:
    """Stop using a key Intervals.icu refused (401/403) until the athlete reconnects."""
    async with sessions() as session, session.begin():
        athlete_id = await session.scalar(
            select(AthleteProfile.id).where(AthleteProfile.slug == slug)
        )
        if athlete_id is None:
            return
        await session.execute(
            update(IntervalsConnection)
            .where(IntervalsConnection.athlete_id == athlete_id)
            .values(status="invalid", last_error=error[:_MAX_ERROR_LEN])
        )
    logger.warning("intervals_key_invalid", extra={"athlete": slug, "error": error})


async def connected_slugs(sessions: async_sessionmaker[AsyncSession]) -> list[str]:
    """Athletes whose connection is active, in a stable order."""
    async with sessions() as session:
        rows = await session.scalars(
            select(AthleteProfile.slug)
            .join(IntervalsConnection, IntervalsConnection.athlete_id == AthleteProfile.id)
            .where(IntervalsConnection.status == "active")
            .order_by(AthleteProfile.id)
        )
        return list(rows)
