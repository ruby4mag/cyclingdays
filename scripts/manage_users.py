"""Manage web-login users (``coach.app_users``).

Usage (inside the app container: ``podman exec -it cycling-coach_app_1 ...``):

    python -m scripts.manage_users add <username> [--email a@b.c]   # asks for the password
    python -m scripts.manage_users email <username> <address|->     # "-" clears it
    python -m scripts.manage_users link <username> <athlete-slug>   # existing athlete
    python -m scripts.manage_users passwd <username>     # also ends their sessions
    python -m scripts.manage_users disable <username>    # ends their sessions
    python -m scripts.manage_users enable <username>
    python -m scripts.manage_users delete <username>
    python -m scripts.manage_users list

Phase 10: a new user signs in, connects Intervals.icu and sets a goal on
the Account page; that creates their athlete. ``link`` is only for
athletes that already exist (e.g. the original single-user athlete).

``--password-stdin`` reads the password from standard input instead of
prompting (for scripts). Passwords are never accepted as arguments, so they
don't end up in shell history or the process list.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys

from app.auth import validate_password
from app.config import get_settings
from app.database.connection import create_db_engine, create_session_factory, session_scope
from app.services import users


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return validate_password(sys.stdin.readline().rstrip("\r\n"))
    first = getpass.getpass("New password: ")
    validate_password(first)
    if getpass.getpass("Repeat password: ") != first:
        raise ValueError("passwords don't match")
    return first


async def run(args: argparse.Namespace) -> object:
    password = _read_password(args.password_stdin) if args.command in ("add", "passwd") else None
    engine = create_db_engine(get_settings())
    try:
        async with session_scope(create_session_factory(engine)) as session:
            if args.command == "list":
                return await users.list_users(session)
            name = args.username
            if args.command == "add":
                assert password is not None
                record = await users.create_user(session, name, password, args.email)
                return {"created": record.username}
            if args.command == "email":
                address = None if args.address == "-" else args.address
                await users.set_email(session, name, address)
                return {"email_set": name, "email": address}
            if args.command == "link":
                await users.link_athlete(session, name, args.athlete_slug)
                return {"linked": name, "athlete": args.athlete_slug}
            if args.command == "passwd":
                assert password is not None
                await users.set_password(session, name, password)
                return {"password_changed": name}
            if args.command in ("enable", "disable"):
                await users.set_active(session, name, args.command == "enable")
                return {args.command + "d": name}
            await users.delete_user(session, name)
            return {"deleted": name}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.manage_users")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "passwd"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password-stdin", action="store_true")
        if name == "add":
            p.add_argument("--email", default=None)
    p = sub.add_parser("email")
    p.add_argument("username")
    p.add_argument("address", help='email address, or "-" to clear it')
    p = sub.add_parser("link")
    p.add_argument("username")
    p.add_argument("athlete_slug")
    for name in ("enable", "disable", "delete"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except (ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
