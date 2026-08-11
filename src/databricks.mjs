// Databricks Genie adapter.
//
// ARCHITECTURALLY DIFFERENT FROM THE OTHERS, AND THAT IS THE POINT.
//
// Postgres: we inject trace context into the SQL and parse the server log.
// Snowflake: we set QUERY_TAG and read it back from ACCOUNT_USAGE.
// Genie:    we control NEITHER. Genie authors the SQL and runs it under its own
//           session inside Databricks. There is no hook to inject anything.
//
// This is the shape any real product faces — verification sitting ABOVE a
// managed connection you do not own. What we still get is everything that
// matters for answer-grounding:
//   - the question asked
//   - the SQL Genie generated (returned in the message attachments)
//   - the result rows
//
// So sub-expression analysis and value-grounding both still work; per-query
// credit attribution does not, because that would need query_tags we cannot set.
// system.query.history does expose query_tags as MAP<STRING,STRING> now, but
// only for statements whose caller sets them — Genie's are its own.
//
// Credentials from the environment only:
//   DATABRICKS_HOST            https://<workspace>.cloud.databricks.com
//   DATABRICKS_TOKEN           personal access token (or OAuth bearer)
//   DATABRICKS_GENIE_SPACE_ID  the Genie space to query

import './env.mjs';

const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'QUERY_RESULT_EXPIRED']);
const PENDING = new Set(['IN_PROGRESS', 'PENDING_WAREHOUSE', 'EXECUTING_QUERY', 'FETCHING_METADATA',
                         'FILTERING_CONTEXT', 'ASKING_AI', 'SUBMITTING_QUERY']);

export function config() {
  const host = (process.env.DATABRICKS_HOST ?? '').replace(/\/+$/, '');
  const token = process.env.DATABRICKS_TOKEN;
  const spaceId = process.env.DATABRICKS_GENIE_SPACE_ID;
  const missing = [
    !host && 'DATABRICKS_HOST',
    !token && 'DATABRICKS_TOKEN',
    !spaceId && 'DATABRICKS_GENIE_SPACE_ID',
  ].filter(Boolean);
  if (missing.length) {
    throw new Error(`Missing ${missing.join(', ')}. See docs/DATABRICKS.md.`);
  }
  if (!/^https:\/\//.test(host)) {
    throw new Error(`DATABRICKS_HOST must start with https:// (got ${host.slice(0, 40)})`);
  }
  return { host, token, spaceId };
}

async function api(cfg, path, { method = 'GET', body } = {}) {
  const res = await fetch(`${cfg.host}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${cfg.token}`,
      'Content-Type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) {
    // Surface the API's own message — Databricks errors are specific and the
    // generic "request failed" wrapper throws that detail away.
    let detail = text.slice(0, 300);
    try { detail = JSON.parse(text).message ?? detail; } catch { /* keep raw */ }
    throw new Error(`${res.status} ${path.split('?')[0]}: ${detail}`);
  }
  return text ? JSON.parse(text) : {};
}

export const startConversation = (cfg, content) =>
  api(cfg, `/api/2.0/genie/spaces/${cfg.spaceId}/start-conversation`,
      { method: 'POST', body: { content } });

export const sendMessage = (cfg, conversationId, content) =>
  api(cfg, `/api/2.0/genie/spaces/${cfg.spaceId}/conversations/${conversationId}/messages`,
      { method: 'POST', body: { content } });

export const getMessage = (cfg, conversationId, messageId) =>
  api(cfg, `/api/2.0/genie/spaces/${cfg.spaceId}/conversations/${conversationId}/messages/${messageId}`);

export const getQueryResult = (cfg, conversationId, messageId, attachmentId) =>
  api(cfg, `/api/2.0/genie/spaces/${cfg.spaceId}/conversations/${conversationId}` +
           `/messages/${messageId}/query-result/${attachmentId}`);

// Poll until the message reaches a terminal status. Genie can take minutes on a
// cold warehouse, so the ceiling is generous but bounded — an unbounded poll
// against a stuck message is how a harness hangs forever.
export async function waitForMessage(cfg, conversationId, messageId, { timeoutMs = 300_000, intervalMs = 2000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    last = await getMessage(cfg, conversationId, messageId);
    const status = last.status ?? last.state;
    if (TERMINAL.has(status)) return last;
    if (status && !PENDING.has(status)) {
      // Unknown status: return rather than spin, and let the caller decide.
      return last;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`Genie message ${messageId} did not finish within ${timeoutMs / 1000}s`);
}

// Pull the parts we care about out of Genie's response shape.
export function extractAttachments(message) {
  const out = { text: null, sql: null, attachmentId: null, description: null };
  for (const a of message?.attachments ?? []) {
    if (a.text?.content && !out.text) out.text = a.text.content;
    if (a.query) {
      out.sql = a.query.query ?? a.query.statement ?? null;
      out.description = a.query.description ?? null;
      out.attachmentId = a.attachment_id ?? a.id ?? null;
    }
  }
  return out;
}

// Genie returns results in statement-execution shape: a manifest of column
// names plus a data_array of row arrays. Normalise to objects so the rest of
// the harness (value grounding especially) sees the same thing it does from
// Postgres and Snowflake.
export function rowsFromResult(result) {
  const sr = result?.statement_response ?? result;
  const cols = (sr?.manifest?.schema?.columns ?? []).map((c) => c.name);
  const data = sr?.result?.data_array ?? sr?.result?.data_typed_array ?? [];
  if (!cols.length) return [];
  return data.map((row) => {
    const vals = Array.isArray(row) ? row : (row.values ?? []).map((v) => v?.str ?? v);
    return Object.fromEntries(cols.map((c, i) => [c, vals[i] ?? null]));
  });
}
