// Tests for the load-bearing logic: normalization, subsumption, and candidate
// synthesis. Every redundancy number this repo reports comes out of these
// functions, so they need golden cases — especially the NEGATIVE ones, where a
// too-eager subsumption rule would silently inflate the headline result.
//
//   node --test test/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  normalize, astHash, exactHash, extractShape, subsumes, coveringSet, synthesizeCandidates,
} from '../src/fingerprint.mjs';

const shape = (sql) => extractShape(sql);

// --- normalization ---------------------------------------------------------

test('alias choice does not change the hash', () => {
  const a = `select sum(amount) from orders o where o.region = 'EMEA'`;
  const b = `select sum(amount) from orders x where x.region = 'EMEA'`;
  assert.equal(astHash(a), astHash(b));
  assert.notEqual(exactHash(a), exactHash(b), 'exact hash should still differ');
});

test('predicate order does not change the hash', () => {
  const a = `select sum(amount) from orders where region = 'EMEA' and channel = 'email'`;
  const b = `select sum(amount) from orders where channel = 'email' and region = 'EMEA'`;
  assert.equal(astHash(a), astHash(b));
});

test('whitespace and case do not change the hash', () => {
  const a = `SELECT   sum(amount)\n  FROM orders\n WHERE region = 'EMEA'`;
  const b = `select sum(amount) from orders where region = 'EMEA'`;
  assert.equal(astHash(a), astHash(b));
});

test('a genuinely different query gets a different hash', () => {
  const a = `select sum(amount) from orders where region = 'EMEA'`;
  const b = `select sum(amount) from orders where region = 'APAC'`;
  assert.notEqual(astHash(a), astHash(b));
});

// --- subsumption: positive cases -------------------------------------------

test('a rollup subsumes a query filtered on the grouped column', () => {
  const rollup = shape(`select order_date, sum(amount) from orders group by order_date`);
  const daily  = shape(`select sum(amount) from orders where order_date = '2026-07-12'`);
  assert.ok(subsumes(rollup, daily));
});

test('a two-dimension rollup subsumes a single-cell query', () => {
  const cube = shape(`select region, channel, sum(amount) from orders group by region, channel`);
  const cell = shape(`select sum(amount) from orders where region = 'EMEA' and channel = 'email'`);
  assert.ok(subsumes(cube, cell));
});

test('avg() is servable from sum() + count()', () => {
  const rollup = shape(`select region, sum(amount), count(*) from orders group by region`);
  const avgQ   = shape(`select avg(amount) from orders where region = 'EMEA'`);
  assert.ok(subsumes(rollup, avgQ));
});

test('a coarser grouping subsumes a finer one on the same measures', () => {
  const fine   = shape(`select region, channel, sum(amount) from orders group by region, channel`);
  const coarse = shape(`select region, sum(amount) from orders group by region`);
  assert.ok(subsumes(fine, coarse), 'region+channel rollup should answer region-only');
});

// --- subsumption: negative cases (the ones that keep the numbers honest) ----

test('does NOT subsume when the filter column is not in the grouping', () => {
  const rollup = shape(`select region, sum(amount) from orders group by region`);
  const q      = shape(`select sum(amount) from orders where channel = 'email'`);
  assert.equal(subsumes(rollup, q), false);
});

test('does NOT subsume when the anchor is more restrictive than the query', () => {
  const narrow = shape(`select order_date, sum(amount) from orders where region = 'EMEA' group by order_date`);
  const wide   = shape(`select sum(amount) from orders where order_date = '2026-07-12'`);
  assert.equal(subsumes(narrow, wide), false);
});

test('does NOT subsume across different tables', () => {
  const orders  = shape(`select order_date, sum(amount) from orders group by order_date`);
  const refunds = shape(`select sum(amount) from refunds where refund_date = '2026-07-12'`);
  assert.equal(subsumes(orders, refunds), false);
});

test('does NOT subsume a measure the anchor never computed', () => {
  const rollup = shape(`select region, sum(amount) from orders group by region`);
  const q      = shape(`select count(order_id) from orders where region = 'EMEA'`);
  assert.equal(subsumes(rollup, q), false);
});

test('avg() alone cannot serve another avg() at finer grain', () => {
  const rollup = shape(`select region, avg(amount) from orders group by region`);
  const q      = shape(`select avg(amount) from orders where region = 'EMEA' and channel = 'email'`);
  assert.equal(subsumes(rollup, q), false, 'avg is not additive and channel is not grouped');
});

test('a finer grouping cannot be recovered from a coarser one', () => {
  const coarse = shape(`select region, sum(amount) from orders group by region`);
  const fine   = shape(`select region, channel, sum(amount) from orders group by region, channel`);
  assert.equal(subsumes(coarse, fine), false);
});

test('non-aggregate queries do not participate', () => {
  assert.equal(shape(`select * from orders limit 5`), null);
  assert.equal(shape(`select order_id from orders where region = 'EMEA'`), null);
});

// --- candidate synthesis ---------------------------------------------------

test('synthesis lifts equality filters into the grouping', () => {
  const shapes = [
    shape(`select sum(amount) from orders where order_date = '2026-07-01'`),
    shape(`select sum(amount) from orders where order_date = '2026-07-02'`),
  ];
  const cands = synthesizeCandidates(shapes);
  assert.ok(
    cands.some((c) => c.groupby.includes('order_date') && c.filters.length === 0),
    'expected a synthesized GROUP BY order_date candidate'
  );
});

test('one synthesized anchor covers a whole per-day fan-out', () => {
  const shapes = Array.from({ length: 31 }, (_, i) =>
    shape(`select count(order_id), sum(amount) from orders where order_date = '2026-07-${String(i + 1).padStart(2, '0')}'`)
  );
  const cover = coveringSet(shapes);
  assert.equal(cover.coveredCount, 31);
  assert.equal(cover.anchors.length, 1, 'a single GROUP BY order_date should serve all 31');
  assert.ok(cover.anchors[0].anchor.synthetic, 'and it should be synthesized, not observed');
});

test('unrelated queries are not collapsed together', () => {
  const shapes = [
    shape(`select sum(amount) from orders where region = 'EMEA'`),
    shape(`select sum(amount) from refunds where refund_date = '2026-07-01'`),
  ];
  const cover = coveringSet(shapes);
  assert.ok(cover.anchors.length >= 2, 'different tables cannot share one anchor');
});

test('covering set never claims to cover a query it cannot', () => {
  const shapes = [
    shape(`select region, sum(amount) from orders group by region`),
    shape(`select channel, avg(amount) from orders group by channel`),
    shape(`select sum(amount) from refunds`),
  ];
  const cover = coveringSet(shapes);
  for (const a of cover.anchors) {
    for (const i of a.covers) {
      assert.ok(subsumes(a.anchor, shapes[i]), `anchor claimed to cover query ${i} but does not subsume it`);
    }
  }
});

// --- real-agent SQL patterns the regex extractor could not read ------------

test('positional GROUP BY resolves to the select-list expressions', () => {
  const s = shape(`select region, channel, sum(amount) from orders group by 1, 2`);
  assert.deepEqual(s.groupby, ['channel', 'region']);
  assert.deepEqual(s.measures, ['sum(amount)']);
});

test('an alias in GROUP BY resolves to the underlying expression', () => {
  const s = shape(`select date_trunc('month', order_date) as m, sum(amount) from orders group by m`);
  assert.deepEqual(s.groupby, [`date_trunc('month',order_date)`]);
});

test('a daily rollup subsumes a monthly question (days roll up)', () => {
  const daily   = shape(`select order_date, sum(amount) from orders group by order_date`);
  const monthly = shape(`select date_trunc('month', order_date), sum(amount) from orders group by 1`);
  assert.ok(subsumes(daily, monthly));
});

test('a monthly rollup does NOT subsume a daily question', () => {
  const monthly = shape(`select date_trunc('month', order_date), sum(amount) from orders group by 1`);
  const daily   = shape(`select order_date, sum(amount) from orders group by order_date`);
  assert.equal(subsumes(monthly, daily), false);
});

test('count(distinct) is not derivable from a grouped rollup', () => {
  const rollup = shape(`select region, count(distinct customer_id) from orders group by region`);
  const q      = shape(`select count(distinct customer_id) from orders where region = 'EMEA'`);
  assert.equal(subsumes(rollup, q), false, 'distinct counts do not sum');
});

test('queries with joins are excluded rather than mis-parsed', () => {
  assert.equal(shape(`select sum(o.amount) from orders o join refunds r on r.order_id = o.order_id group by o.region`), null);
});

test('CTEs are excluded', () => {
  assert.equal(shape(`with x as (select * from orders) select sum(amount) from x`), null);
});

test('OR in the WHERE clause is excluded', () => {
  assert.equal(shape(`select sum(amount) from orders where region = 'EMEA' or region = 'APAC'`), null);
});

test('HAVING is excluded', () => {
  assert.equal(shape(`select region, sum(amount) from orders group by region having sum(amount) > 100`), null);
});

test('unparseable SQL returns null instead of throwing', () => {
  assert.equal(shape(`this is not sql at all`), null);
  assert.equal(shape(``), null);
});

test('a cast dimension is distinct from its base column', () => {
  const s = shape(`select order_date::date, sum(amount) from orders group by 1`);
  assert.equal(s.groupby.length, 1);
  assert.notEqual(s.groupby[0], 'order_date');
});

// --- citation claim parsing ------------------------------------------------

test('CITED line is parsed through markdown emphasis', async () => {
  const { parseCited } = await import('../src/verify-citations.mjs');
  assert.deepEqual([...parseCited('text\n**CITED: q1, q4, q5**')], ['q1', 'q4', 'q5']);
  assert.deepEqual([...parseCited('CITED: q1,q2')], ['q1', 'q2']);
  assert.deepEqual([...parseCited('  - CITED: q3 ')], ['q3']);
  assert.equal(parseCited('no citation line here'), null);
});
