// MCP server exposing Snowflake to a real agent, with trace context carried in
// QUERY_TAG rather than a SQL comment.
//
// Compared to the Postgres server this is strictly less machinery: no log file,
// no log parser, no span reconstruction. Snowflake records the tag against the
// query itself and reports credits for it.
//
// Default dataset is SNOWFLAKE_SAMPLE_DATA.TPCH_SF1, which every trial account
// has. That matters for validity: TPC-H has no planted anomaly, so an agent
// must do real analysis rather than find an answer someone hid for it — which
// was the weakest point of every condition run so far.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { appendFileSync } from 'node:fs';
import { createHash, randomBytes } from 'node:crypto';
import { connect, execute, setTag } from './snowflake.mjs';
import { readOnlyRefusal } from './readonly.mjs';

const EVENTS_PATH = process.env.TRACE_EVENTS_PATH
  ? new URL(`file://${process.env.TRACE_EVENTS_PATH}`)
  : new URL('../out/snowflake-events.jsonl', import.meta.url);
const TRACE_ID = randomBytes(8).toString('hex');
const AGENT_ID = process.env.AGENT_ID ?? 'snowflake-analyst';
const MODEL_ID = process.env.AGENT_MODEL ?? 'claude-opus-5';

const conn = await connect();
// Belt and braces: the guard below refuses non-SELECT, and the session cannot
// write anyway. A production deployment would use a role with SELECT-only
// grants rather than relying on either.
await execute(conn, 'ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 120');

function scalarValues(rows) {
  const out = new Set();
  for (const row of rows.slice(0, 200)) {
    for (const v of Object.values(row)) {
      if (v == null) continue;
      if (v instanceof Date) { out.add(v.toISOString().slice(0, 10)); continue; }
      const s = typeof v === 'string' ? v : String(v);
      if (/^-?\d+(\.\d+)?$/.test(s.trim())) out.add(Number(s));
      else if (s.length <= 64) out.add(s);
    }
    if (out.size > 400) break;
  }
  return [...out];
}

let seq = 0;
const spansByLabel = new Map();

const server = new Server(
  { name: 'traced-snowflake', version: '0.1.0' },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'run_sql',
    description:
      'Run a read-only SQL query against a Snowflake analytics warehouse. ' +
      'Returns up to 50 rows as JSON plus a query id you can reference later. ' +
      'The database is the TPC-H sample schema: customer, orders, lineitem, part, ' +
      'partsupp, supplier, nation, region. Use information_schema to inspect columns.',
    inputSchema: {
      type: 'object',
      properties: {
        sql: { type: 'string', description: 'The SELECT statement to run.' },
        intent: { type: 'string', description: 'One short phrase describing what you are trying to learn.' },
        follows_from: { type: 'string', description: 'Optional query id (e.g. "q3") whose result prompted this one.' },
      },
      required: ['sql', 'intent'],
    },
  }],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== 'run_sql') {
    return { content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }], isError: true };
  }
  const { sql, intent, follows_from } = req.params.arguments ?? {};

  const refusal = readOnlyRefusal(sql ?? '');
  if (refusal) return { content: [{ type: 'text', text: refusal }], isError: true };

  const label = `q${++seq}`;
  const span = {
    trace_id: TRACE_ID,
    span_id: randomBytes(6).toString('hex'),
    parent_span_id: follows_from ? spansByLabel.get(follows_from) ?? null : null,
    agent_id: AGENT_ID,
    model_id: MODEL_ID,
    span_intent: intent ?? label,
    speculation_class: follows_from ? 'refine' : 'probe',
  };
  spansByLabel.set(label, span.span_id);

  let rows = [];
  let error = null;
  let queryId = null;
  const t0 = process.hrtime.bigint();
  try {
    await setTag(conn, span);            // native trace context — no SQL comment
    const res = await execute(conn, sql);
    rows = res.rows;
    queryId = res.queryId;               // joins to ACCOUNT_USAGE later
  } catch (e) {
    error = e.message.split('\n')[0];
  }
  const clientMs = Number(process.hrtime.bigint() - t0) / 1e6;

  appendFileSync(EVENTS_PATH, JSON.stringify({
    trace_id: span.trace_id, span_id: span.span_id, parent_span_id: span.parent_span_id,
    label, speculation_class: span.speculation_class, span_intent: span.span_intent,
    snowflake_query_id: queryId,
    result_hash: createHash('sha1').update(JSON.stringify(rows)).digest('hex').slice(0, 12),
    rows: rows.length, client_ms: clientMs, values: scalarValues(rows), error,
  }) + '\n');

  if (error) return { content: [{ type: 'text', text: `[${label}] SQL error: ${error}` }], isError: true };
  const shown = rows.slice(0, 50);
  return {
    content: [{
      type: 'text',
      text: `[${label}] ${rows.length} row(s)${rows.length > 50 ? ' (showing first 50)' : ''}\n` +
            JSON.stringify(shown, null, 1),
    }],
  };
});

await server.connect(new StdioServerTransport());
