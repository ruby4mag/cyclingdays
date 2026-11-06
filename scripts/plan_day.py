"""Plan one day with the LangGraph coaching workflow, then publish it.

Usage::

    python -m scripts.plan_day                       # plan tomorrow (athlete timezone) and publish
    python -m scripts.plan_day --date 2026-11-06
    python -m scripts.plan_day --sync                # sync + evaluate new rides first
    python -m scripts.plan_day --force               # new run even if the day is planned
    python -m scripts.plan_day --no-publish          # plan only

Planning uses the long-term plan's prescription for the day, adapted to the
evaluations of recent rides (Phase 7). An open-ended plan is extended
automatically. Prints the result as JSON on stdout (logs on stderr). Re-running the same day
returns the stored plan without calling the LLM again; publishing is
idempotent, so a re-run also repairs a failed or interrupted publish.
Publishing is skipped when ``INTERVALS_PUBLISH_ENABLED=false``.

Exit codes: 0 ok, 1 aborted or error, 3 fallback plan used, 4 publishing
blocked or failed.
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
from app.services.daily_planner import DailyPlanner


async def plan(
    settings: Settings,
    slug: str,
    day: dt.date | None,
    *,
    force: bool,
    sync: bool,
    publish: bool,
) -> dict[str, Any]:
    engine = create_db_engine(settings)
    try:
        planner = DailyPlanner(settings, create_session_factory(engine))
        return await planner.plan_day(slug, day, force=force, publish=publish, sync=sync)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None)
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    slug = args.athlete_slug or AthleteSeedSettings().slug
    publish = settings.intervals_publish_enabled and not args.no_publish
    result = asyncio.run(
        plan(settings, slug, args.date, force=args.force, sync=args.sync, publish=publish)
    )
    print(json.dumps(result, indent=2, default=str))
    if result.get("outcome") == "aborted":
        return 1
    if result["publish"] is not None and not result["publish"]["ok"]:
        return 4
    return 3 if result.get("outcome") == "fallback" else 0


if __name__ == "__main__":
    sys.exit(main())
