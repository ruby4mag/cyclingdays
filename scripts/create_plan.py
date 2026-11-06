"""Create the athlete's long-term training plan (Phase 7).

Usage::

    python -m scripts.create_plan --weekly-hours 6-9                 # open-ended
    python -m scripts.create_plan --weekly-hours 6-9 --event-date 2027-08-20
    python -m scripts.create_plan --weekly-hours 6-9 --dry-run       # show, store nothing
    python -m scripts.create_plan --weekly-hours 6-9 --replace       # archive the active plan

The plan is deterministic (``app/training/plan_builder.py``): 4-week blocks
(3 load weeks + 1 recovery week), a weekly template of day roles, and weekly
TSS and time targets sized to the weekly hours. With an event date it ends
with peak and taper weeks. Weekly hours are also stored in the athlete's
``availability``. JSON on stdout. Exit codes: 0 ok, 1 error.
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
from app.logging_config import configure_logging
from app.services.training_plans import Goal, PlanExistsError, create_plan
from app.training.plan_builder import PlanBuildError


def _hours(value: str) -> tuple[float, float]:
    try:
        lo, _, hi = value.partition("-")
        return float(lo), float(hi or lo)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use e.g. 6-9") from exc


async def create(
    settings: Settings,
    slug: str,
    *,
    hours: tuple[float, float],
    event_date: dt.date | None,
    start: dt.date | None,
    replace: bool,
    dry_run: bool,
) -> dict[str, Any]:
    engine = create_db_engine(settings)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session, session.begin():
            athlete = await AthleteProfileRepository(session).get_by_slug(slug)
            if athlete is None:
                return {"ok": False, "error": f"athlete {slug!r} not found"}
            today = dt.datetime.now(ZoneInfo(athlete.timezone)).date()
            goal = Goal(weekly_hours=hours, event_date=event_date, start=start)
            try:
                return await create_plan(
                    session, athlete, goal, today=today, replace=replace, dry_run=dry_run
                )
            except PlanExistsError as exc:
                return {"ok": False, "error": f"{exc} (use --replace)"}
            except PlanBuildError as exc:
                return {"ok": False, "error": str(exc)}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None)
    parser.add_argument("--weekly-hours", type=_hours, required=True, help="e.g. 6-9")
    parser.add_argument("--event-date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    slug = args.athlete_slug or AthleteSeedSettings().slug
    result = asyncio.run(
        create(
            settings,
            slug,
            hours=args.weekly_hours,
            event_date=args.event_date,
            start=args.start,
            replace=args.replace,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
