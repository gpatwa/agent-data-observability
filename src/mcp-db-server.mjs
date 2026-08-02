// MCP server exposing the traced warehouse to a real LLM agent.
//
// This is the production shape of the middleware: the agent talks to a tool,
// the tool injects trace context as a SQL comment, and the warehouse logs it.
// The agent never sees the trace machinery.
//
// Lineage here is AGENT-DECLARED rather than harness-assigned: the tool schema
// asks the model for `intent` and `follows_from`, so the plan tree is the
// agent's own account of its reasoning, not our reconstruction of it.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import pg from 'pg';
import { readOnlyRefusal } from './readonly.mjs';
import { appendFileSync } from 'node:fs';
import { createHash, randomBytes } from 'node:crypto';
import { serializeContext } from './context.mjs';
import { PG } from './config.mjs';

const EVENTS_PATH = process.env.TRACE_EVENTS_PATH
  ? new URL(`file://${process.env.TRACE_EVENTS_PATH}`)
  : new URL('../out/agent-events.jsonl', import.meta.url);
const TRACE_ID = randomBytes(8).toString('hex');
const AGENT_ID = process.env.AGENT_ID ?? 'claude-code-analyst';
const MODEL_ID = process.env.AGENT_MODEL ?? 'claude-opus-5';
const QUESTION = process.env.TRACE_QUESTION ?? null;

// Scalar values from a result set, used later to verify — rather than trust —
// which query results actually reached the agent's final answer.
function scalarValues(rows) {
  const out = new Set();
  for (const row of rows.slice(0, 200)) {
    for (const v of Object.values(row)) {
      if (v == null) continue;
      if (v instanceof Date) { out.add(v.toISOString().slice(0, 10)); continue; }
      if (typeof v === 'number') { out.add(v); continue; }
      // pg returns numeric/bigint columns as STRINGS. Keeping them as strings
      // makes them substring-matched later, which never fires against rounded
      // prose ("69.33" vs "69.331240..."). Coerce to Number so the verifier
      // compares them numerically, with tolerance.
      const s = typeof v === 'string' ? v : String(v);
      if (/^-?\d+(\.\d+)?$/.test(s.trim())) out.add(Number(s));
      else if (s.length <= 64) out.add(s);
    }
    if (out.size > 400) break;
  }
  return [...out];
}

const client = new pg.Client(PG);
await client.connect();
// Defence in depth: even if the parser is fooled, the session cannot write.
// A production deployment would use a role with SELECT-only grants instead of
// relying on a session setting the agent could in principle reset.
await client.query('set default_transaction_read_only = on');
await client.query('set statement_timeout = 30000');

let seq = 0;
const spansByLabel = new Map(); // "q3" -> span_id

const server = new Server(
  { name: 'traced-warehouse', version: '0.1.0' },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'run_sql',
      description:
        'Run a read-only SQL query against the analytics warehouse (PostgreSQL). ' +
        'Returns up to 50 rows as JSON, plus a query id you can reference later. ' +
        // WIDE_SCHEMA hides the schema so the agent must discover which tables
        // matter — the condition under which schema-probing redundancy could
        // appear at all.
        (process.env.WIDE_SCHEMA
          ? 'The warehouse contains many tables and you do not know the schema. ' +
            'Use information_schema.tables and information_schema.columns to discover ' +
            'what exists before querying it.'
          : 'Tables: orders(order_id, order_date, region, channel, customer_id, amount), ' +
            'refunds(refund_id, order_id, refund_date, amount).'),
      inputSchema: {
        type: 'object',
        properties: {
          sql: { type: 'string', description: 'The SELECT statement to run.' },
          intent: {
            type: 'string',
            description:
              'One short phrase describing what you are trying to learn with this query, ' +
              'e.g. "daily revenue for July" or "check whether refunds spiked".',
          },
          follows_from: {
            type: 'string',
            description:
              'Optional. The query id (e.g. "q3") whose result prompted this query. ' +
              'Omit for a query that starts a new line of investigation.',
          },
        },
        required: ['sql', 'intent'],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== 'run_sql') {
    return { content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }], isError: true };
  }
  const { sql, intent, follows_from } = req.params.arguments ?? {};

  const refusal = readOnlyRefusal(sql ?? '');
  if (refusal) {
    return { content: [{ type: 'text', text: refusal }], isError: true };
  }

  const label = `q${++seq}`;
  const span = {
    trace_id: TRACE_ID,
    span_id: randomBytes(6).toString('hex'),
    parent_span_id: follows_from ? spansByLabel.get(follows_from) ?? null : null,
    agent_id: AGENT_ID,
    model_id: MODEL_ID,
    span_intent: intent ?? label,
    // Classified from the agent's own declaration: a query that starts a new
    // line of investigation is a probe; one that follows a prior result refines.
    speculation_class: follows_from ? 'refine' : 'probe',
    attempt_n: 1,
    retry_of: null,
  };
  spansByLabel.set(label, span.span_id);

  const tagged = `${serializeContext(span)} ${sql}`;
  let rows = [];
  let error = null;
  const t0 = process.hrtime.bigint();
  try {
    const res = await client.query(tagged);
    rows = res.rows;
  } catch (e) {
    error = e.message.split('\n')[0];
  }
  const clientMs = Number(process.hrtime.bigint() - t0) / 1e6;

  appendFileSync(
    EVENTS_PATH,
    JSON.stringify({
      trace_id: span.trace_id,
      span_id: span.span_id,
      parent_span_id: span.parent_span_id,
      label,
      speculation_class: span.speculation_class,
      span_intent: span.span_intent,
      attempt_n: 1,
      retry_of: null,
      result_hash: createHash('sha1').update(JSON.stringify(rows)).digest('hex').slice(0, 12),
      rows: rows.length,
      client_ms: clientMs,
      values: scalarValues(rows),
      question: QUESTION,
      error,
    }) + '\n'
  );

  if (error) {
    return { content: [{ type: 'text', text: `[${label}] SQL error: ${error}` }], isError: true };
  }
  const shown = rows.slice(0, 50);
  return {
    content: [
      {
        type: 'text',
        text:
          `[${label}] ${rows.length} row(s)` +
          (rows.length > 50 ? ' (showing first 50)' : '') +
          `\n${JSON.stringify(shown, null, 1)}`,
      },
    ],
  };
});

await server.connect(new StdioServerTransport());
