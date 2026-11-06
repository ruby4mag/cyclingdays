"""Recompute training metrics and print the readiness assessment.

Usage::

    python -m scripts.training_report                    # today (athlete's timezone)
    python -m scripts.training_report --date 2026-11-05
    python -m scripts.training_report --no-store         # compute only, write nothing
    python -m scripts.training_report --sync             # Intervals.icu sync first

Prints a JSON report on stdout (logs go to stderr). Exit code 0 on success,
1 on error (including a failed sync when --sync is given).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from typing import Any

from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.logging_config import configure_logging
from app.services import connections
from app.services.intervals_sync import IntervalsSyncService
from app.services.training_metrics import TrainingMetricsError, TrainingMetricsService


async def report(
    settings: Settings,
    slug: str,
    day: dt.date | None,
    *,
    store: bool,
    sync: bool,
    database_url: str | None = None,
) -> dict[str, Any]:
    engine = create_db_engine(settings, url=database_url)
    try:
        factory = create_session_factory(engine)
        result: dict[str, Any] = {}
        if sync:
            async with await connections.open_client(settings, factory, slug) as client:
                sync_report = await IntervalsSyncService(factory, client, settings).run(slug)
            result["sync_ok"] = sync_report.ok
        training = await TrainingMetricsService(factory).run(slug, day=day, store=store)
        result.update(training.as_dict())
        return result
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None)
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--no-store", action="store_true")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    slug = args.athlete_slug or AthleteSeedSettings().slug
    try:
        result = asyncio.run(
            report(settings, slug, args.date, store=not args.no_store, sync=args.sync)
        )
    except TrainingMetricsError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("sync_ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
