#!/usr/bin/env bash
# Writes the Databricks half of .env so nothing has to be exported by hand.
#
# The token is NOT taken as an argument — arguments land in shell history.
# Paste it at the prompt (input is hidden) or set DATABRICKS_TOKEN beforehand.
#
#   ./scripts/databricks-env.sh <HOST> <GENIE_SPACE_ID>
set -euo pipefail

HOST="${1:-}"
SPACE="${2:-}"
if [ -z "$HOST" ] || [ -z "$SPACE" ]; then
  cat >&2 <<USAGE
usage: ./scripts/databricks-env.sh <HOST> <GENIE_SPACE_ID>

  HOST            https://dbc-xxxx.cloud.databricks.com   (no ?o=... suffix)
  GENIE_SPACE_ID  the id in your Genie space URL, after /genie/rooms/

  Get a token: avatar (top right) -> Settings -> Developer -> Access tokens
  Get a space: Genie in the left nav -> New, pick tables from samples.tpch
USAGE
  exit 1
fi

HOST="${HOST%%\?*}"          # strip any ?o=... the browser URL carries
HOST="${HOST%/}"             # strip trailing slash
case "$HOST" in https://*) ;; *) echo "error: HOST must start with https://" >&2; exit 1;; esac

TOKEN="${DATABRICKS_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  printf 'Databricks personal access token (input hidden): ' >&2
  read -rs TOKEN
  printf '\n' >&2
fi
[ -n "$TOKEN" ] || { echo "error: no token given" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"

if [ -f "$ENV_FILE" ] && grep -q '^DATABRICKS_' "$ENV_FILE"; then
  cp "$ENV_FILE" "$ENV_FILE.bak"
  grep -v '^DATABRICKS_' "$ENV_FILE.bak" > "$ENV_FILE" || true
  echo "==> replaced existing DATABRICKS_* keys (previous copy in .env.bak)" >&2
fi

cat >> "$ENV_FILE" <<ENVEOF
DATABRICKS_HOST=$HOST
DATABRICKS_TOKEN=$TOKEN
DATABRICKS_GENIE_SPACE_ID=$SPACE
ENVEOF
chmod 600 "$ENV_FILE"

echo "==> wrote $ENV_FILE (gitignored, mode 600)" >&2
echo "==> next: npm run databricks:check" >&2
