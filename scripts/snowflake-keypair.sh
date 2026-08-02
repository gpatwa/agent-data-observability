#!/usr/bin/env bash
# Generates a Snowflake key-pair and prints the exact statements to finish setup.
#
# Key-pair is the recommended auth for drivers: it needs no network policy
# (which blocks PATs on fresh trials), it survives enabling MFA later, and it
# keeps a long-lived password out of your shell environment.
#
# The private key never leaves this machine and is never printed. Only the
# PUBLIC key body is echoed, for pasting into Snowflake.
#
#   ./scripts/snowflake-keypair.sh <SNOWFLAKE_USER> [ACCOUNT]
set -euo pipefail

USER_NAME="${1:-}"
ACCOUNT="${2:-${SNOWFLAKE_ACCOUNT:-<YOUR-ACCOUNT>}}"
KEY="${SNOWFLAKE_PRIVATE_KEY_PATH:-$HOME/.ssh/snowflake_key.p8}"

if [ -z "$USER_NAME" ]; then
  echo "usage: ./scripts/snowflake-keypair.sh <SNOWFLAKE_USER> [ACCOUNT]" >&2
  echo "  find your user with:  SELECT CURRENT_USER();" >&2
  exit 1
fi
command -v openssl >/dev/null || { echo "error: openssl not on PATH" >&2; exit 1; }

if [ -f "$KEY" ]; then
  echo "==> reusing existing private key at $KEY"
else
  echo "==> generating private key at $KEY"
  mkdir -p "$(dirname "$KEY")"
  openssl genrsa 2048 2>/dev/null | openssl pkcs8 -topk8 -inform PEM -out "$KEY" -nocrypt
  chmod 600 "$KEY"
fi

PUB=$(openssl rsa -in "$KEY" -pubout 2>/dev/null | grep -v '^-' | tr -d '\n')

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"

# Write .env so there is nothing to copy by hand. It is gitignored; the shell
# environment still takes precedence over it at runtime.
if [ -f "$ENV_FILE" ] && grep -q '^SNOWFLAKE_ACCOUNT=' "$ENV_FILE"; then
  cp "$ENV_FILE" "$ENV_FILE.bak"
  grep -v '^SNOWFLAKE_' "$ENV_FILE.bak" > "$ENV_FILE" || true
  echo "==> existing .env had SNOWFLAKE_* keys; previous copy saved to .env.bak"
fi

cat >> "$ENV_FILE" <<ENVEOF
SNOWFLAKE_ACCOUNT=$ACCOUNT
SNOWFLAKE_USER=$USER_NAME
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=ACCOUNTADMIN
SNOWFLAKE_PRIVATE_KEY_PATH=$KEY
ENVEOF
chmod 600 "$ENV_FILE"

echo "==> wrote $ENV_FILE (gitignored, mode 600)"

cat <<EOF

────────────────────────────────────────────────────────────────────────
ONE manual step — paste into a Snowsight worksheet and run:

ALTER USER $USER_NAME SET RSA_PUBLIC_KEY='$PUB';

────────────────────────────────────────────────────────────────────────
Then verify (no exports needed — .env is loaded automatically):

  npm run snowflake:check

EOF
