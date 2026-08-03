// Replication of the actual published measurement.
//
// WHAT I GOT WRONG: every earlier condition in this repo gave each agent a
// DIFFERENT question, then reported that the redundancy thesis "did not
// reproduce". The published claim is not about different questions. From the
// EPIC Lab paper (arXiv 2509.00997):
//
//   BIRD text-to-SQL benchmark, 50 independent attempts PER TASK with
//   GPT-4o-mini; redundancy is "the proportion of distinct sub-expressions
//   relative to total sub-expressions across multiple agent attempts", and
//   "the number of distinct sub-plans of each size is often a small fraction
//   of less than 10-20% of the total".
//
// So the setup is N agents attempting the SAME task, and the unit is
// SUB-EXPRESSIONS, not whole queries. This script measures both, at both
// levels, so the comparison is finally like-for-like.
//
//   node src/same-task.mjs [--attempts 8] [--question "..."] [--analyze-only]

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runAgent } from './real-agent.mjs';
import { reconstruct } from './trace.mjs';
import { extractShape, coveringSet } from './fingerprint.mjs';
import { astHash, exactHash } from './fingerprint.mjs';

const ROOT = new URL('..', import.meta.url);
const LOG = fileURLToPath(new URL('.pgdata/pglog/queries.log', ROOT));
const OUT = fileURLToPath(new URL('out/', ROOT));

const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i > -1 ? process.argv[i + 1] : d; };
const ATTEMPTS = Number(arg('attempts', 8));
const QUESTION = arg('question', 'Why did revenue drop in July 2026 compared to June 2026?');
const pct = (n) => `${(n * 100).toFixed(1)}%`;

async function pool(items, n, fn) {
  const out = [];
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k]); }
  }));
  return out;
}

// --- sub-expression decomposition -----------------------------------------
// The paper counts sub-plans by size. This approximates that: each aggregate
// query is broken into the pieces a plan would share — the filtered scan, the
// grouping, the individual measures, and the whole aggregate — so overlap can
// be counted at each level rather than only for identical whole queries.
function subExpressions(shape) {
  if (!shape) return [];
  const out = [];
  const scan = `scan(${shape.table})`;
  out.push({ size: 1, kind: 'scan', key: scan });
  for (const f of shape.filters) out.push({ size: 1, kind: 'filter', key: `${scan}|${f}` });
  out.push({ size: 2, kind: 'filtered_scan', key: `${scan}|${shape.filters.join('&')}` });
  for (const m of shape.measures) {
    out.push({ size: 2, kind: 'measure', key: `${scan}|${m}` });
  }
  if (shape.groupby.length) {
    out.push({ size: 3, kind: 'grouping', key: `${scan}|by:${shape.groupby.join(',')}` });
  }
  out.push({
    size: 4, kind: 'full_agg',
    key: `${scan}|${shape.filters.join('&')}|by:${shape.groupby.join(',')}|${shape.measures.join(',')}`,
  });
  return out;
}

function ratio(list) {
  const total = list.length;
  const distinct = new Set(list).size;
  return { total, distinct, distinctPct: total ? distinct / total : 1 };
}

async function main() {
  const analyzeOnly = process.argv.includes('--analyze-only');
  const tags = Array.from({ length: ATTEMPTS }, (_, i) => `same${i + 1}`);

  if (!analyzeOnly) {
    writeFileSync(LOG, '');
    console.log(`==> ${ATTEMPTS} independent attempts at ONE task`);
    console.log(`    "${QUESTION}"\n`);
    const runs = await pool(tags, 4, async (tag) => {
      const r = await runAgent({ question: QUESTION, tag });
      console.log(`  ${r.ok ? '✓' : '✗'} ${tag.padEnd(8)} ${String(r.queries).padStart(2)} queries · $${(r.cost ?? 0).toFixed(3)}`);
      return r;
    });
    console.log(`\n==> LLM cost $${runs.reduce((a, r) => a + (r.cost ?? 0), 0).toFixed(2)}\n`);
  }

  const eventPaths = tags.map((t) => `${OUT}${t}-events.jsonl`).filter(existsSync);
  const spans = reconstruct(LOG, eventPaths);
  if (!spans.length) { console.error('no spans found — run without --analyze-only first'); process.exit(1); }

  const byTrace = new Map();
  for (const s of spans) {
    if (!byTrace.has(s.trace_id)) byTrace.set(s.trace_id, []);
    byTrace.get(s.trace_id).push(s);
  }

  console.log('='.repeat(72));
  console.log(`SAME-TASK REPLICATION — ${byTrace.size} attempts, ${spans.length} queries`);
  console.log('='.repeat(72));
  console.log(`\n  queries per attempt: ${[...byTrace.values()].map((g) => g.length).join(', ')}`);

  // --- whole-query level (what this repo measured before) -----------------
  console.log('\n── WHOLE-QUERY LEVEL (what this repo measured previously) ─────');
  const ex = ratio(spans.map((s) => s.exact));
  const ast = ratio(spans.map((s) => s.ast));
  console.log(`  exact SQL          ${ex.distinct}/${ex.total} distinct   ${pct(ex.distinctPct)}`);
  console.log(`  AST-normalized     ${ast.distinct}/${ast.total} distinct   ${pct(ast.distinctPct)}`);
  const shapes = spans.map((s) => s.shape);
  const cover = coveringSet(shapes);
  console.log(`  covering set       ${cover.anchors.length} anchors serve ${cover.coveredCount}/${cover.total} servable`);
  console.log(`  (${cover.unmodelled} queries unmodellable — joins/CTEs/schema lookups)`);

  // --- sub-expression level (what the paper measured) ----------------------
  console.log('\n── SUB-EXPRESSION LEVEL (what the paper measured) ─────────────');
  const all = spans.flatMap((s) => subExpressions(s.shape));
  if (!all.length) {
    console.log('  no modellable aggregate queries — cannot decompose');
  } else {
    console.log(`  ${'size'.padEnd(6)} ${'kind'.padEnd(15)} ${'total'.padStart(6)} ${'distinct'.padStart(9)} ${'distinct %'.padStart(11)}`);
    const bySize = new Map();
    for (const e of all) {
      const k = `${e.size}|${e.kind}`;
      if (!bySize.has(k)) bySize.set(k, []);
      bySize.get(k).push(e.key);
    }
    for (const [k, keys] of [...bySize.entries()].sort()) {
      const [size, kind] = k.split('|');
      const r = ratio(keys);
      console.log(`  ${size.padEnd(6)} ${kind.padEnd(15)} ${String(r.total).padStart(6)} ${String(r.distinct).padStart(9)} ${pct(r.distinctPct).padStart(11)}`);
    }
    const overall = ratio(all.map((e) => e.key));
    console.log(`\n  ALL SUB-EXPRESSIONS  ${overall.distinct}/${overall.total} distinct = ${pct(overall.distinctPct)}`);
    console.log(`  paper reports: "often a small fraction of less than 10-20% of the total"`);
    const verdict = overall.distinctPct <= 0.20 ? 'REPRODUCES the published range'
      : overall.distinctPct <= 0.40 ? 'partially — above the published range but substantial sharing'
      : 'does NOT reproduce at this scale';
    console.log(`  → ${verdict}`);
  }

  console.log('\n  Caveats: this is an approximation of plan sub-expressions from the');
  console.log('  query shape, not a real plan decomposition; the paper used BIRD with');
  console.log(`  50 attempts on GPT-4o-mini, this is ${byTrace.size} attempts on a frontier model.`);
  console.log('');
}

main();
