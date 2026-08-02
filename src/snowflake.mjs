// Snowflake connection + trace tagging.
//
// WHY SNOWFLAKE IS DIFFERENT, AND SIMPLER:
// On Postgres this repo injects trace context as a SQL comment and reconstructs
// the plan tree by parsing the server log. Snowflake has a native slot for
// exactly this — QUERY_TAG — which lands in ACCOUNT_USAGE.QUERY_HISTORY without
// any log access at all. And QUERY_ATTRIBUTION_HISTORY reports actual credits
// per query, so cost stops being MODELLED and becomes MEASURED.
//
// Every dollar figure published by this project so far is Snowflake billing
// rules applied to Postgres execution times. This module is how that claim gets
// checked.
//
// Credentials come from the environment only — never a file in the repo, never
// an argument. Set them in your shell:
//
//   export SNOWFLAKE_ACCOUNT='ORGNAME-ACCOUNTNAME'   # e.g. ABCDEFG-HI12345
//   export SNOWFLAKE_USER='your_user'
//   # then ONE of:
//   export SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_key.p8   # recommended
//   export SNOWFLAKE_PRIVATE_KEY_PASSPHRASE='...'              # if encrypted
//   export SNOWFLAKE_PAT='...'                                 # programmatic access token
//   export SNOWFLAKE_PASSWORD='...'                            # often blocked by MFA policy
//   # optional:
//   export SNOWFLAKE_WAREHOUSE=COMPUTE_WH SNOWFLAKE_ROLE=ACCOUNTADMIN
//   export SNOWFLAKE_DATABASE=SNOWFLAKE_SAMPLE_DATA SNOWFLAKE_SCHEMA=TPCH_SF1

import './env.mjs';   // loads .env (shell env still wins)
import snowflake from 'snowflake-sdk';
import { readFileSync } from 'node:fs';
import { createPrivateKey } from 'node:crypto';

snowflake.configure({ logLevel: 'OFF' });

export function envConfig() {
  const account = process.env.SNOWFLAKE_ACCOUNT;
  const username = process.env.SNOWFLAKE_USER;
  if (!account || !username) {
    throw new Error(
      'SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER must be set. See docs/SNOWFLAKE.md.'
    );
  }

  const base = {
    account,
    username,
    warehouse: process.env.SNOWFLAKE_WAREHOUSE ?? 'COMPUTE_WH',
    role: process.env.SNOWFLAKE_ROLE ?? undefined,
    database: process.env.SNOWFLAKE_DATABASE ?? 'SNOWFLAKE_SAMPLE_DATA',
    schema: process.env.SNOWFLAKE_SCHEMA ?? 'TPCH_SF1',
    clientSessionKeepAlive: true,
  };

  if (process.env.SNOWFLAKE_PRIVATE_KEY_PATH) {
    // Key-pair is the path Snowflake pushes for programmatic access; password
    // auth is commonly blocked outright by MFA policy on newer accounts.
    const pem = readFileSync(process.env.SNOWFLAKE_PRIVATE_KEY_PATH, 'utf8');
    const key = createPrivateKey({
      key: pem,
      format: 'pem',
      passphrase: process.env.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE || undefined,
    });
    return {
      ...base,
      authenticator: 'SNOWFLAKE_JWT',
      privateKey: key.export({ type: 'pkcs8', format: 'pem' }),
    };
  }
  if (process.env.SNOWFLAKE_PAT) {
    return { ...base, authenticator: 'PROGRAMMATIC_ACCESS_TOKEN', token: process.env.SNOWFLAKE_PAT };
  }
  if (process.env.SNOWFLAKE_PASSWORD) {
    return { ...base, password: process.env.SNOWFLAKE_PASSWORD };
  }
  throw new Error(
    'No credential found. Set SNOWFLAKE_PRIVATE_KEY_PATH (recommended), ' +
    'SNOWFLAKE_PAT, or SNOWFLAKE_PASSWORD. See docs/SNOWFLAKE.md.'
  );
}

export function connect(cfg = envConfig()) {
  const conn = snowflake.createConnection(cfg);
  return new Promise((resolve, reject) => {
    conn.connect((err) => (err ? reject(err) : resolve(conn)));
  });
}

export function execute(conn, sqlText, binds = []) {
  return new Promise((resolve, reject) => {
    conn.execute({
      sqlText,
      binds,
      complete: (err, stmt, rows) =>
        err ? reject(err) : resolve({ rows: rows ?? [], queryId: stmt.getQueryId() }),
    });
  });
}

// The trace context, as a QUERY_TAG rather than a SQL comment. Snowflake caps
// QUERY_TAG at 2000 characters, so intent is truncated rather than risking a
// rejected ALTER SESSION mid-run.
export function traceTag(span) {
  const tag = {
    t: span.trace_id,
    s: span.span_id,
    p: span.parent_span_id ?? null,
    a: span.agent_id,
    c: span.speculation_class,
    i: String(span.span_intent ?? '').slice(0, 300),
  };
  return JSON.stringify(tag).slice(0, 2000);
}

export async function setTag(conn, span) {
  // Bound parameter so a quote in the intent cannot break the statement.
  await execute(conn, 'ALTER SESSION SET QUERY_TAG = ?', [traceTag(span)]);
}

export const parseTag = (raw) => {
  try {
    const t = JSON.parse(raw);
    return t && t.t ? t : null;
  } catch {
    return null;
  }
};
