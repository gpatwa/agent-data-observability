#!/usr/bin/env bash
# End-to-end test: brings up a throwaway Postgres with statement logging,
# runs the pipeline test against it, tears it down.
#
# Separate from `npm test` on purpose — the unit tests must stay database-free
# so they run anywhere in under a second.
set -euo pipefail

export LC_ALL=C LANG=C   # PG18 on macOS aborts at startup without this

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="$ROOT/.pgdata-e2e"
PORT="${E2E_PORT:-55433}"
export E2E_LOG_PATH="$PGDATA/pglog/queries.log"
export PGPORT="$PORT"

for bin in initdb pg_ctl node; do
  command -v "$bin" >/dev/null || { echo "error: '$bin' not on PATH" >&2; exit 1; }
done

cleanup() { pg_ctl -D "$PGDATA" stop -m immediate >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "==> initializing e2e cluster"
  initdb -D "$PGDATA" -U postgres --auth=trust -E UTF8 >/dev/null
  cat >> "$PGDATA/postgresql.conf" <<EOF
port = $PORT
listen_addresses = 'localhost'
logging_collector = on
log_directory = 'pglog'
log_filename = 'queries.log'
log_statement = 'all'
log_min_duration_statement = 0
log_line_prefix = '%m [%p] '
log_rotation_size = 0
log_rotation_age = 0
EOF
fi

echo "==> starting postgres on :$PORT"
pg_ctl -D "$PGDATA" -l "$PGDATA/startup.log" start >/dev/null
for _ in $(seq 1 20); do
  pg_isready -h localhost -p "$PORT" >/dev/null 2>&1 && break
  sleep 0.5
done
pg_isready -h localhost -p "$PORT" >/dev/null || {
  echo "postgres failed to start:" >&2; tail -20 "$PGDATA/pglog/queries.log" >&2; exit 1; }

echo "==> running end-to-end pipeline test"
node --test "$ROOT/test/e2e/**/*.test.mjs"
