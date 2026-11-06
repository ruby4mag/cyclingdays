"""Sync from Intervals.icu, evaluate new rides, mark missed days (Phase 7).

Usage::

    python -m scripts.process_activities            # sync + evaluate + sync checks
    python -m scripts.process_activities --no-sync  # evaluate stored data only

Idempotent: a ride is evaluated once per revision. JSON on stdout. Exit
codes: 0 ok, 4 if an evaluation failed or Intervals.icu was unreachable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.agents.llm import OpenAIStructuredLLM
from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.graph.workflow import make_llm
from app.logging_config import configure_logging
from app.services import connections
from app.services.activity_processor import ActivityProcessor
from app.services.daily_planner import DailyPlanner


async def run(settings: Settings, slug: str, *, sync: bool) -> dict[str, Any]:
    engine = create_db_engine(settings)
    try:
        sessions = create_session_factory(engine)
        if sync:
            return await DailyPlanner(settings, sessions).poll(slug)
        llm = make_llm(settings)
        try:
            async with await connections.open_client(settings, sessions, slug) as client:
                report = await ActivityProcessor(settings, sessions, client, llm).run(
                    slug, lookback_days=settings.activity_lookback_days
                )
        finally:
            if isinstance(llm, OpenAIStructuredLLM):
                await llm.aclose()
        return {"athlete_slug": slug, "activities": report.as_dict()}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None)
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    slug = args.athlete_slug or AthleteSeedSettings().slug
    result = asyncio.run(run(settings, slug, sync=not args.no_sync))
    print(json.dumps(result, indent=2, default=str))
    activities = result.get("activities") or {}
    return 4 if result.get("error") or activities.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
