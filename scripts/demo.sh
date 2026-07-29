#!/usr/bin/env bash
# End-to-end demo: throwaway Postgres cluster -> seed -> simulated agent -> trace report.
# Creates its own cluster on port 55432 under .pgdata/ and never touches an existing instance.
set -euo pipefail

# PG18 on macOS aborts with "postmaster became multithreaded" unless the locale is set.
export LC_ALL=C LANG=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="$ROOT/.pgdata"
PORT=55432
LOG="$PGDATA/pglog/queries.log"

for bin in initdb pg_ctl psql node; do
  command -v "$bin" >/dev/null || { echo "error: '$bin' not found on PATH" >&2; exit 1; }
done
[ -d "$ROOT/node_modules/pg" ] || { echo "error: run 'npm install' first" >&2; exit 1; }

cleanup() { pg_ctl -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "==> initializing throwaway cluster at .pgdata/"
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

echo "==> starting postgres on port $PORT"
pg_ctl -D "$PGDATA" -l "$PGDATA/startup.log" start >/dev/null
for _ in $(seq 1 20); do
  pg_isready -h localhost -p $PORT >/dev/null 2>&1 && break
  sleep 0.5
done
pg_isready -h localhost -p $PORT >/dev/null || { echo "postgres failed to start:" >&2; tail -20 "$PGDATA/pglog/queries.log" >&2; exit 1; }

echo "==> seeding 4.4M rows (this is the slow part)"
psql -h localhost -p $PORT -U postgres -d postgres -q -f "$ROOT/seed.sql" 2>&1 | grep -v NOTICE || true

echo "==> running simulated agent"
: > "$LOG"
node "$ROOT/src/agent-sim.mjs"

echo
# 100 = the simulator's think-time compression factor (see src/config.mjs).
node "$ROOT/src/assemble.mjs" "$LOG" "$ROOT/out/agent-events.jsonl" 100
