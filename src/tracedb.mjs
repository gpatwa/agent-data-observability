// The middleware. In PHASE 1 (this file) it is out-of-path: it injects the
// trace comment and forwards. The same seam is where PHASE 2 would flip to
// in-path mode (dedup / cache / approximate) behind a config flag.

import { serializeContext } from './context.mjs';
import { createHash } from 'node:crypto';

export class TracedClient {
  constructor(pgClient, { mode = 'observe' } = {}) {
    this.pg = pgClient;
    this.mode = mode; // 'observe' (out of path) | 'intercept' (phase 2, not built)
    this.agentEvents = [];
  }

  async run(span, sql) {
    if (this.mode !== 'observe') {
      throw new Error('intercept mode is Phase 2 — deliberately not implemented');
    }
    const tagged = `${serializeContext(span)} ${sql}`;
    const t0 = process.hrtime.bigint();
    const res = await this.pg.query(tagged);
    const elapsedMs = Number(process.hrtime.bigint() - t0) / 1e6;

    // Agent-side event. This is the half the warehouse CANNOT see, and it is
    // what makes `used_downstream` computable later.
    const resultHash = createHash('sha1')
      .update(JSON.stringify(res.rows))
      .digest('hex')
      .slice(0, 12);

    this.agentEvents.push({
      trace_id: span.trace_id,
      span_id: span.span_id,
      parent_span_id: span.parent_span_id,
      speculation_class: span.speculation_class,
      span_intent: span.span_intent,
      attempt_n: span.attempt_n,
      retry_of: span.retry_of,
      result_hash: resultHash,
      rows: res.rowCount,
      client_ms: elapsedMs,
    });
    return res;
  }

  // Called by the agent when it composes its final answer: records which
  // upstream results actually informed the response.
  citeResults(spanIds) {
    this.cited = new Set(spanIds);
  }

  dumpEvents() {
    return this.agentEvents.map((e) => ({
      ...e,
      used_downstream: this.cited ? this.cited.has(e.span_id) : null,
    }));
  }
}
