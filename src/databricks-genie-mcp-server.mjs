// MCP server exposing Databricks Genie to a real agent, with verification
// wrapped around a connection we do not control.
//
// The tool here is `ask_genie` — natural language, not SQL. That is a genuine
// difference from the Postgres and Snowflake servers: the agent no longer
// authors queries, so this harness cannot inject trace context, cannot enforce
// a read-only guard (Genie is read-only by construction), and cannot tag for
// credit attribution.
//
// What it CAN still do, and what makes this the interesting deployment shape:
//   - record the question, the SQL Genie generated, and the result rows
//   - extract scalar values for answer-grounding
//   - reconstruct a plan tree from agent-declared intent/follows_from
//
// Unity Catalog enforces the caller's row filters and column masks on every
// Genie call, so governance is the platform's job here rather than ours.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { appendFileSync } from 'node:fs';
import { createHash, randomBytes } from 'node:crypto';
import {
  config, startConversation, sendMessage, waitForMessage,
  extractAttachments, getQueryResult, rowsFromResult,
} from './databricks.mjs';

const EVENTS_PATH = process.env.TRACE_EVENTS_PATH
  ? new URL(`file://${process.env.TRACE_EVENTS_PATH}`)
  : new URL('../out/databricks-events.jsonl', import.meta.url);
const TRACE_ID = randomBytes(8).toString('hex');
const AGENT_ID = process.env.AGENT_ID ?? 'genie-analyst';
const MODEL_ID = process.env.AGENT_MODEL ?? 'claude-opus-5';

const cfg = config();
let conversationId = null;      // Genie conversations are stateful; reuse one
let seq = 0;
const spansByLabel = new Map();

function scalarValues(rows) {
  const out = new Set();
  for (const row of rows.slice(0, 200)) {
    for (const v of Object.values(row)) {
      if (v == null) continue;
      const s = typeof v === 'string' ? v : String(v);
      if (/^-?\d+(\.\d+)?$/.test(s.trim())) out.add(Number(s));
      else if (s.length <= 64) out.add(s);
    }
    if (out.size > 400) break;
  }
  return [...out];
}

const server = new Server(
  { name: 'traced-genie', version: '0.1.0' },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'ask_genie',
    description:
      'Ask a question in plain English about the data in this Databricks Genie space. ' +
      'Genie writes and runs the SQL against Unity Catalog under your permissions, and ' +
      'returns both the generated SQL and the result rows. You do not write SQL yourself.',
    inputSchema: {
      type: 'object',
      properties: {
        question: { type: 'string', description: 'The question, in plain English.' },
        intent: { type: 'string', description: 'One short phrase describing what you are trying to learn.' },
        follows_from: { type: 'string', description: 'Optional id (e.g. "q3") of the answer that prompted this question.' },
      },
      required: ['question', 'intent'],
    },
  }],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== 'ask_genie') {
    return { content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }], isError: true };
  }
  const { question, intent, follows_from } = req.params.arguments ?? {};
  if (!question?.trim()) {
    return { content: [{ type: 'text', text: 'question is required' }], isError: true };
  }

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
  let sql = null;
  let genieText = null;
  let error = null;
  const t0 = process.hrtime.bigint();

  try {
    const started = conversationId
      ? await sendMessage(cfg, conversationId, question)
      : await startConversation(cfg, question);

    conversationId ??= started.conversation_id ?? started.conversation?.id;
    const messageId = started.message_id ?? started.id ?? started.message?.id;
    if (!conversationId || !messageId) {
      throw new Error(`unexpected Genie response shape: ${JSON.stringify(started).slice(0, 200)}`);
    }

    const done = await waitForMessage(cfg, conversationId, messageId);
    const status = done.status ?? done.state;
    const att = extractAttachments(done);
    sql = att.sql;
    genieText = att.text ?? att.description;

    if (status === 'FAILED' || status === 'CANCELLED') {
      error = done.error?.message ?? `Genie returned ${status}`;
    } else if (att.attachmentId) {
      const result = await getQueryResult(cfg, conversationId, messageId, att.attachmentId);
      rows = rowsFromResult(result);
    }
  } catch (e) {
    error = e.message.split('\n')[0];
  }

  const clientMs = Number(process.hrtime.bigint() - t0) / 1e6;

  appendFileSync(EVENTS_PATH, JSON.stringify({
    trace_id: span.trace_id, span_id: span.span_id, parent_span_id: span.parent_span_id,
    label, speculation_class: span.speculation_class, span_intent: span.span_intent,
    question,
    sql,                                   // Genie authored this, not the agent
    genie_text: genieText,
    conversation_id: conversationId,
    result_hash: createHash('sha1').update(JSON.stringify(rows)).digest('hex').slice(0, 12),
    rows: rows.length,
    client_ms: clientMs,
    values: scalarValues(rows),
    error,
  }) + '\n');

  if (error) {
    return { content: [{ type: 'text', text: `[${label}] Genie error: ${error}` }], isError: true };
  }

  const shown = rows.slice(0, 50);
  const parts = [`[${label}] ${rows.length} row(s)${rows.length > 50 ? ' (showing first 50)' : ''}`];
  if (genieText) parts.push(`Genie: ${genieText}`);
  if (sql) parts.push(`SQL Genie ran:\n${sql}`);
  parts.push(JSON.stringify(shown, null, 1));

  return { content: [{ type: 'text', text: parts.join('\n\n') }] };
});

await server.connect(new StdioServerTransport());
