"""Seed the initial athlete profile (idempotent).

Creates the profile identified by ATHLETE_SLUG if it does not exist. If it
already exists, nothing is changed: the database profile is authoritative
after the first seed (FTP, weight etc. are updated through the application).

Usage: python -m scripts.seed_athlete_profile
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys

from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory, session_scope
from app.database.repositories import AthleteProfileRepository


async def seed(
    settings: Settings, seed_values: AthleteSeedSettings, database_url: str | None = None
) -> dict[str, object]:
    engine = create_db_engine(settings, url=database_url)
    try:
        async with session_scope(create_session_factory(engine)) as session:
            repo = AthleteProfileRepository(session)
            existing = await repo.get_by_slug(seed_values.slug)
            if existing is not None:
                return {"created": False, "athlete_id": existing.id, "slug": existing.slug}
            values = seed_values.model_dump()
            profile = await repo.create(**values, ftp_updated_at=dt.datetime.now(dt.UTC))
            return {"created": True, "athlete_id": profile.id, "slug": profile.slug}
    finally:
        await engine.dispose()


def main() -> int:
    result = asyncio.run(seed(get_settings(), AthleteSeedSettings()))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
