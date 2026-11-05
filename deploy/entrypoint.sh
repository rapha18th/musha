#!/bin/sh
# Musha's start on Cloud Run: bring the database back, then serve.
#
# SQLite with a replica (LITESTREAM_REPLICA_URL set, the base: gs://<bucket>/musha):
#   1. list the generations under <base>/gen (backend/generations.py) and restore the one written
#      most recently; a restored database gains any new columns as the app starts,
#   2. run the web process under Litestream, which replicates every change to this start's own
#      generation and syncs once more when Cloud Run stops the instance.
#
# Every start writes a generation of its own, <base>/gen/<UTC start stamp>-<random>. Litestream
# allows one writer per replica path, and no two starts share one: two revisions overlapping in a
# rollout, or two instances of one revision, each write their own. The lister ranks generations by
# their last write, so a start that stopped early never hides one an older instance kept writing.
#
# An empty listing stops the start: a fresh database seeds on request alone. MUSHA_RESTORE_FROM is
# that request and the recovery (deploy.ps1 -RestoreFrom): fresh seeds a new database, and a
# generation URL restores that one. MUSHA_RESTORE_AFTER names the generation that started last when
# it was set (none: there was none). The request holds until a generation starts after that one: the
# recovery's own first start writes it, and from then on the request is spent in every revision
# that carries it. Generations that started after MUSHA_RESTORE_AFTER also rank first, so the line
# the recovery replaced stays behind.
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
MUSHA_RESTORE_FROM="${MUSHA_RESTORE_FROM:-}"
MUSHA_RESTORE_AFTER="${MUSHA_RESTORE_AFTER:-none}"
MUSHA_DB_PATH="${MUSHA_DB_PATH:-/data/musha.db}"
DATABASE_URL="${DATABASE_URL:-}"
# Cloud Run's front end is the one proxy in front of the app, and it sets X-Forwarded-Proto.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"

say() { echo "musha: $*" >&2; }

# This start's generation: its name sorts by the UTC start time, and the random part keeps two
# starts in one second apart. The lister reads this shape alone, so a name without it stops here.
BASE="${LITESTREAM_REPLICA_URL%/}"
if [ -n "$BASE" ]; then
  GENERATION="$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  case "$GENERATION" in
    *Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) say "this start's generation name came out as $GENERATION; stopping"; exit 1 ;;
  esac
  LITESTREAM_REPLICA_URL="$BASE/gen/$GENERATION"
  case "$MUSHA_RESTORE_AFTER" in
    none) ;;
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) say "MUSHA_RESTORE_AFTER reads $MUSHA_RESTORE_AFTER, and it takes a generation name or none; stopping"; exit 1 ;;
  esac
fi

case "$DATABASE_URL" in
  "") DATABASE_URL="sqlite:///$MUSHA_DB_PATH"; on_sqlite=1 ;;
  sqlite:///*) MUSHA_DB_PATH="${DATABASE_URL#sqlite:///}"; on_sqlite=1 ;;
  *) on_sqlite=0 ;;
esac
export PORT MUSHA_DB_PATH LITESTREAM_REPLICA_URL LITESTREAM_SOCKET DATABASE_URL FORWARDED_ALLOW_IPS

# uvicorn exits within 4 s of a stop, so Litestream's final sync fits inside Cloud Run's 10 s.
SERVE="$PYTHON -m uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --no-access-log --timeout-graceful-shutdown 4"

cd "$APP_DIR"

if [ "$on_sqlite" = 1 ] && [ -n "$BASE" ]; then
  mkdir -p "$(dirname "$MUSHA_DB_PATH")"
  if [ -e "$MUSHA_DB_PATH" ]; then
    # A laptop run that kept its file: that file is the newest copy. Litestream's local state
    # belongs to an older generation, so it goes, and the new generation opens with a full snapshot.
    say "the database is on disk already; generation $GENERATION starts from it"
    rm -rf "$(dirname "$MUSHA_DB_PATH")/.$(basename "$MUSHA_DB_PATH")-litestream"
    "$PYTHON" -m musha.seed --if-empty
  else
    if ! found=$("$PYTHON" -I "$APP_DIR/generations.py" "$BASE" "$MUSHA_RESTORE_AFTER"); then
      say "the generations under $BASE/gen could not be listed; stopping before the wrong database serves"
      exit 1
    fi
    recent=$(printf '%s\n' "$found" | sed -n 1p)
    source="$recent"
    if [ -n "$MUSHA_RESTORE_FROM" ]; then
      # The lister puts a generation that started after MUSHA_RESTORE_AFTER first, when there is one.
      first="${recent##*/}"
      if [ -n "$recent" ] && { [ "$MUSHA_RESTORE_AFTER" = none ] \
          || LC_ALL=C expr "$first" \> "$MUSHA_RESTORE_AFTER" > /dev/null; }; then
        say "MUSHA_RESTORE_FROM is spent: generation $first started after it was set"
      elif [ "$MUSHA_RESTORE_FROM" = fresh ]; then
        source=fresh
        say "MUSHA_RESTORE_FROM=fresh: seeding a fresh database on purpose"
      else
        source="$MUSHA_RESTORE_FROM"
        say "MUSHA_RESTORE_FROM names $source; restoring it in place of the most recent generation"
      fi
    fi
    if [ -z "$source" ]; then
      say "no generations under $BASE/gen; stopping, since a fresh database seeds on request alone (MUSHA_RESTORE_FROM=fresh, deploy.ps1 -RestoreFrom fresh)"
      exit 1
    fi
    if [ "$source" = fresh ]; then
      "$PYTHON" -m musha.seed --if-empty
    else
      if [ "$source" = "$recent" ]; then
        say "restoring the most recently written of $(printf '%s\n' "$found" | wc -l | tr -d ' ') generations, $source"
      fi
      # The source's instance may still be writing it: a file it compacts away mid-read fails the
      # restore, and the next try reads the generation as it stands then. Each try gives up after
      # 60 s, so a Cloud Storage brownout ends all three, pauses included, inside Cloud Run's 240 s
      # startup probe. A generation that never restores stops the start, so a deploy fails and the
      # old revision serves. Another generation would drop this one's shops, so the start never
      # reaches for it.
      tries=0
      until timeout 60 "$LITESTREAM" restore -o "$MUSHA_DB_PATH" -integrity-check full "$source" \
          && [ -e "$MUSHA_DB_PATH" ]; do
        tries=$((tries + 1))
        rm -f "$MUSHA_DB_PATH" "$MUSHA_DB_PATH-wal" "$MUSHA_DB_PATH-shm" "$MUSHA_DB_PATH-journal" "$MUSHA_DB_PATH.tmp"
        if [ "$tries" -ge 3 ]; then
          say "$source failed to restore three times; stopping (deploy/README.md, Recovery)"
          exit 1
        fi
        say "restore $tries of 3 failed; trying again in 2 s"
        sleep 2
      done
      # The app adds any new columns as it starts (db.prepare), so a restored shop list goes
      # straight to serving and the cold start skips a second Python start.
      say "restored the database from $source"
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
