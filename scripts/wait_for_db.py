"""Block until PostgreSQL accepts connections (or give up).

Usage: python -m scripts.wait_for_db [--timeout SECONDS]
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg

from app.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    settings = get_settings()
    # psycopg wants a libpq URL, not the SQLAlchemy dialect prefix.
    url = settings.sqlalchemy_url().replace("postgresql+psycopg://", "postgresql://", 1)
    deadline = time.monotonic() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            print(f"Database is ready (attempt {attempt}).")
            return 0
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                print(f"Database not ready after {args.timeout}s: {exc}", file=sys.stderr)
                return 1
            time.sleep(min(2.0, 0.25 * attempt))


if __name__ == "__main__":
    sys.exit(main())
