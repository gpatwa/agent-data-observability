// Three-tier fingerprinting.
//
//   1. exact      — literal SQL. Catches dumb retries.
//   2. ast_hash   — normalized. Catches cosmetic variation.
//   3. subsumption— can query A be answered from query B's result?
//
// NOTE ON RIGOUR: tier 2/3 here use a tokenizer + a structured extractor for
// the common aggregate shape, NOT a real SQL parser. That is the correct
// shortcut for a prototype whose job is to size the prize; a product needs a
// real parser (sqlglot / ZetaSQL / Calcite) behind the same interface.

import { createHash } from 'node:crypto';

const sha = (s) => createHash('sha1').update(s).digest('hex').slice(0, 12);

export const exactHash = (sql) => sha(sql.trim());

function stripComments(sql) {
  return sql.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/--[^\n]*/g, ' ');
}

// Split a boolean expression on top-level AND (parenthesis-aware).
function splitConjuncts(expr) {
  const parts = [];
  let depth = 0;
  let buf = '';
  const toks = expr.split(/(\(|\)|\s+and\s+)/i);
  for (const t of toks) {
    if (t === '(') depth++;
    if (t === ')') depth--;
    if (/^\s+and\s+$/i.test(t) && depth === 0) {
      parts.push(buf.trim());
      buf = '';
    } else {
      buf += t;
    }
  }
  if (buf.trim()) parts.push(buf.trim());
  return parts.filter(Boolean);
}

export function normalize(sql) {
  let s = stripComments(sql).replace(/\s+/g, ' ').trim().replace(/;$/, '');
  s = s.toLowerCase();

  // Normalize aliases: `from orders o` / `from orders as o` -> drop the alias,
  // then rewrite `o.` -> `orders.` so alias choice stops mattering.
  const aliasRe = /\b(from|join)\s+([a-z_][a-z0-9_]*)\s+(?:as\s+)?([a-z_][a-z0-9_]*)\b(?!\s*\()/g;
  const aliases = {};
  s = s.replace(aliasRe, (m, kw, table, alias) => {
    const reserved = ['where', 'group', 'order', 'limit', 'on', 'join', 'left', 'inner', 'having'];
    if (reserved.includes(alias)) return m;
    aliases[alias] = table;
    return `${kw} ${table}`;
  });
  for (const [alias, table] of Object.entries(aliases)) {
    s = s.replace(new RegExp(`\\b${alias}\\.`, 'g'), `${table}.`);
  }

  // Sort top-level WHERE conjuncts so predicate order stops mattering.
  s = s.replace(/\bwhere\b(.*?)(?=\bgroup by\b|\border by\b|\blimit\b|\bhaving\b|$)/i, (m, pred) => {
    const sorted = splitConjuncts(pred.trim()).sort().join(' and ');
    return `where ${sorted} `;
  });

  // Sort select list and group-by list.
  s = s.replace(/^select\s+(.*?)\s+from\b/i, (m, cols) =>
    `select ${cols.split(',').map((c) => c.trim()).sort().join(', ')} from`);
  s = s.replace(/\bgroup by\s+(.*?)(?=\border by\b|\blimit\b|\bhaving\b|$)/i, (m, cols) =>
    `group by ${cols.split(',').map((c) => c.trim()).sort().join(', ')} `);

  return s.replace(/\s+/g, ' ').trim();
}

export const astHash = (sql) => sha(normalize(sql));

// ---------------------------------------------------------------------------
// Structured extraction for subsumption

const AGG_RE = /\b(sum|count|avg|min|max)\s*\(\s*([^)]*?)\s*\)/g;

export function extractShape(sql) {
  const s = normalize(sql);
  const from = s.match(/\bfrom\s+([a-z_][a-z0-9_]*)/);
  if (!from) return null;
  const table = from[1];

  const selectPart = (s.match(/^select\s+(.*?)\s+from\b/) || [])[1] || '';
  const measures = [];
  let m;
  AGG_RE.lastIndex = 0;
  while ((m = AGG_RE.exec(selectPart))) {
    measures.push(`${m[1]}(${m[2].replace(/^.*\./, '')})`);
  }
  if (!measures.length) return null; // only aggregate queries participate

  const gbPart = (s.match(/\bgroup by\s+(.*?)(?=\border by\b|\blimit\b|\bhaving\b|$)/) || [])[1] || '';
  const groupby = gbPart
    .split(',')
    .map((c) => c.trim().replace(/^.*\./, ''))
    .filter(Boolean)
    .sort();

  const wherePart = (s.match(/\bwhere\b(.*?)(?=\bgroup by\b|\border by\b|\blimit\b|\bhaving\b|$)/) || [])[1] || '';
  const filters = splitConjuncts(wherePart.trim())
    .map((f) => f.replace(/([a-z_][a-z0-9_]*)\./g, '').trim())
    .sort();

  // Columns constrained by equality — these can be recovered from a coarser
  // query IF that query groups by them.
  const eqCols = filters
    .map((f) => (f.match(/^([a-z_][a-z0-9_]*)\s*=/) || [])[1])
    .filter(Boolean);

  return { table, measures: measures.sort(), groupby, filters, eqCols };
}

const additive = (m) => /^(sum|count|min|max)\(/.test(m);

// A measure of A is servable from B if B carries it directly, or if it is an
// avg() that can be reconstructed from B's sum + count.
function measureServable(B, m) {
  if (additive(m) && B.measures.includes(m)) return true;
  const avg = m.match(/^avg\((.+)\)$/);
  if (avg) return B.measures.includes(`sum(${avg[1]})`) && B.measures.some((x) => /^count\(/.test(x));
  return false;
}

// Can A be computed from B's result set?
export function subsumes(B, A) {
  if (!A || !B) return false;
  if (A.table !== B.table) return false;
  if (!A.measures.every((m) => measureServable(B, m))) return false;
  // A's grouping must be derivable from B's grouping.
  if (!A.groupby.every((g) => B.groupby.includes(g))) return false;
  // B must be no more restrictive than A.
  if (!B.filters.every((f) => A.filters.includes(f))) return false;
  // Any filter A has that B lacks must be an equality on a column B grouped by,
  // so the rows can be selected out of B's result.
  const extra = A.filters.filter((f) => !B.filters.includes(f));
  for (const f of extra) {
    const col = (f.match(/^([a-z_][a-z0-9_]*)\s*=/) || [])[1];
    if (!col || !B.groupby.includes(col)) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Candidate synthesis
//
// The anchors that matter are usually queries NOBODY RAN. An agent that fires
// 31 per-day probes never issues the `GROUP BY order_date` rollup that would
// have answered all 31 — so picking anchors from observed queries finds almost
// nothing. Instead, synthesize candidates by lifting each query's equality
// filters up into the GROUP BY.

function renderSQL(shape) {
  const dims = shape.groupby.join(', ');
  const sel = [dims, shape.measures.join(', ')].filter(Boolean).join(', ');
  const where = shape.filters.length ? ` where ${shape.filters.join(' and ')}` : '';
  const grp = shape.groupby.length ? ` group by ${dims}` : '';
  return `select ${sel} from ${shape.table}${where}${grp}`;
}

export function synthesizeCandidates(shapes) {
  const byTable = new Map();
  for (const s of shapes) {
    if (!s) continue;
    if (!byTable.has(s.table)) byTable.set(s.table, []);
    byTable.get(s.table).push(s);
  }

  const candidates = [];
  for (const [table, group] of byTable) {
    // Union every measure anyone asked for, expanding avg -> sum + count.
    const measures = new Set(['count(*)']);
    for (const s of group) {
      for (const m of s.measures) {
        const avg = m.match(/^avg\((.+)\)$/);
        if (avg) measures.add(`sum(${avg[1]})`);
        else measures.add(m);
      }
    }

    // Distinct dimension sets: each query's grouping plus the columns it
    // filtered on by equality (those become dimensions in the rollup).
    const dimSets = new Map();
    for (const s of group) {
      const dims = [...new Set([...s.groupby, ...s.eqCols])].sort();
      dimSets.set(dims.join('|'), dims);
    }

    // Residual (non-equality) filters, e.g. date ranges, kept as an envelope.
    const residual = new Set();
    for (const s of group) {
      for (const f of s.filters) {
        if (!/^[a-z_][a-z0-9_]*\s*=/.test(f)) residual.add(f);
      }
    }

    for (const dims of dimSets.values()) {
      // Unfiltered variant: maximally reusable.
      candidates.push({ table, measures: [...measures].sort(), groupby: dims, filters: [], eqCols: [], synthetic: true });
    }
    // Also offer the union-of-all-dims cube.
    const allDims = [...new Set([...dimSets.values()].flat())].sort();
    if (allDims.length) {
      candidates.push({ table, measures: [...measures].sort(), groupby: allDims, filters: [], eqCols: [], synthetic: true });
    }
    void residual;
  }
  return candidates.map((c) => ({ ...c, sql: renderSQL(c) }));
}

// Greedy set cover over synthesized candidates (plus observed queries, in case
// an already-issued query is the best anchor).
export function coveringSet(shapes) {
  const idxs = shapes.map((s, i) => i).filter((i) => shapes[i]);
  const pool = [
    ...synthesizeCandidates(shapes),
    ...idxs.map((i) => ({ ...shapes[i], synthetic: false, sql: renderSQL(shapes[i]) })),
  ];

  const covered = new Set();
  const anchors = [];
  while (covered.size < idxs.length) {
    let best = null;
    let bestCover = [];
    for (const cand of pool) {
      const cov = idxs.filter((j) => !covered.has(j) && subsumes(cand, shapes[j]));
      if (cov.length > bestCover.length) {
        best = cand;
        bestCover = cov;
      }
    }
    if (!best || !bestCover.length) break;
    anchors.push({ anchor: best, covers: bestCover });
    bestCover.forEach((j) => covered.add(j));
  }
  return { anchors, coveredCount: covered.size, total: idxs.length, uncovered: idxs.filter((i) => !covered.has(i)) };
}
