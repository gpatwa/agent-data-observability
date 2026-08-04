// Deferred cost join: turn tagged agent queries into MEASURED credits.
//
// Two tiers, because Snowflake's views have very different latency:
//
//   INFORMATION_SCHEMA.QUERY_HISTORY   near-real-time, no credit attribution.
//                                      Used to confirm tagging worked at all.
//   ACCOUNT_USAGE.QUERY_HISTORY        ~45 min latency, has QUERY_TAG.
//   ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
//                                      hours of latency, has CREDITS_ATTRIBUTED_COMPUTE
//                                      — the actual per-query cost.
//
// So this cannot run in the same breath as the agent. Run the agent, wait, then
// run this. It reports clearly which tier had data rather than silently
// returning zeros.
//
//   node src/snowflake-cost.mjs [--hours 6] [--credit-price 3.00]

import { connect, execute, parseTag } from './snowflake.mjs';

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 ? process.argv[i + 1] : dflt;
};

// 72h by default: ACCOUNT_USAGE lags hours, and in practice the read happens
// well after the run. A 6h default silently reported "no tagged queries" for a
// run whose data was present the whole time.
const HOURS = Number(arg('hours', 72));
const CREDIT_PRICE = Number(arg('credit-price', 3.0)); // Standard edition list
const usd = (n) => `$${n.toFixed(4)}`;

async function main() {
  const conn = await connect();

  // --- tier 1: did tagging work at all? -----------------------------------
  const live = await execute(conn, `
    select QUERY_ID, QUERY_TAG, TOTAL_ELAPSED_TIME, BYTES_SCANNED, WAREHOUSE_NAME
    from table(information_schema.query_history(
      end_time_range_start => dateadd('hour', -${HOURS}, current_timestamp()),
      -- RESULT_LIMIT defaults to 100 and the WHERE below filters AFTER the
      -- function returns, so tagged queries beyond the 100 most recent were
      -- invisible. This reported "0 tagged queries" for runs that had tagged
      -- correctly.
      result_limit => 10000))
    where QUERY_TAG like '%"t":%'
      and QUERY_TAG not like '%preflight%'
    order by START_TIME desc
    limit 500`);

  const liveTagged = live.rows.filter((r) => parseTag(r.QUERY_TAG));
  console.log('── TAGGING (INFORMATION_SCHEMA, near real-time) ───────────────');
  console.log(`  tagged agent queries in last ${HOURS}h   ${liveTagged.length}`);
  if (!liveTagged.length) {
    console.log('\n  No tagged queries found. Either the agent has not run yet, or');
    console.log('  QUERY_TAG was not set. Run: node src/snowflake-agent.mjs "<question>"');
    conn.destroy(() => {});
    return;
  }
  const traces = new Set(liveTagged.map((r) => parseTag(r.QUERY_TAG).t));
  console.log(`  distinct traces                        ${traces.size}`);

  // --- tier 2: measured credits -------------------------------------------
  const cost = await execute(conn, `
    select q.QUERY_ID, q.QUERY_TAG, q.TOTAL_ELAPSED_TIME, q.BYTES_SCANNED,
           a.CREDITS_ATTRIBUTED_COMPUTE
    from snowflake.account_usage.query_history q
    left join snowflake.account_usage.query_attribution_history a
           on a.QUERY_ID = q.QUERY_ID
    where q.START_TIME >= dateadd('hour', -${HOURS}, current_timestamp())
      and q.QUERY_TAG like '%"t":%'
      and q.QUERY_TAG not like '%preflight%'
    order by q.START_TIME desc
    limit 1000`);

  console.log('\n── MEASURED COST (ACCOUNT_USAGE, lagging) ─────────────────────');
  if (!cost.rows.length) {
    console.log(`  ACCOUNT_USAGE has no tagged rows yet.`);
    console.log(`  QUERY_HISTORY lags ~45 min and QUERY_ATTRIBUTION_HISTORY longer.`);
    console.log(`  Re-run this later — the agent run is already recorded.`);
    conn.destroy(() => {});
    return;
  }

  const withCredits = cost.rows.filter((r) => r.CREDITS_ATTRIBUTED_COMPUTE != null);
  console.log(`  tagged queries in ACCOUNT_USAGE         ${cost.rows.length}`);
  console.log(`  with credit attribution                ${withCredits.length}`);
  if (!withCredits.length) {
    console.log('\n  Credits not attributed yet — that view lags the furthest.');
    console.log('  Everything else below would be zero, so stopping here.');
    conn.destroy(() => {});
    return;
  }

  // --- per trace -----------------------------------------------------------
  const byTrace = new Map();
  for (const r of withCredits) {
    const t = parseTag(r.QUERY_TAG);
    if (!t) continue;
    if (!byTrace.has(t.t)) byTrace.set(t.t, { queries: 0, credits: 0, ms: 0, bytes: 0 });
    const e = byTrace.get(t.t);
    e.queries += 1;
    e.credits += Number(r.CREDITS_ATTRIBUTED_COMPUTE) || 0;
    e.ms += Number(r.TOTAL_ELAPSED_TIME) || 0;
    e.bytes += Number(r.BYTES_SCANNED) || 0;
  }

  console.log('\n  trace             queries   credits      measured cost');
  let totC = 0, totQ = 0;
  for (const [id, e] of byTrace) {
    totC += e.credits; totQ += e.queries;
    console.log(
      `  ${id.slice(0, 16).padEnd(17)} ${String(e.queries).padStart(7)}   ` +
      `${e.credits.toFixed(6).padStart(9)}   ${usd(e.credits * CREDIT_PRICE).padStart(12)}`
    );
  }

  console.log('\n── MEASURED vs MODELLED ───────────────────────────────────────');
  const measuredPerTask = (totC * CREDIT_PRICE) / Math.max(byTrace.size, 1);
  console.log(`  traces                                 ${byTrace.size}`);
  console.log(`  total queries                          ${totQ}`);
  console.log(`  total credits                          ${totC.toFixed(6)}`);

  // Refuse to publish a comparison built on nothing. Attribution arrives
  // gradually, so a partially-populated view yields a near-zero cost that looks
  // like a real measurement and would invite "correcting" a published figure to
  // zero. Silence is the correct output until the data is actually there.
  if (totC <= 0) {
    console.log('\n  Total attributed credits are zero, so there is no measurement yet.');
    console.log('  NOT printing a measured-vs-modelled ratio — it would read as 0.00x');
    console.log('  and invite correcting a real published figure to zero.');
    console.log('  Re-run once QUERY_ATTRIBUTION_HISTORY has caught up.');
    conn.destroy(() => {});
    return;
  }

  console.log(`  MEASURED cost per resolved task        ${usd(measuredPerTask)}`);
  console.log(`  modelled equivalent (Postgres runs)    $0.073   <- published figure`);
  console.log(`\n  Ratio measured/modelled: ${(measuredPerTask / 0.073).toFixed(2)}x`);
  console.log('  A ratio far from 1.0 means the modelled numbers in the README and');
  console.log('  on the landing page need correcting — that is the point of this run.');

  conn.destroy(() => {});
}

main().catch((e) => {
  console.error('error:', e.message);
  process.exit(1);
});
