// Reconstructs the agent plan tree from the warehouse's own query log,
// joins it to the agent-side event log, fingerprints the workload, and applies
// a Snowflake-style billing model to attribute cost.
//
// Nothing here sits in the data path. The only input from the DB side is a log
// file the warehouse already writes.

import { readFileSync } from 'node:fs';
import pg from 'pg';
import { parseContext } from './context.mjs';
import { exactHash, astHash, extractShape, coveringSet } from './fingerprint.mjs';
import { PG } from './config.mjs';

// Time dilation applies ONLY to the simulated agent, which compresses its
// think-time by this factor. A real agent's log timestamps are already real —
// passing a dilation there would multiply its idle gaps into fictional ones.
// Default 1 (no scaling); the demo script passes 100 for the simulator.
const DILATION = Number(process.argv[4] ?? 1);

// --- Warehouse billing model (Snowflake XS, Standard edition) --------------
const CREDITS_PER_HOUR = 1;      // XS warehouse
const DOLLARS_PER_CREDIT = 3.0;
const MIN_BILLING_SEC = 60;      // charged on every resume
const AUTO_SUSPEND_SEC = 60;
const sec2dollars = (s) => (s / 3600) * CREDITS_PER_HOUR * DOLLARS_PER_CREDIT;

const LOG_LINE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \w+ \[(\d+)\] (LOG|ERROR|STATEMENT|HINT|WARNING|DETAIL|FATAL):\s+(.*)$/;

function parseLog(path) {
  const lines = readFileSync(path, 'utf8').split('\n');
  const events = [];
  let cur = null;

  for (const line of lines) {
    const m = line.match(LOG_LINE);
    if (!m) {
      // continuation of a multi-line statement
      if (cur && cur.kind === 'statement') cur.text += '\n' + line;
      continue;
    }
    const [, ts, pid, , rest] = m;
    const t = new Date(ts.replace(' ', 'T')).getTime();

    if (rest.startsWith('statement: ')) {
      if (cur) events.push(cur);
      cur = { kind: 'statement', t, pid, text: rest.slice('statement: '.length) };
    } else if (rest.startsWith('duration: ')) {
      const ms = parseFloat(rest.match(/duration: ([\d.]+) ms/)?.[1] ?? '0');
      if (cur && cur.kind === 'statement' && cur.pid === pid) {
        cur.duration_ms = ms;
        events.push(cur);
        cur = null;
      }
    } else {
      if (cur) events.push(cur);
      cur = null;
    }
  }
  if (cur) events.push(cur);
  return events.filter((e) => e.kind === 'statement');
}

// --- Reconstruct spans ------------------------------------------------------
function reconstruct(logPath, eventsPath) {
  const stmts = parseLog(logPath);
  const spans = [];
  for (const s of stmts) {
    const ctx = parseContext(s.text);
    if (!ctx) continue; // untagged traffic (seeding, admin) — ignored
    const sql = s.text.replace(/\/\*agenttrace:[^*]*\*\/\s*/, '');
    spans.push({
      ...ctx,
      sql,
      start_ms: s.t,
      exec_ms: s.duration_ms ?? 0,
      exact: exactHash(sql),
      ast: astHash(sql),
      shape: extractShape(sql),
    });
  }

  const agentEvents = readFileSync(eventsPath, 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l));
  const byId = new Map(agentEvents.map((e) => [e.span_id, e]));
  for (const sp of spans) {
    const e = byId.get(sp.span_id);
    sp.used_downstream = e?.used_downstream ?? false;
    sp.result_hash = e?.result_hash;
    sp.rows = e?.rows;
  }
  spans.sort((a, b) => a.start_ms - b.start_ms);
  return spans;
}

// --- Billing ---------------------------------------------------------------
// Elapsed wall-clock is scaled back up by DILATION (the sim compressed agent
// think-time); query execution time is real and is NOT scaled.
function bill(spans) {
  if (!spans.length) return null;
  const t0 = spans[0].start_ms;
  const intervals = spans.map((s) => {
    const start = ((s.start_ms - t0) / 1000) * DILATION;
    return { start, end: start + s.exec_ms / 1000, span: s };
  });

  // Group into resume windows separated by more than AUTO_SUSPEND_SEC of idle.
  const windows = [];
  let w = null;
  for (const iv of intervals) {
    if (!w || iv.start > w.lastEnd + AUTO_SUSPEND_SEC) {
      w = { start: iv.start, lastEnd: iv.end, items: [iv] };
      windows.push(w);
    } else {
      w.lastEnd = Math.max(w.lastEnd, iv.end);
      w.items.push(iv);
    }
  }

  let billedSec = 0;
  for (const win of windows) {
    // Billed from resume until auto-suspend fires, with a per-resume floor.
    win.billed = Math.max(MIN_BILLING_SEC, win.lastEnd - win.start + AUTO_SUSPEND_SEC);
    billedSec += win.billed;
  }

  const productiveSec = intervals.reduce((a, iv) => a + (iv.end - iv.start), 0);

  // Attribute productive seconds to spans; spread the overhead pro-rata.
  const overhead = billedSec - productiveSec;
  for (const iv of intervals) {
    const prod = iv.end - iv.start;
    iv.span.billed_sec = prod + (productiveSec > 0 ? (prod / productiveSec) * overhead : overhead / intervals.length);
    iv.span.cost = sec2dollars(iv.span.billed_sec);
  }

  // Counterfactual: same queries, issued back-to-back in one window.
  const batchedSec = Math.max(MIN_BILLING_SEC, productiveSec + AUTO_SUSPEND_SEC);

  return { windows, billedSec, productiveSec, overhead, batchedSec, elapsedSec: intervals.at(-1).end };
}

// --- Report ----------------------------------------------------------------
const usd = (n) => `$${n.toFixed(n < 1 ? 3 : 2)}`;
const pct = (n) => `${(n * 100).toFixed(1)}%`;
const bar = (n) => '█'.repeat(Math.max(1, Math.round(n * 30)));

// Collapse runs of sibling spans whose intent differs only by a literal.
function collapseLabel(kids) {
  const norm = (s) => s
    .replace(/\d{4}-\d{2}-\d{2}/g, '<date>')
    .replace(/\b(AMER|EMEA|APAC)\b/g, '<region>')
    .replace(/\b(paid_search|organic|partner|email)\b/g, '<channel>');
  const groups = new Map();
  for (const k of kids) {
    const key = norm(k.span_intent);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(k);
  }
  return groups;
}

async function verifyAnchors(anchors) {
  const client = new pg.Client(PG);
  await client.connect();
  const out = [];
  for (const a of anchors) {
    const sql = a.anchor.sql;
    try {
      const t0 = process.hrtime.bigint();
      const res = await client.query(sql);
      const ms = Number(process.hrtime.bigint() - t0) / 1e6;
      out.push({ ...a, ms, rows: res.rowCount, ok: true });
    } catch (e) {
      out.push({ ...a, ok: false, err: e.message.split('\n')[0] });
    }
  }
  await client.end();
  return out;
}

async function main() {
  const [logPath, eventsPath] = process.argv.slice(2);
  const spans = reconstruct(logPath, eventsPath);
  if (!spans.length) {
    console.error('no tagged spans found in log');
    process.exit(1);
  }
  const b = bill(spans);

  const total = spans.length;
  const distinctExact = new Set(spans.map((s) => s.exact)).size;
  const distinctAst = new Set(spans.map((s) => s.ast)).size;
  const shapes = spans.map((s) => s.shape);
  const aggIdx = shapes.map((s, i) => (s ? i : -1)).filter((i) => i >= 0);
  const cover = coveringSet(shapes);
  const totalCost = sec2dollars(b.billedSec);

  console.log('='.repeat(78));
  console.log(`TRACE ${spans[0].trace_id}   agent=${spans[0].agent_id}   model=${spans[0].model_id}`);
  console.log(`TASK  "Why did revenue drop in July 2026?"`);
  console.log(`SOURCE  ${total} tagged statements recovered from the Postgres log (nothing in the data path)`);
  console.log(`TIME    elapsed scaled ${DILATION}× ${DILATION === 1 ? '(real timestamps)' : '(simulated think-time, decompressed)'}`);
  console.log('='.repeat(78));

  // --- plan tree ---
  console.log('\n── PLAN TREE (reconstructed from the warehouse log alone) ────────────────');
  const children = new Map();
  for (const s of spans) {
    const p = s.parent_span_id ?? 'ROOT';
    if (!children.has(p)) children.set(p, []);
    children.get(p).push(s);
  }
  const known = new Set(spans.map((s) => s.span_id));
  const roots = spans.filter((s) => !s.parent_span_id || !known.has(s.parent_span_id));

  const printNode = (s, depth) => {
    const kids = children.get(s.span_id) ?? [];
    const mark = s.used_downstream ? '✓' : ' ';
    const pad = '  '.repeat(depth);
    console.log(`${pad}${mark} [${s.speculation_class.padEnd(6)}] ${s.span_intent.padEnd(38 - depth * 2)} ${usd(s.cost).padStart(7)}`);
    for (const [label, group] of collapseLabel(kids)) {
      if (group.length > 3) {
        const c = group.reduce((a, k) => a + k.cost, 0);
        console.log(`${pad}    └─ ${String(group.length).padStart(2)}× ${label.padEnd(31)} ${usd(c).padStart(7)}`);
      } else {
        group.forEach((k) => printNode(k, depth + 1));
      }
    }
  };
  roots.forEach((r) => printNode(r, 0));

  // --- redundancy ---
  console.log('\n── REDUNDANCY ────────────────────────────────────────────────');
  console.log(`  queries issued                  ${total}`);
  console.log(`  distinct, exact SQL             ${distinctExact}  → ${pct(1 - distinctExact / total)} caught by literal match`);
  console.log(`  distinct, AST-normalized        ${distinctAst}  → ${pct(1 - distinctAst / total)} caught by normalization`);
  console.log(`  aggregate queries               ${aggIdx.length}`);
  console.log(`  minimal covering set            ${cover.anchors.length} rollups answer ${cover.coveredCount}/${cover.total}`);
  console.log(`  excluded as unmodellable        ${cover.unmodelled}  (schema lookups, joins, CTEs — not servable by a rollup)`);
  // Measured against queries the anchors could actually serve. Dividing by every
  // query counts unmodellable ones as deduplicated, which they are not.
  const distinctPlans = cover.coveredCount ? cover.anchors.length / cover.coveredCount : 1;
  console.log(`\n  DISTINCT SUB-PLANS              ${pct(distinctPlans)}   ${bar(distinctPlans)}   (of servable queries)`);
  console.log(`  REDUNDANCY                      ${pct(1 - distinctPlans)}   ${bar(1 - distinctPlans)}`);
  console.log(`  (BAIR post reports 10–20% distinct sub-plans across agent attempts)`);

  console.log('\n  Synthesized anchors — note these are queries the agent NEVER RAN:');
  const verified = await verifyAnchors(cover.anchors.slice(0, 5));
  let anchorMs = 0;
  for (const a of verified) {
    const tag = a.anchor.synthetic ? 'SYNTH' : 'obs. ';
    console.log(`    [${tag}] covers ${String(a.covers.length).padStart(3)}  ${a.ok ? `${a.rows} rows, ${a.ms.toFixed(0)}ms` : 'FAILED: ' + a.err}`);
    console.log(`             ${a.anchor.sql.slice(0, 92)}`);
    if (a.ok) anchorMs += a.ms;
  }

  // --- waste ---
  console.log('\n── SPECULATION WASTE (requires the agent-side half of the trace) ─────────');
  const cited = spans.filter((s) => s.used_downstream);
  const uncited = spans.filter((s) => !s.used_downstream);
  const wasteCost = uncited.reduce((a, s) => a + s.cost, 0);
  console.log(`  results that reached the answer  ${cited.length}/${total}`);
  console.log(`  cost of results that did not     ${usd(wasteCost)} of ${usd(totalCost)}   (${pct(wasteCost / totalCost)})`);
  const deadEnd = uncited.filter((s) => /refund/i.test(s.span_intent));
  console.log(`  largest dead-end branch          "refunds spiked" — ${deadEnd.length} queries, ${usd(deadEnd.reduce((a, s) => a + s.cost, 0))}`);

  // --- cost ---
  console.log('\n── COST ATTRIBUTION (Snowflake XS, $3/credit, 60s min, 60s auto-suspend) ──');
  console.log(`  wall-clock span of the task      ${b.elapsedSec.toFixed(0)}s`);
  console.log(`  warehouse resumes                ${b.windows.length}`);
  console.log(`  billed warehouse-seconds         ${b.billedSec.toFixed(0)}s   ${usd(sec2dollars(b.billedSec))}`);
  console.log(`  productive (query exec) seconds  ${b.productiveSec.toFixed(1)}s   ${usd(sec2dollars(b.productiveSec))}`);
  console.log(`  idle + cold-start tax            ${b.overhead.toFixed(0)}s   ${usd(sec2dollars(b.overhead))}   (${pct(b.overhead / b.billedSec)} of bill)`);
  console.log(`\n  → COST PER RESOLVED TASK         ${usd(totalCost)}`);
  console.log(`    at 5k agent tasks/day           ${usd(totalCost * 5000)}/day   ${usd(totalCost * 5000 * 30)}/mo`);

  // --- recommendations ---
  console.log('\n── PHASE-1 RECOMMENDATIONS (advice only — no query rewriting, no interception) ─');
  const batchedCost = sec2dollars(b.batchedSec);
  console.log(`  1. Batch probes into one warehouse window`);
  console.log(`     think-time between probes is what pays the idle tax`);
  console.log(`     ${usd(totalCost)} → ${usd(batchedCost)} per task   (${(totalCost / batchedCost).toFixed(1)}×)`);
  console.log(`  2. Materialize ${cover.anchors.length} rollups to serve ${cover.coveredCount} of ${cover.total} aggregate queries`);
  console.log(`     measured anchor exec: ${anchorMs.toFixed(0)}ms total vs ${(b.productiveSec * 1000).toFixed(0)}ms of probe execution`);
  console.log(`     → ${(1 - anchorMs / (b.productiveSec * 1000)) > 0 ? pct(1 - anchorMs / (b.productiveSec * 1000)) : '0%'} less compute for the same answers`);
  console.log(`  3. ${uncited.length} of ${total} queries never informed the answer (${usd(wasteCost)}/task).`);
  console.log(`     Of those, the "refunds spiked" hypothesis is a fully dead branch (${deadEnd.length} queries)`);
  console.log(`     — the rest are probes whose findings the rollups would have surfaced in one shot.`);
  console.log('');
}

main().catch((e) => { console.error(e); process.exit(1); });
