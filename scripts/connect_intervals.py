"""Store an athlete's Intervals.icu credentials, encrypted (Phase 10, admin).

Users normally connect on the Account page. This is for the administrator:
moving the single-user ``.env`` credentials into the database, or fixing a
connection by hand.

Usage (inside the app container)::

    python -m scripts.connect_intervals --athlete-slug primary --from-env
    python -m scripts.connect_intervals --athlete-slug rider --intervals-id i12345  # asks for key
    python -m scripts.connect_intervals --athlete-slug rider --status

``--from-env`` uses ``INTERVALS_ATHLETE_ID`` / ``INTERVALS_API_KEY``. The key
is checked against Intervals.icu (``GET /athlete/{id}``) before it is
stored. Keys are never accepted as arguments (shell history, process list).
JSON on stdout. Exit codes: 0 ok, 1 refused or error, 2 not configured.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from app.config import Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.database.repositories import AthleteProfileRepository
from app.integrations.intervals_client import IntervalsError
from app.logging_config import configure_logging
from app.services import connections
from app.services.connections import CredentialsKeyError, InvalidCredentialsError


async def status(settings: Settings, slug: str) -> dict[str, Any]:
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            athlete = await AthleteProfileRepository(session).get_by_slug(slug)
            if athlete is None:
                return {"ok": False, "error": f"athlete {slug!r} not found"}
            row = await connections.get_connection(session, athlete.id)
            if row is None:
                return {"ok": True, "athlete_slug": slug, "connected": False}
            return {
                "ok": True,
                "athlete_slug": slug,
                "connected": True,
                "intervals_athlete_id": row.intervals_athlete_id,
                "status": row.status,
                "last_error": row.last_error,
                "last_verified_at": row.last_verified_at,
            }
    finally:
        await engine.dispose()


async def connect(settings: Settings, slug: str, intervals_id: str, api_key: str) -> dict[str, Any]:
    intervals_id = connections.normalize_athlete_id(intervals_id)
    api_key = connections.normalize_api_key(api_key)
    connections.cipher(settings)
    async with connections.client_for(settings, intervals_id, api_key) as client:
        await connections.verify_credentials(client)
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            athlete = await AthleteProfileRepository(session).get_by_slug(slug)
            if athlete is None:
                return {"ok": False, "error": f"athlete {slug!r} not found"}
            row = await connections.store_connection(
                session, settings, athlete, intervals_id, api_key
            )
            return {
                "ok": True,
                "athlete_slug": slug,
                "intervals_athlete_id": row.intervals_athlete_id,
                "status": row.status,
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-env", action="store_true")
    source.add_argument("--intervals-id")
    source.add_argument("--status", action="store_true")
    parser.add_argument("--key-stdin", action="store_true", help="read the API key from stdin")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    if args.status:
        result = asyncio.run(status(settings, args.athlete_slug))
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["ok"] else 1
    if args.from_env:
        if settings.intervals_api_key is None or settings.intervals_athlete_id is None:
            print(json.dumps({"ok": False, "error": "INTERVALS_* not set in .env"}))
            return 2
        intervals_id = settings.intervals_athlete_id
        api_key = settings.intervals_api_key.get_secret_value()
    else:
        intervals_id = args.intervals_id
        api_key = (
            sys.stdin.readline().strip()
            if args.key_stdin
            else getpass.getpass("Intervals.icu API key: ")
        )
    try:
        result = asyncio.run(connect(settings, args.athlete_slug, intervals_id, api_key))
    except CredentialsKeyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except (InvalidCredentialsError, IntervalsError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
