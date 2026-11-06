#!/bin/sh
# Container entrypoint: wait for PostgreSQL, apply migrations, start the app.
set -eu

python -m scripts.wait_for_db

if [ "${RUN_MIGRATIONS_ON_START:-true}" = "true" ]; then
    echo "Applying database migrations..."
    alembic upgrade head
fi

exec "$@"
