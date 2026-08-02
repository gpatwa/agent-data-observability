// Preflight for the Snowflake pilot. Run this before anything else — it fails
// with a specific, actionable message rather than letting an agent run die
// halfway through on a permissions problem.

import { connect, execute, envConfig } from './snowflake.mjs';

const ok = (s) => `  ✓ ${s}`;
const bad = (s) => `  ✗ ${s}`;

async function main() {
  let cfg;
  try {
    cfg = envConfig();
  } catch (e) {
    console.error(bad(e.message));
    process.exit(1);
  }
  const authMode = cfg.authenticator === 'SNOWFLAKE_JWT' ? 'key-pair'
    : cfg.authenticator === 'PROGRAMMATIC_ACCESS_TOKEN' ? 'PAT' : 'password';
  console.log(`── SNOWFLAKE PREFLIGHT ────────────────────────────────────────`);
  console.log(`  account   ${cfg.account}`);
  console.log(`  user      ${cfg.username}`);
  console.log(`  auth      ${authMode}`);
  console.log(`  warehouse ${cfg.warehouse}   role ${cfg.role ?? '(default)'}`);

  let conn;
  try {
    conn = await connect(cfg);
    console.log(ok('connected'));
  } catch (e) {
    console.error(bad(`connect failed: ${e.message}`));
    if (/JWT|token is invalid/i.test(e.message)) {
      console.error('    Key-pair auth: confirm ALTER USER ... SET RSA_PUBLIC_KEY was run,');
      console.error('    and that SNOWFLAKE_USER matches the user it was set on.');
    }
    if (/password|MFA/i.test(e.message)) {
      console.error('    Password auth is often blocked by MFA policy. Use key-pair or a PAT.');
    }
    process.exit(1);
  }

  const q = async (label, sql, onRows) => {
    try {
      const { rows } = await execute(conn, sql);
      onRows(rows);
    } catch (e) {
      console.log(bad(`${label}: ${e.message.split('\n')[0]}`));
    }
  };

  await q('context', 'select current_account() a, current_region() r, current_version() v',
    (rows) => console.log(ok(`account ${rows[0].A} · region ${rows[0].R} · version ${rows[0].V}`)));

  await q('sample data',
    `select count(*) c from snowflake_sample_data.information_schema.schemata where schema_name like 'TPCH%'`,
    (rows) => console.log(rows[0].C > 0
      ? ok(`SNOWFLAKE_SAMPLE_DATA present (${rows[0].C} TPCH schemas)`)
      : bad('SNOWFLAKE_SAMPLE_DATA not visible to this role')));

  await q('tpch scale', `select count(*) c from snowflake_sample_data.tpch_sf1.orders`,
    (rows) => console.log(ok(`TPCH_SF1.orders readable — ${Number(rows[0].C).toLocaleString()} rows`)));

  // QUERY_TAG round trip: the whole mechanism depends on this landing.
  await q('query tag', `alter session set query_tag = '{"t":"preflight"}'`, () => {});
  await q('tag visible',
    `select query_tag from table(information_schema.query_history(result_limit=>10))
     where query_tag like '%preflight%' limit 1`,
    (rows) => console.log(rows.length
      ? ok('QUERY_TAG round-trips into query history')
      : bad('QUERY_TAG not visible yet (history can lag a few seconds — retry)')));
  await q('clear tag', `alter session unset query_tag`, () => {});

  // ACCOUNT_USAGE is the one that actually carries credits.
  await q('account_usage',
    `select count(*) c from snowflake.account_usage.query_history
      where start_time >= dateadd('hour',-24,current_timestamp())`,
    (rows) => console.log(ok(`ACCOUNT_USAGE.QUERY_HISTORY readable (${rows[0].C} rows in 24h)`)));

  await q('attribution',
    `select count(*) c from snowflake.account_usage.query_attribution_history
      where start_time >= dateadd('day',-7,current_timestamp())`,
    (rows) => console.log(rows[0].C > 0
      ? ok(`QUERY_ATTRIBUTION_HISTORY readable (${rows[0].C} rows in 7d) — measured cost available`)
      : bad('QUERY_ATTRIBUTION_HISTORY empty — new account, or not enough activity yet')));

  console.log('\n  Next: node src/snowflake-agent.mjs "<question>"');
  console.log('  Then (after ~1h): npm run snowflake:cost');
  conn.destroy(() => {});
}

main().catch((e) => { console.error('error:', e.message); process.exit(1); });
