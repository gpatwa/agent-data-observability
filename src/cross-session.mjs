// Phase 0b — cross-session redundancy.
//
// The single-agent run killed the INTRA-session redundancy thesis: one agent,
// one question, seven well-targeted queries, nothing to deduplicate. This tests
// the version that survives it:
//
//   Many analysts' agents hit the same warehouse asking overlapping questions.
//   How much of the corpus is answerable from a shared set of rollups?
//
// That framing needs no agent to be wasteful — only for different people to ask
// related questions about the same tables, which is what a company is.
//
//   node src/cross-session.mjs [--concurrency 4]

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runAgent } from './real-agent.mjs';
import { reconstruct, bill, sec2dollars } from './trace.mjs';
import { coveringSet } from './fingerprint.mjs';

const ROOT = new URL('..', import.meta.url);
const LOG = fileURLToPath(new URL('.pgdata/pglog/queries.log', ROOT));
const OUT = fileURLToPath(new URL('out/', ROOT));

// Eight questions a real analytics team might ask of the same warehouse in a
// week. Deliberately overlapping — same tables, same date range, same
// dimensions — without being duplicates of each other.
export const QUESTIONS = [
  ['drop',     'Why did revenue drop in July 2026 compared to June 2026?'],
  ['growth',   'Which region had the strongest revenue growth in July 2026?'],
  ['aov',      'What is the average order value by channel for July 2026, and how does it compare to June?'],
  ['anomaly',  'Is there any day in July 2026 where order volume or revenue behaved unusually?'],
  ['emea',     'Which channel contributes the most revenue in EMEA?'],
  ['refunds',  'How much did refunds cost us in June and July 2026, and is the rate rising?'],
  ['mix',      'Break down July 2026 revenue by region and channel and flag anything that looks anomalous.'],
  ['daily',    'What was total revenue per day in July 2026?'],
];

async function pool(items, n, fn) {
  const results = [];
  let i = 0;
  await Promise.all(
    Array.from({ length: Math.min(n, items.length) }, async () => {
      while (i < items.length) {
        const idx = i++;
        results[idx] = await fn(items[idx]);
      }
    })
  );
  return results;
}

const pct = (n) => `${(n * 100).toFixed(1)}%`;
const usd = (n) => `$${n.toFixed(n < 1 ? 3 : 2)}`;

async function main() {
  const cIdx = process.argv.indexOf('--concurrency');
  const concurrency = cIdx > -1 ? Number(process.argv[cIdx + 1]) : 4;
  const skipRun = process.argv.includes('--analyze-only');

  if (!skipRun) {
    writeFileSync(LOG, '');
    console.log(`==> launching ${QUESTIONS.length} agents (concurrency ${concurrency})\n`);
    const runs = await pool(QUESTIONS, concurrency, async ([tag, question]) => {
      const r = await runAgent({ question, tag });
      console.log(
        `  ${r.ok ? '✓' : '✗'} ${tag.padEnd(9)} ${String(r.queries).padStart(2)} queries · ` +
        `grounded ${r.grounded ?? 0}/${r.queries} · ${r.cost != null ? usd(r.cost) : 'n/a'}`
      );
      return r;
    });
    const totalCost = runs.reduce((a, r) => a + (r.cost ?? 0), 0);
    console.log(`\n==> ${runs.filter((r) => r.ok).length}/${runs.length} succeeded, LLM cost ${usd(totalCost)}\n`);
  }

  // ---- analysis -----------------------------------------------------------
  const eventPaths = QUESTIONS.map(([tag]) => `${OUT}${tag}-events.jsonl`).filter(existsSync);
  const spans = reconstruct(LOG, eventPaths);
  if (!spans.length) {
    console.error('no tagged spans found — did the agents run?');
    process.exit(1);
  }

  const byTrace = new Map();
  for (const s of spans) {
    if (!byTrace.has(s.trace_id)) byTrace.set(s.trace_id, []);
    byTrace.get(s.trace_id).push(s);
  }

  console.log('='.repeat(78));
  console.log(`CROSS-SESSION ANALYSIS — ${byTrace.size} agent sessions, ${spans.length} queries`);
  console.log('='.repeat(78));

  console.log('\n── PER-SESSION ───────────────────────────────────────────────');
  console.log(`  ${'session'.padEnd(10)} ${'queries'.padStart(7)} ${'anchors'.padStart(7)} ${'grounded'.padStart(8)}`);
  let perTraceAnchors = 0;
  for (const [, group] of byTrace) {
    const cover = coveringSet(group.map((s) => s.shape));
    perTraceAnchors += cover.anchors.length;
    const tag = group.find((s) => s.agent_id)?.agent_id?.replace('claude-code-', '') ?? '?';
    const grounded = group.filter((s) => s.grounded).length;
    console.log(
      `  ${tag.padEnd(10)} ${String(group.length).padStart(7)} ${String(cover.anchors.length).padStart(7)} ${String(grounded).padStart(8)}`
    );
  }

  // The whole point: one covering set computed over every session at once.
  const global = coveringSet(spans.map((s) => s.shape));
  const b = bill(spans, 1);

  console.log('\n── CROSS-SESSION REDUNDANCY ──────────────────────────────────');
  console.log(`  queries across all sessions      ${spans.length}`);
  console.log(`  anchors needed per-session (sum) ${perTraceAnchors}`);
  console.log(`  anchors needed GLOBALLY          ${global.anchors.length}  covering ${global.coveredCount}/${global.total}`);
  console.log(`  excluded as unmodellable         ${global.unmodelled}  (parser declined rather than guess)`);
  console.log(`  modelled but uncovered           ${global.uncovered.length}`);
  const crossRedundancy = perTraceAnchors > 0 ? 1 - global.anchors.length / perTraceAnchors : 0;
  console.log(`\n  CROSS-SESSION REDUNDANCY         ${pct(crossRedundancy)}`);
  console.log(`  (share of per-session rollups made unnecessary by sharing across sessions)`);
  // Dedup must be measured against the queries the anchors could actually
  // serve. Dividing by every raw query counts the 12 unmodellable ones as
  // "deduplicated", which they are not — that inflated this number to 51%.
  console.log(`  dedup vs COVERED queries         ${pct(1 - global.anchors.length / Math.max(global.coveredCount, 1))}`
    + `   (${global.anchors.length} anchors serve ${global.coveredCount})`);
  console.log(`  dedup vs MODELLED queries        ${pct(1 - global.anchors.length / Math.max(global.total, 1))}`);

  console.log('\n  Shared anchors — each serves queries from multiple sessions:');
  for (const a of global.anchors.slice(0, 6)) {
    const sessions = new Set(a.covers.map((i) => spans[i].trace_id)).size;
    console.log(
      `    covers ${String(a.covers.length).padStart(3)} queries across ${sessions} session(s)  [${a.anchor.synthetic ? 'SYNTH' : 'obs. '}]`
    );
    console.log(`      ${a.anchor.sql.slice(0, 92)}`);
  }

  console.log('\n── COST ──────────────────────────────────────────────────────');
  const totalCost = sec2dollars(b.billedSec);
  console.log(`  billed warehouse-seconds         ${b.billedSec.toFixed(0)}s   ${usd(totalCost)}`);
  console.log(`  productive (query exec) seconds  ${b.productiveSec.toFixed(1)}s   ${usd(sec2dollars(b.productiveSec))}`);
  console.log(`  idle + cold-start tax            ${b.overhead.toFixed(0)}s   ${usd(sec2dollars(b.overhead))}   (${pct(b.overhead / b.billedSec)})`);
  console.log(`  cost per session                 ${usd(totalCost / byTrace.size)}`);

  console.log('\n── CITATION VERIFICATION (all sessions) ──────────────────────');
  const claimed = spans.filter((s) => s.used_downstream).length;
  const grounded = spans.filter((s) => s.grounded).length;
  console.log(`  self-reported as cited           ${claimed}/${spans.length}`);
  console.log(`  verified grounded in the answer  ${grounded}/${spans.length}`);
  console.log('');

  writeFileSync(`${OUT}cross-session-summary.json`, JSON.stringify({
    sessions: byTrace.size,
    queries: spans.length,
    perTraceAnchors,
    globalAnchors: global.anchors.length,
    crossRedundancy,
    claimed, grounded,
    billedSec: b.billedSec, productiveSec: b.productiveSec,
  }, null, 2));
}

main();
