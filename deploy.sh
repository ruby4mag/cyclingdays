 #!/usr/bin/env bash
  set -euo pipefail
  cd /opt/cycledays

  # --- checks ---------------------------------------------------------------
  [ -f .env ]       || { echo "missing .env"; exit 1; }
  [ -f coach.dump ] || { echo "missing coach.dump"; exit 1; }
  chmod 600 .env coach.dump
  env_val() { grep "^$1=" .env | cut -d= -f2-; }   # .env has spaces: don't 'source' it
  DB=$(env_val POSTGRES_DB); U=$(env_val POSTGRES_USER)
  [ -n "$DB" ] && [ -n "$U" ] || { echo "POSTGRES_DB/POSTGRES_USER not set in .env"; exit 1; }
  db_container() { podman ps -q --filter label=com.docker.compose.project=cycling-coach \
                                --filter label=com.docker.compose.service=db; }

  # --- 1. database container ------------------------------------------------
  podman-compose up -d db
  echo -n "waiting for PostgreSQL"
  until podman exec "$(db_container)" pg_isready -U "$U" -d "$DB" >/dev/null 2>&1; do echo -n .; sleep 2; done; echo

  # --- 2. restore the data (only into an empty database) --------------------
  if podman exec "$(db_container)" psql -U "$U" -d "$DB" -tAc \
       "select 1 from information_schema.schemata where schema_name='coach'" | grep -q 1; then
    echo "schema 'coach' already exists: skipping the restore (no overwrite)"
  else
    podman exec -i "$(db_container)" pg_restore -U "$U" -d "$DB" \
      --no-owner --exit-on-error --single-transaction < coach.dump
    echo "restore done"
  fi

  # --- 3. application (runs the migrations on start) -------------------------
  podman-compose build app
  podman-compose up -d app
  echo -n "waiting for the app"
  until curl -fs http://127.0.0.1:8010/health/ready >/dev/null; do echo -n .; sleep 3; done; echo
  curl -s http://127.0.0.1:8010/health/ready; echo

  # --- 4. quick data check ------------------------------------------------------
  podman exec "$(db_container)" psql -U "$U" -d "$DB" -c \
    "select workout_date, status, intervals_event_id from coach.planned_workouts order by workout_date desc limit 5;"

  echo
  echo "OK. Check the dashboard, then start the scheduler:"
  echo "  podman-compose up -d scheduler && podman logs -f \$(podman ps -q --filter label=com.docker.compose.service=scheduler)"
 until podman exec "$(db_container)" pg_isready -U "$U" -d "$DB" >/dev/null 2>&1; do echo -n .; sleep 2; done; echo

  # --- 2. restore the data (only into an empty database) --------------------
  if podman exec "$(db_container)" psql -U "$U" -d "$DB" -tAc \
       "select 1 from information_schema.schemata where schema_name='coach'" | grep -q 1; then
    echo "schema 'coach' already exists: skipping the restore (no overwrite)"
  else
    podman exec -i "$(db_container)" pg_restore -U "$U" -d "$DB" \
      --no-owner --exit-on-error --single-transaction < coach.dump
    echo "restore done"
  fi

  # --- 3. application (runs the migrations on start) -------------------------
  podman-compose build app
  podman-compose up -d app
  echo -n "waiting for the app"
  until curl -fs http://127.0.0.1:8010/health/ready >/dev/null; do echo -n .; sleep 3; done; echo
  curl -s http://127.0.0.1:8010/health/ready; echo

  # --- 4. quick data check ------------------------------------------------------
  podman exec "$(db_container)" psql -U "$U" -d "$DB" -c \
    "select workout_date, status, intervals_event_id from coach.planned_workouts order by workout_date desc limit 5;"

  echo
  echo "OK. Check the dashboard, then start the scheduler:"
  echo "  podman-compose up -d scheduler && podman logs -f \$(podman ps -q --filter label=com.docker.compose.service=scheduler)"
                                           
