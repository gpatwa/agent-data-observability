// A simulated data agent answering "Why did revenue drop in July 2026?"
//
// The shape of the speculation is what matters, and it mirrors what the BAIR
// post describes: wide fan-out of near-duplicate probes, retries with cosmetic
// differences, per-partition scans that one rollup would have covered, and a
// dead-end hypothesis whose results never reach the answer.
//
// Time is compressed: the agent "thinks" for THINK_MS/DILATION of real time
// between queries. assemble.mjs scales elapsed time back up by DILATION before
// applying real warehouse billing rules.

import pg from 'pg';
import { writeFileSync } from 'node:fs';
import { newTrace, newSpan } from './context.mjs';
import { TracedClient } from './tracedb.mjs';
import { DILATION, PG } from './config.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const think = (ms) => sleep(ms / DILATION);

const JULY = "order_date >= '2026-07-01' and order_date <= '2026-07-31'";

async function main() {
  const client = new pg.Client(PG);
  await client.connect();
  const db = new TracedClient(client, { mode: 'observe' });

  const trace = newTrace({
    agentId: 'revenue-analyst-v3',
    model: 'claude-opus-5',
    taskIntent: 'Why did revenue drop in July 2026?',
  });

  const root = newSpan(trace, { intent: 'root', speculationClass: 'probe' });
  const cited = [];

  // --- Phase 1: schema discovery. Cheap, necessary, never cited. ------------
  for (const t of ['orders', 'refunds']) {
    const s = newSpan(trace, { intent: `discover schema ${t}`, speculationClass: 'probe', parent: root.span_id });
    await db.run(s, `select column_name, data_type from information_schema.columns where table_name = '${t}'`);
    await think(1500);
    const p = newSpan(trace, { intent: `peek rows ${t}`, speculationClass: 'probe', parent: root.span_id });
    await db.run(p, `select * from ${t} limit 5`);
    await think(1200);
  }

  // --- Phase 2: confirm the drop is real. Cited. ---------------------------
  const h0 = newSpan(trace, { intent: 'confirm monthly drop', speculationClass: 'refine', parent: root.span_id });
  await db.run(h0, `select sum(amount) from orders where order_date >= '2026-06-01' and order_date <= '2026-06-30'`);
  await think(2500);
  const h0b = newSpan(trace, { intent: 'confirm monthly drop (july)', speculationClass: 'refine', parent: root.span_id });
  await db.run(h0b, `select sum(amount) from orders where ${JULY}`);
  cited.push(h0.span_id, h0b.span_id);
  await think(3000);

  // --- Phase 3: hypothesis "fewer orders" — 31 per-day probes. -------------
  // Every one of these is subsumed by a single GROUP BY order_date.
  const hA = newSpan(trace, { intent: 'hypothesis: order volume fell', speculationClass: 'refine', parent: root.span_id });
  await db.run(hA, `select count(order_id) from orders where ${JULY}`);
  await think(2000);
  for (let d = 1; d <= 31; d++) {
    const day = `2026-07-${String(d).padStart(2, '0')}`;
    const s = newSpan(trace, { intent: `daily volume ${day}`, speculationClass: 'probe', parent: hA.span_id });
    await db.run(s, `select count(order_id), sum(amount) from orders where order_date = '${day}'`);
    await think(900);
  }

  // --- Phase 4: hypothesis "AOV fell" — retried 3x with cosmetic edits. ----
  const hB = newSpan(trace, { intent: 'hypothesis: AOV fell', speculationClass: 'refine', parent: root.span_id });
  await db.run(hB, `select avg(amount) from orders where order_date >= '2026-06-01' and order_date <= '2026-06-30'`);
  await think(1800);
  const aovVariants = [
    `select avg(amount) from orders where ${JULY}`,
    `SELECT   avg(amount)  FROM orders o  WHERE o.order_date <= '2026-07-31' AND o.order_date >= '2026-07-01'`,
    `select avg(amount) from orders as t where t.order_date >= '2026-07-01' and t.order_date <= '2026-07-31'`,
  ];
  let prev = null;
  for (let i = 0; i < aovVariants.length; i++) {
    const s = newSpan(trace, {
      intent: 'avg order value july',
      speculationClass: 'refine',
      parent: hB.span_id,
      attempt: i + 1,
      retryOf: prev,
    });
    await db.run(s, aovVariants[i]);
    prev = s.span_id;
    await think(2000);
  }

  // --- Phase 5: hypothesis "region/channel mix" — the real cause. ----------
  const hC = newSpan(trace, { intent: 'hypothesis: mix shift', speculationClass: 'refine', parent: root.span_id });
  await db.run(hC, `select region, sum(amount) from orders where ${JULY} group by region`);
  await think(2200);
  const regions = ['AMER', 'EMEA', 'APAC'];
  const channels = ['paid_search', 'organic', 'partner', 'email'];
  // 12 per-cell probes, all subsumed by one GROUP BY region, channel.
  for (const r of regions) {
    for (const c of channels) {
      const s = newSpan(trace, { intent: `cell ${r}/${c}`, speculationClass: 'probe', parent: hC.span_id });
      await db.run(s, `select sum(amount) from orders where ${JULY} and region = '${r}' and channel = '${c}'`);
      await think(800);
    }
  }
  // ...and then it runs the rollup anyway, having already paid for the cells.
  const roll = newSpan(trace, { intent: 'rollup region x channel', speculationClass: 'refine', parent: hC.span_id });
  await db.run(roll, `select region, channel, sum(amount) from orders where ${JULY} group by region, channel`);
  cited.push(roll.span_id);
  await think(3500);

  // --- Phase 6: dead end — refunds. Never cited. --------------------------
  const hD = newSpan(trace, { intent: 'hypothesis: refunds spiked', speculationClass: 'probe', parent: root.span_id });
  await db.run(hD, `select count(refund_id) from refunds`);
  await think(1500);
  for (const q of [
    `select count(refund_id), sum(amount) from refunds where refund_date >= '2026-07-01' and refund_date <= '2026-07-31'`,
    `select count(refund_id), sum(amount) from refunds where refund_date >= '2026-06-01' and refund_date <= '2026-06-30'`,
    `select refund_date, sum(amount) from refunds where refund_date >= '2026-07-01' and refund_date <= '2026-07-31' group by refund_date`,
  ]) {
    const s = newSpan(trace, { intent: 'refund check', speculationClass: 'probe', parent: hD.span_id });
    await db.run(s, q);
    await think(1800);
  }

  // --- Phase 7: narrow to the culprit, then confirm. ----------------------
  const hE = newSpan(trace, { intent: 'isolate EMEA paid_search', speculationClass: 'refine', parent: root.span_id });
  await db.run(hE, `select sum(amount) from orders where ${JULY} and region = 'EMEA' and channel = 'paid_search'`);
  await think(2000);
  for (let d = 1; d <= 31; d++) {
    const day = `2026-07-${String(d).padStart(2, '0')}`;
    const s = newSpan(trace, { intent: `emea paid_search ${day}`, speculationClass: 'probe', parent: hE.span_id });
    await db.run(s, `select sum(amount) from orders where order_date = '${day}' and region = 'EMEA' and channel = 'paid_search'`);
    await think(700);
  }

  const fin = newSpan(trace, { intent: 'final confirming query', speculationClass: 'final', parent: root.span_id });
  await db.run(fin, `select order_date, sum(amount) from orders where ${JULY} and region = 'EMEA' and channel = 'paid_search' group by order_date order by order_date`);
  cited.push(fin.span_id);

  db.citeResults(cited);
  writeFileSync(
    new URL('../out/agent-events.jsonl', import.meta.url),
    db.dumpEvents().map((e) => JSON.stringify(e)).join('\n')
  );

  await client.end();
  console.log(`agent finished: ${db.agentEvents.length} queries issued, ${cited.length} results cited in the answer`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
