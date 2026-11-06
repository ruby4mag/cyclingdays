"""Check which synchronization directions around Intervals.icu actually work.

Usage::

    python -m scripts.check_sync                     # evaluate stored data (last 28 days)
    python -m scripts.check_sync --sync              # sync from Intervals.icu first
    python -m scripts.check_sync --days 60
    python -m scripts.check_sync --confirm intervals_to_ictrainer.planned \\
        --note "workout listed in icTrainer on 2026-11-06"

Each check reports one status: verified, confirmed, configured,
not_observed, not_configured or attention. See docs/SYNC.md. JSON on stdout,
logs on stderr. Exit codes: 0 ok, 4 if any check needs attention, 1 on error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.config import AthleteSeedSettings, Settings, get_settings
from app.database.connection import create_db_engine, create_session_factory
from app.integrations.intervals_client import IntervalsClient
from app.logging_config import configure_logging
from app.services.intervals_sync import IntervalsSyncService
from app.services.sync_verification import CONFIRMABLE, SyncVerifier


async def run(
    settings: Settings,
    slug: str,
    *,
    days: int,
    sync_first: bool,
    confirm: str | None,
    note: str,
) -> dict[str, Any]:
    engine = create_db_engine(settings)
    try:
        sessions = create_session_factory(engine)
        out: dict[str, Any] = {}
        if sync_first:
            async with IntervalsClient.from_settings(settings) as client:
                sync_report = await IntervalsSyncService(sessions, client, settings).run(slug)
            out["sync_ok"] = sync_report.as_dict()["ok"]
        verifier = SyncVerifier(sessions)
        if confirm:
            await verifier.confirm(slug, confirm, note)
            out["confirmed"] = confirm
        report = await verifier.run(slug, window_days=days)
        return {**out, **report.as_dict()}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--athlete-slug", default=None)
    parser.add_argument("--days", type=int, default=28, help="look-back window (default 28)")
    parser.add_argument("--sync", action="store_true", help="sync from Intervals.icu first")
    parser.add_argument("--confirm", choices=CONFIRMABLE, default=None)
    parser.add_argument("--note", default="", help="what you observed (with --confirm)")
    args = parser.parse_args(argv)
    if args.confirm and not args.note.strip():
        parser.error("--confirm needs --note describing what you observed")
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    slug = args.athlete_slug or AthleteSeedSettings().slug
    result = asyncio.run(
        run(
            settings,
            slug,
            days=max(1, args.days),
            sync_first=args.sync,
            confirm=args.confirm,
            note=args.note,
        )
    )
    print(json.dumps(result, indent=2, default=str))
    if result.get("sync_ok") is False:
        return 1
    return 4 if any(c["status"] == "attention" for c in result["checks"]) else 0


if __name__ == "__main__":
    sys.exit(main())
