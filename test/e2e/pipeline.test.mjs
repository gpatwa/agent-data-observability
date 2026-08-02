// End-to-end test of the actual claim this repo makes:
//   an agent's queries go into a warehouse, and the whole plan tree comes back
//   out of the warehouse's own log with nothing in the data path.
//
// The unit tests cover pure functions. Nothing covered the pipeline, which is
// where the interesting failures were (log parsing, dilation, denominators).
//
// Needs a live Postgres with log_statement=all whose log file is readable.
// scripts/e2e.sh manages that cluster. Run: npm run test:e2e

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import pg from 'pg';
import { newTrace, newSpan, serializeContext } from '../../src/context.mjs';
import { reconstruct, bill, sec2dollars } from '../../src/trace.mjs';
import { coveringSet, subsumes } from '../../src/fingerprint.mjs';
import { readOnlyRefusal } from '../../src/readonly.mjs';
import { PG } from '../../src/config.mjs';

const LOG_PATH = process.env.E2E_LOG_PATH;
let client;
let eventsPath;
let issued = [];

before(async () => {
  assert.ok(LOG_PATH, 'E2E_LOG_PATH must point at the cluster query log (see scripts/e2e.sh)');
  client = new pg.Client(PG);
  await client.connect();

  // Small fixture — this test is about the pipeline, not query performance.
  await client.query(`drop table if exists e2e_orders`);
  await client.query(`create table e2e_orders (
    order_id bigserial primary key, order_date date, region text, channel text, amount numeric(10,2))`);
  await client.query(`insert into e2e_orders (order_date, region, channel, amount)
    select (DATE '2026-07-01' + (n % 20))::date,
           (ARRAY['AMER','EMEA','APAC'])[1 + (n % 3)],
           (ARRAY['paid_search','organic','email'])[1 + (n % 3)],
           (n % 400 + 10)::numeric(10,2)
    from generate_series(1, 4000) n`);
  await client.query('analyze e2e_orders');

  // Truncate the log so this test sees only its own traffic.
  writeFileSync(LOG_PATH, '');

  const trace = newTrace({ agentId: 'e2e-agent', model: 'test-model', taskIntent: 'e2e pipeline check' });
  const root = newSpan(trace, { intent: 'total revenue', speculationClass: 'probe' });
  eventsPath = join(mkdtempSync(join(tmpdir(), 'adoe2e-')), 'events.jsonl');

  // A deliberate fan-out: five single-day probes that ONE rollup should serve.
  const plan = [
    [root, `select sum(amount) from e2e_orders`],
    ...[1, 2, 3, 4, 5].map((d) => [
      newSpan(trace, { intent: `day ${d}`, speculationClass: 'probe', parent: root.span_id }),
      `select sum(amount), count(*) from e2e_orders where order_date = '2026-07-0${d}'`,
    ]),
  ];

  const events = [];
  for (const [span, sql] of plan) {
    assert.equal(readOnlyRefusal(sql), null, 'fixture SQL must pass the read-only guard');
    await client.query(`${serializeContext(span)} ${sql}`);
    issued.push({ span_id: span.span_id, sql });
    events.push(JSON.stringify({
      trace_id: span.trace_id, span_id: span.span_id, parent_span_id: span.parent_span_id,
      label: span.span_intent, used_downstream: true, grounded: true, values: [],
    }));
    await new Promise((r) => setTimeout(r, 60)); // distinct log timestamps
  }
  writeFileSync(eventsPath, events.join('\n'));
});

after(async () => {
  if (client) { await client.query('drop table if exists e2e_orders'); await client.end(); }
});

test('every issued query is recovered from the warehouse log', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  assert.equal(spans.length, issued.length,
    `issued ${issued.length} tagged queries, recovered ${spans.length}`);
  for (const i of issued) {
    assert.ok(spans.some((s) => s.span_id === i.span_id), `span ${i.span_id} missing from the log`);
  }
});

test('trace context survives the round trip through SQL comments', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  assert.equal(new Set(spans.map((s) => s.trace_id)).size, 1, 'one trace expected');
  assert.ok(spans.every((s) => s.agent_id === 'e2e-agent'));
  // The SQL comes back without the injected comment.
  assert.ok(spans.every((s) => !s.sql.includes('agenttrace')), 'comment must be stripped from recovered SQL');
});

test('parent links resolve to spans that exist', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  const ids = new Set(spans.map((s) => s.span_id));
  const children = spans.filter((s) => s.parent_span_id);
  assert.ok(children.length >= 5, 'expected the fan-out children');
  for (const c of children) assert.ok(ids.has(c.parent_span_id), 'dangling parent link');
});

test('billing is internally consistent', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  const b = bill(spans, 1);
  assert.ok(b.productiveSec > 0, 'queries must record execution time');
  assert.ok(b.billedSec >= b.productiveSec, 'billed time cannot be less than productive time');
  assert.ok(b.billedSec >= 60, 'the 60s minimum must apply at least once');
  assert.equal(
    Math.round((b.overhead + b.productiveSec) * 1000),
    Math.round(b.billedSec * 1000),
    'overhead + productive must equal billed'
  );
  const summed = spans.reduce((a, s) => a + s.cost, 0);
  assert.ok(Math.abs(summed - sec2dollars(b.billedSec)) < 1e-6,
    'per-span attributed cost must sum to the total bill');
});

test('dilation scales elapsed time but never execution time', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  const a = bill(spans, 1);
  const b = bill(spans, 100);
  assert.ok(Math.abs(a.productiveSec - b.productiveSec) < 1e-9,
    'query execution time must not be scaled by dilation');
  assert.ok(b.elapsedSec > a.elapsedSec, 'elapsed wall-clock should scale');
});

test('the day fan-out collapses to a single synthesized rollup', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  const dayShapes = spans
    .filter((s) => /order_date = /.test(s.sql))
    .map((s) => s.shape);
  assert.equal(dayShapes.length, 5);
  const cover = coveringSet(dayShapes);
  assert.equal(cover.coveredCount, 5, 'all five day probes should be covered');
  assert.equal(cover.anchors.length, 1, 'one GROUP BY order_date rollup should serve all five');
  assert.ok(cover.anchors[0].anchor.synthetic, 'and it should be synthesized — the agent never ran it');
});

test('no anchor ever claims a query it cannot actually serve', () => {
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  const shapes = spans.map((s) => s.shape);
  const cover = coveringSet(shapes);
  for (const a of cover.anchors) {
    for (const i of a.covers) {
      assert.ok(subsumes(a.anchor, shapes[i]), 'anchor covers a query it does not subsume');
    }
  }
});

test('untagged traffic is ignored, not mis-attributed', async () => {
  await client.query('select count(*) from e2e_orders'); // no trace comment
  await new Promise((r) => setTimeout(r, 120));
  const spans = reconstruct(LOG_PATH, [eventsPath]);
  assert.equal(spans.length, issued.length, 'untagged query must not appear as a span');
});
