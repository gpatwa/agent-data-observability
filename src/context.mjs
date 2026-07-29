// Trace context: mint IDs for the agent plan tree and serialize them into a
// SQL comment (sqlcommenter-style) so the warehouse logs them verbatim.
//
// This is the ONLY thing that touches the data path, and all it does is prepend
// a comment. No rewriting, no interception, no added latency.

import { randomBytes } from 'node:crypto';

const id = (n = 8) => randomBytes(n).toString('hex');

export function newTrace({ agentId, model, taskIntent }) {
  return {
    trace_id: id(8),
    agent_id: agentId,
    model_id: model,
    task_intent: taskIntent,
    span_stack: [],
  };
}

export function newSpan(trace, { intent, speculationClass, parent = null, attempt = 1, retryOf = null }) {
  return {
    trace_id: trace.trace_id,
    span_id: id(6),
    parent_span_id: parent,
    agent_id: trace.agent_id,
    model_id: trace.model_id,
    task_intent: trace.task_intent,
    span_intent: intent,
    speculation_class: speculationClass, // probe | refine | final
    attempt_n: attempt,
    retry_of: retryOf,
  };
}

// sqlcommenter-style serialization. Values are URL-encoded so that quotes,
// spaces and `*/` in intent text can never break out of the comment.
export function serializeContext(span) {
  const fields = {
    t: span.trace_id,
    s: span.span_id,
    p: span.parent_span_id ?? '-',
    a: span.agent_id,
    m: span.model_id,
    c: span.speculation_class,
    n: String(span.attempt_n),
    r: span.retry_of ?? '-',
    i: span.span_intent,
  };
  const kv = Object.entries(fields)
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join(',');
  return `/*agenttrace:${kv}*/`;
}

export function parseContext(comment) {
  const m = comment.match(/\/\*agenttrace:([^*]*)\*\//);
  if (!m) return null;
  const out = {};
  for (const pair of m[1].split(',')) {
    const eq = pair.indexOf('=');
    if (eq === -1) continue;
    out[pair.slice(0, eq)] = decodeURIComponent(pair.slice(eq + 1));
  }
  return {
    trace_id: out.t,
    span_id: out.s,
    parent_span_id: out.p === '-' ? null : out.p,
    agent_id: out.a,
    model_id: out.m,
    speculation_class: out.c,
    attempt_n: Number(out.n),
    retry_of: out.r === '-' ? null : out.r,
    span_intent: out.i,
  };
}
