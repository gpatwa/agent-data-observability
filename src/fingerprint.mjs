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
// Query shape, subsumption and candidate synthesis now live in shape.mjs,
// which parses SQL properly instead of pattern-matching it. Re-exported here so
// callers keep a single import surface.
//
// The normalize()/astHash() functions above remain regex-based on purpose: they
// only need to decide whether two queries are *textually* the same modulo
// cosmetics, and they are measured at ~0-2% hit rate — nothing depends on them
// being exact. Everything that feeds a redundancy number goes through shape.mjs.

export { extractShape, subsumes, synthesizeCandidates, coveringSet } from './shape.mjs';
