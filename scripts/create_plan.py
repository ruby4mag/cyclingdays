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
from app.database.repositories import AthleteProfileRepository, TrainingPlanRepository
from app.logging_config import configure_logging
from app.training.plan_builder import PlanBuildError, build_plan


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
            plans = TrainingPlanRepository(session)
            existing = await plans.get_active(athlete.id)
            if existing is not None and not replace and not dry_run:
                return {
                    "ok": False,
                    "error": f"active plan {existing.name!r} exists; use --replace",
                }
            try:
                phases = build_plan(start=start or today, weekly_hours=hours, event_date=event_date)
            except PlanBuildError as exc:
                return {"ok": False, "error": str(exc)}
            lo, hi = hours
            name = (
                f"{athlete.primary_objective or 'Event'} on {event_date.isoformat()}"
                if event_date
                else f"Open-ended endurance plan ({lo:g}-{hi:g} h/week)"
            )
            first = min(p.start_date for p in phases.phases)
            last = max(p.end_date for p in phases.phases)
            out: dict[str, Any] = {
                "ok": True,
                "dry_run": dry_run,
                "name": name,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
                "week_template": phases.week_template,
                "weeks": [
                    {
                        "start": p.start_date.isoformat(),
                        "name": p.name,
                        "type": p.type,
                        "key_session": p.key_session,
                        "tss": p.weekly_tss_target,
                        "minutes": p.weekly_minutes_target,
                    }
                    for p in phases.phases
                ],
            }
            if dry_run:
                return out
            row = await plans.activate(
                athlete.id,
                name=name,
                objective=athlete.primary_objective or "endurance",
                start_date=first,
                end_date=last,
                phases=phases.model_dump(mode="json"),
            )
            athlete.availability = {**(athlete.availability or {}), "weekly_hours": [lo, hi]}
            out["plan_id"] = row.id
            out["replaced"] = None if existing is None else existing.name
            return out
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
