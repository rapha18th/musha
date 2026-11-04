#!/bin/sh
# Musha's start on Cloud Run: bring the database back, seed it once, then serve.
#
# SQLite with a replica (LITESTREAM_REPLICA_URL set):
#   1. restore the database from Cloud Storage when a replica exists,
#   2. seed a fresh database (python -m musha.seed --if-empty); a restored one gains any new
#      columns as the app starts,
#   3. run the web process under Litestream, which replicates every change and syncs once more
#      when Cloud Run stops the instance.
# Postgres (DATABASE_URL=postgresql://...) or SQLite alone: seed when empty, then serve.
#
# DATABASE_URL names the database. A SQLite URL also gives Litestream its file
# (sqlite:////data/musha.db is /data/musha.db); with DATABASE_URL unset, MUSHA_DB_PATH names it.
# Every path comes from the environment, so the same script runs in the image and on a laptop.
set -eu

PORT="${PORT:-8080}"
APP_DIR="${MUSHA_APP_DIR:-/app/backend}"
PYTHON="${MUSHA_PYTHON:-python}"
LITESTREAM="${LITESTREAM_BIN:-litestream}"
LITESTREAM_CONFIG="${LITESTREAM_CONFIG:-/etc/litestream.yml}"
LITESTREAM_SOCKET="${LITESTREAM_SOCKET:-/tmp/litestream.sock}"
LITESTREAM_REPLICA_URL="${LITESTREAM_REPLICA_URL:-}"
MUSHA_DB_PATH="${MUSHA_DB_PATH:-/data/musha.db}"
DATABASE_URL="${DATABASE_URL:-}"
# Cloud Run's front end is the one proxy in front of the app, and it sets X-Forwarded-Proto.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"

case "$DATABASE_URL" in
  "") DATABASE_URL="sqlite:///$MUSHA_DB_PATH"; on_sqlite=1 ;;
  sqlite:///*) MUSHA_DB_PATH="${DATABASE_URL#sqlite:///}"; on_sqlite=1 ;;
  *) on_sqlite=0 ;;
esac
export PORT MUSHA_DB_PATH LITESTREAM_REPLICA_URL LITESTREAM_SOCKET DATABASE_URL FORWARDED_ALLOW_IPS

# uvicorn exits within 4 s of a stop, so Litestream's final sync fits inside Cloud Run's 10 s.
SERVE="$PYTHON -m uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --no-access-log --timeout-graceful-shutdown 4"

say() { echo "musha: $*" >&2; }

cd "$APP_DIR"

if [ "$on_sqlite" = 1 ] && [ -n "$LITESTREAM_REPLICA_URL" ]; then
  mkdir -p "$(dirname "$MUSHA_DB_PATH")"
  if [ -e "$MUSHA_DB_PATH" ]; then
    say "the database is on disk already; replication picks up from it"
    "$PYTHON" -m musha.seed --if-empty
  else
    # A replica that fails to restore stops the start: a fresh database would replicate
    # over the shops the replica holds.
    "$LITESTREAM" restore -config "$LITESTREAM_CONFIG" -if-db-not-exists -if-replica-exists \
      -integrity-check quick "$MUSHA_DB_PATH"
    if [ -e "$MUSHA_DB_PATH" ]; then
      # The app adds any new columns as it starts (db.prepare), so a restored shop list goes
      # straight to serving and the cold start skips a second Python start.
      say "restored the database from the replica"
    else
      say "the replica is empty; seeding a fresh database"
      "$PYTHON" -m musha.seed --if-empty
    fi
  fi
  say "serving on port $PORT and replicating to $LITESTREAM_REPLICA_URL"
  exec "$LITESTREAM" replicate -config "$LITESTREAM_CONFIG" -exec "$SERVE"
fi

if [ "$on_sqlite" = 1 ]; then
  mkdir -p "$(dirname "$MUSHA_DB_PATH")"
  say "SQLite on the instance disk: the database lasts as long as this instance"
fi
"$PYTHON" -m musha.seed --if-empty
say "serving on port $PORT"
# shellcheck disable=SC2086
exec $SERVE
