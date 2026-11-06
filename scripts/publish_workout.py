"""Publish (or verify) the planned workout of one day on Intervals.icu.

Usage::

    python -m scripts.publish_workout                     # tomorrow (athlete timezone)
    python -m scripts.publish_workout --date 2026-11-06
    python -m scripts.publish_workout --dry-run           # show the event payload, write nothing
    python -m scripts.publish_workout --verify            # retrieve and verify the published event

The day must have been planned (``scripts.plan_day``). Publishing is
idempotent: repeating it never creates a second event. JSON on stdout, logs on
stderr. Exit codes: 0 ok, 4 blocked or failed, 1 error.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from typing import Any
from zoneinfo import ZoneInfo

from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.database.repositories import AthleteProfileRepository
from app.integrations.intervals_client import IntervalsClient
from app.logging_config import configure_logging
from app.services.workout_publisher import WorkoutPublisher


async def run(
    settings: Settings, slug: str, day: dt.date | None, *, dry_run: bool, verify: bool
) -> dict[str, Any]:
    engine = create_db_engine(settings)
    try:
        sessions = create_session_factory(engine)
        if day is None:
            async with sessions() as session:
                athlete = await AthleteProfileRepository(session).get_by_slug(slug)
            tz = ZoneInfo(athlete.timezone if athlete else settings.app_timezone)
            day = dt.datetime.now(tz).date() + dt.timedelta(days=1)
        async with IntervalsClient.from_settings(settings) as client:
            publisher = WorkoutPublisher(settings, sessions, client)
            if verify:
                result = await publisher.verify(slug, day)
            else:
                result = await publisher.publish(slug, day, dry_run=dry_run)
        return result.as_dict()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None)
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    slug = args.athlete_slug or AthleteSeedSettings().slug
    result = asyncio.run(run(settings, slug, args.date, dry_run=args.dry_run, verify=args.verify))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    sys.exit(main())
