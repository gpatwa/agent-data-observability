// AST-based query shape extraction, replacing the regex extractor.
//
// WHY THIS EXISTS: the regex version could not read what real agents write —
// positional `GROUP BY 1, 2`, `date_trunc('month', order_date)`, casts, or
// aliases — and silently produced garbage dimension names ("1", "2"). Every
// covering-set number computed on real agent SQL was untrustworthy as a result.
//
// DESIGN RULE: when this cannot confidently model a query, it returns null and
// the query is excluded from the covering set. An excluded query lowers the
// reported coverage; a mis-parsed one inflates it. Excluding is the honest
// failure, so anything with joins, subqueries, CTEs, HAVING, DISTINCT-select,
// window functions, or OR in the WHERE clause bails out.

// CJS package: the named export is not exposed to ESM, so take it off default.
import sqlParser from 'node-sql-parser';

const parser = new sqlParser.Parser();
const OPTS = { database: 'PostgreSQL' };

// Functions that bucket a column into a coarser grain. A rollup grouped by the
// underlying column can serve a query grouped by the bucket (days roll up into
// months); the reverse is never true.
const BUCKETING = new Set(['date_trunc', 'date', 'to_char', 'extract', 'date_part']);

const ADDITIVE = new Set(['SUM', 'COUNT', 'MIN', 'MAX']);

function exprKey(node) {
  if (!node) return null;
  switch (node.type) {
    case 'column_ref': {
      const c = node.column?.expr?.value ?? node.column;
      return typeof c === 'string' ? c.toLowerCase() : null;
    }
    case 'number': return String(node.value);
    case 'single_quote_string':
    case 'string':
    case 'double_quote_string': return `'${node.value}'`;
    case 'bool': return String(node.value);
    case 'null': return 'null';
    case 'function': {
      const name = (node.name?.name?.[0]?.value ?? node.name)?.toString().toLowerCase();
      const args = (node.args?.value ?? []).map(exprKey);
      if (args.some((a) => a === null)) return null;
      return `${name}(${args.join(',')})`;
    }
    case 'cast': {
      const inner = exprKey(node.expr);
      if (inner === null) return null;
      return `${inner}::${(node.target?.[0]?.dataType ?? node.target?.dataType ?? '?').toLowerCase()}`;
    }
    case 'binary_expr': {
      const l = exprKey(node.left); const r = exprKey(node.right);
      if (l === null || r === null) return null;
      return `${l} ${node.operator.toLowerCase()} ${r}`;
    }
    case 'expr_list':
      return `(${(node.value ?? []).map(exprKey).join(',')})`;
    case 'aggr_func': {
      const inner = node.args?.expr;
      const arg = inner?.type === 'star' ? '*' : exprKey(inner);
      if (arg === null) return null;
      const distinct = node.args?.distinct ? 'distinct ' : '';
      return `${node.name.toLowerCase()}(${distinct}${arg})`;
    }
    default: return null;
  }
}

// Split a WHERE tree on AND. Returns null if an OR appears — disjunction breaks
// the "anchor is no more restrictive than the query" reasoning entirely.
function conjuncts(node, acc = []) {
  if (!node) return acc;
  if (node.type === 'binary_expr' && node.operator === 'AND') {
    if (conjuncts(node.left, acc) === null) return null;
    if (conjuncts(node.right, acc) === null) return null;
    return acc;
  }
  if (node.type === 'binary_expr' && node.operator === 'OR') return null;
  acc.push(node);
  return acc;
}

// Walk the whole AST for constructs this module cannot model. Checking only the
// top level missed two: a window function is an `aggr_func` carrying `over`
// (not a `function`), and a subquery nested inside a WHERE predicate parsed
// into a filter key that looked valid. Both produced FALSE POSITIVES — a shape
// that looked modelled but described the wrong query, which is worse than
// declining. This scan is deliberately broad.
function hasUnmodellableNode(node, depth = 0) {
  if (node === null || typeof node !== 'object') return false;
  if (Array.isArray(node)) return node.some((n) => hasUnmodellableNode(n, depth));
  if (node.over) return true;                                // window function
  if (depth > 0 && node.type === 'select') return true;      // nested subquery
  for (const [k, v] of Object.entries(node)) {
    if (k === 'type' || k === 'operator') continue;
    if (hasUnmodellableNode(v, depth + 1)) return true;
  }
  return false;
}

export function extractShape(sql) {
  let ast;
  try {
    const parsed = parser.astify(sql, OPTS);
    // Multiple statements in one string is not an analytics query — and is the
    // shape an injection takes. Never model it.
    if (Array.isArray(parsed) && parsed.length > 1) return null;
    ast = Array.isArray(parsed) ? parsed[0] : parsed;
  } catch {
    return null; // unparseable — exclude rather than guess
  }
  if (!ast || ast.type !== 'select') return null;
  if (ast.with || ast.having || ast.window) return null;
  if (hasUnmodellableNode(ast)) return null;
  if (ast.distinct?.type) return null;
  if (!Array.isArray(ast.from) || ast.from.length !== 1) return null; // joins/subqueries
  const table = ast.from[0].table;
  if (!table || ast.from[0].expr) return null;

  // --- select list: measures vs dimensions --------------------------------
  const measures = [];
  const selectKeys = [];   // positional GROUP BY resolves against this
  const aliasToKey = new Map();

  for (const col of ast.columns ?? []) {
    const e = col.expr;
    if (!e) return null;
    if (e.type === 'aggr_func') {
      const k = exprKey(e);
      if (k === null) return null;
      measures.push(k);
      selectKeys.push(null);           // aggregates are not groupable positions
    } else if (e.type === 'star') {
      return null;                     // SELECT * is not an aggregate shape
    } else {
      const k = exprKey(e);
      if (k === null) return null;
      selectKeys.push(k);
      if (col.as) aliasToKey.set(String(col.as).toLowerCase(), k);
    }
  }
  if (!measures.length) return null;   // only aggregate queries participate
  if (ast.columns?.some((c) => c.expr?.type === 'function' && c.expr?.over)) return null;

  // --- group by -----------------------------------------------------------
  const groupby = [];
  const gbCols = ast.groupby?.columns ?? ast.groupby ?? [];
  for (const g of gbCols) {
    if (g?.type === 'number') {
      const k = selectKeys[Number(g.value) - 1];
      if (!k) return null;             // positional ref to an aggregate — bail
      groupby.push(k);
      continue;
    }
    let k = exprKey(g);
    if (k !== null && aliasToKey.has(k)) k = aliasToKey.get(k);
    if (k === null) return null;
    groupby.push(k);
  }

  // --- where --------------------------------------------------------------
  const rawConj = conjuncts(ast.where);
  if (rawConj === null) return null;   // OR present
  const filters = [];
  const eqCols = [];
  for (const c of rawConj) {
    const k = exprKey(c);
    if (k === null) return null;
    filters.push(k);
    if (c.type === 'binary_expr' && c.operator === '=') {
      const lhs = exprKey(c.left);
      if (lhs && /^[a-z_][a-z0-9_]*$/.test(lhs)) eqCols.push(lhs);
    }
  }

  return {
    table: String(table).toLowerCase(),
    measures: [...new Set(measures)].sort(),
    groupby: [...new Set(groupby)].sort(),
    filters: filters.sort(),
    eqCols: [...new Set(eqCols)],
  };
}

// --- subsumption ------------------------------------------------------------

function measureServable(B, m) {
  // count(distinct x) cannot be recomputed from a coarser rollup — distinct
  // counts do not sum. This is the classic wrong answer in view matching.
  if (/^count\(distinct /.test(m)) return B.measures.includes(m) && B.groupby.length === 0;
  const agg = m.match(/^([a-z_]+)\(/)?.[1]?.toUpperCase();
  if (agg && ADDITIVE.has(agg) && B.measures.includes(m)) return true;
  const avg = m.match(/^avg\((.+)\)$/);
  if (avg) {
    return B.measures.includes(`sum(${avg[1]})`) && B.measures.some((x) => /^count\(/.test(x) && !/distinct/.test(x));
  }
  return false;
}

// Can dimension `d` of the query be produced from the anchor's dimensions?
// Either the anchor grouped by it directly, or `d` is a bucketing function over
// a column the anchor grouped by (days -> months).
function dimensionDerivable(B, d) {
  if (B.groupby.includes(d)) return true;
  const fn = d.match(/^([a-z_]+)\(([^)]*)\)$/);
  if (!fn || !BUCKETING.has(fn[1])) return false;
  const inner = fn[2].split(',').map((s) => s.trim().replace(/^'.*'$/, '')).filter(Boolean);
  return inner.some((col) => B.groupby.includes(col));
}

export function subsumes(B, A) {
  if (!A || !B) return false;
  if (A.table !== B.table) return false;
  if (!A.measures.every((m) => measureServable(B, m))) return false;
  if (!A.groupby.every((g) => dimensionDerivable(B, g))) return false;
  // The anchor must be no more restrictive than the query.
  if (!B.filters.every((f) => A.filters.includes(f))) return false;
  // Any extra restriction the query has must be selectable out of the anchor's
  // result, which requires the anchor to have grouped by that column.
  for (const f of A.filters.filter((x) => !B.filters.includes(x))) {
    const col = f.match(/^([a-z_][a-z0-9_]*)\s*=/)?.[1];
    if (!col || !B.groupby.includes(col)) return false;
  }
  return true;
}

// --- candidate synthesis ----------------------------------------------------

function renderSQL(s) {
  const dims = s.groupby.join(', ');
  const sel = [dims, s.measures.join(', ')].filter(Boolean).join(', ');
  const where = s.filters.length ? ` where ${s.filters.join(' and ')}` : '';
  const grp = s.groupby.length ? ` group by ${dims}` : '';
  return `select ${sel} from ${s.table}${where}${grp}`;
}

export function synthesizeCandidates(shapes) {
  const byTable = new Map();
  for (const s of shapes) {
    if (!s) continue;
    if (!byTable.has(s.table)) byTable.set(s.table, []);
    byTable.get(s.table).push(s);
  }

  const out = [];
  for (const [table, group] of byTable) {
    const measures = new Set(['count(*)']);
    for (const s of group) {
      for (const m of s.measures) {
        if (/^count\(distinct /.test(m)) continue;  // not derivable; don't promise it
        const avg = m.match(/^avg\((.+)\)$/);
        if (avg) measures.add(`sum(${avg[1]})`);
        else measures.add(m);
      }
    }

    // Each query's grouping plus the columns it filtered on by equality —
    // lifting those filters into the grouping is what makes one rollup serve
    // many single-cell probes.
    const dimSets = new Map();
    for (const s of group) {
      const dims = [...new Set([...s.groupby, ...s.eqCols])].sort();
      dimSets.set(dims.join('|'), dims);
    }
    for (const dims of dimSets.values()) {
      out.push({ table, measures: [...measures].sort(), groupby: dims, filters: [], eqCols: [], synthetic: true });
    }
    const allDims = [...new Set([...dimSets.values()].flat())].sort();
    if (allDims.length) {
      out.push({ table, measures: [...measures].sort(), groupby: allDims, filters: [], eqCols: [], synthetic: true });
    }
  }
  return out.map((c) => ({ ...c, sql: renderSQL(c) }));
}

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
      if (cov.length > bestCover.length) { best = cand; bestCover = cov; }
    }
    if (!best || !bestCover.length) break;
    anchors.push({ anchor: best, covers: bestCover });
    bestCover.forEach((j) => covered.add(j));
  }
  return {
    anchors,
    coveredCount: covered.size,
    total: idxs.length,
    unmodelled: shapes.length - idxs.length,
    uncovered: idxs.filter((i) => !covered.has(i)),
  };
}
