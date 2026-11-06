"""Synchronize data from Intervals.icu into the database.

Usage::

    python -m scripts.sync_intervals                 # incremental, all resources
    python -m scripts.sync_intervals --full          # re-fetch INTERVALS_HISTORY_DAYS
    python -m scripts.sync_intervals --resource activities --resource wellness
    python -m scripts.sync_intervals --status        # show sync state, no API calls

Prints a JSON report. Exit code 0 = all resources OK, 1 = at least one
failed, 2 = configuration error. Scheduling is added in Phase 7.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, cast

from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.database.repositories import AthleteProfileRepository, SynchronizationStateRepository
from app.integrations.intervals_client import IntervalsClient, IntervalsConfigError
from app.logging_config import configure_logging
from app.services.intervals_sync import ALL_RESOURCES, IntervalsSyncService, Resource


async def sync(
    settings: Settings,
    athlete_slug: str,
    resources: list[Resource],
    full: bool,
    database_url: str | None = None,
) -> dict[str, Any]:
    engine = create_db_engine(settings, url=database_url)
    try:
        async with IntervalsClient.from_settings(settings) as client:
            service = IntervalsSyncService(create_session_factory(engine), client, settings)
            report = await service.run(athlete_slug, resources=resources, full=full)
            return report.as_dict()
    finally:
        await engine.dispose()


async def status(
    settings: Settings, athlete_slug: str, database_url: str | None = None
) -> dict[str, Any]:
    engine = create_db_engine(settings, url=database_url)
    try:
        async with create_session_factory(engine)() as session:
            athlete = await AthleteProfileRepository(session).get_by_slug(athlete_slug)
            if athlete is None:
                return {"error": f"athlete profile {athlete_slug!r} not found"}
            states = await SynchronizationStateRepository(session).list_for_athlete(athlete.id)
            return {
                "athlete_slug": athlete_slug,
                "intervals_athlete_id": athlete.intervals_athlete_id,
                "resources": [
                    {
                        "resource": s.resource,
                        "cursor": s.cursor,
                        "last_attempt_at": s.last_attempt_at,
                        "last_success_at": s.last_success_at,
                        "consecutive_failures": s.consecutive_failures,
                        "last_error": s.last_error,
                    }
                    for s in states
                ],
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None, help="default: ATHLETE_SLUG or 'primary'")
    parser.add_argument("--resource", action="append", choices=ALL_RESOURCES, dest="resources")
    parser.add_argument("--full", action="store_true", help="ignore cursors, fetch full history")
    parser.add_argument("--status", action="store_true", help="print sync state and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    slug = args.athlete_slug or AthleteSeedSettings().slug
    if args.status:
        print(json.dumps(asyncio.run(status(settings, slug)), indent=2, default=str))
        return 0
    resources = cast(list[Resource], args.resources or list(ALL_RESOURCES))
    try:
        report = asyncio.run(sync(settings, slug, resources, args.full))
    except IntervalsConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
