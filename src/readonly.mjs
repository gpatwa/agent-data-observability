// Read-only enforcement for agent-issued SQL.
//
// Kept in its own side-effect-free module so it is testable without starting an
// MCP server or opening a database connection.
//
// HISTORY: the original guard was /^\s*(select|with)\b/ — it inspected only the
// START of the string. `select 1; drop table x` passed it, and node-postgres
// executes multi-statement strings via the simple query protocol, so the DROP
// ran. Verified against a canary table: it was dropped. That hole is reachable
// by any prompt injection that reaches the agent.
//
// This is the second line of defence, not the first. The first is a database
// role with SELECT-only grants. An application-layer allowlist in front of a
// read-write connection is not a security boundary.

import sqlParser from 'node-sql-parser';

const parser = new sqlParser.Parser();

/** @returns {string|null} a refusal message, or null if the SQL is permitted. */
export function readOnlyRefusal(sql) {
  if (typeof sql !== 'string' || !sql.trim()) return 'Empty query.';
  let parsed;
  try {
    parsed = parser.astify(sql, { database: 'PostgreSQL' });
  } catch {
    return 'Could not parse that SQL. Only a single read-only SELECT is permitted.';
  }
  const stmts = Array.isArray(parsed) ? parsed : [parsed];
  if (stmts.length !== 1) return 'Only one statement per call is permitted.';
  if (stmts[0]?.type !== 'select') return 'Only SELECT queries are permitted.';
  return null;
}
