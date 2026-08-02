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

cat <<EOF

────────────────────────────────────────────────────────────────────────
STEP 1 — run this in a Snowsight worksheet:

ALTER USER $USER_NAME SET RSA_PUBLIC_KEY='$PUB';

────────────────────────────────────────────────────────────────────────
STEP 2 — run this in your shell (or add it to ~/.zshrc):

export SNOWFLAKE_ACCOUNT='$ACCOUNT'
export SNOWFLAKE_USER='$USER_NAME'
export SNOWFLAKE_WAREHOUSE='COMPUTE_WH'
export SNOWFLAKE_ROLE='ACCOUNTADMIN'
export SNOWFLAKE_PRIVATE_KEY_PATH='$KEY'

────────────────────────────────────────────────────────────────────────
STEP 3 — verify:

npm run snowflake:check

EOF
