// Shared trace reconstruction and billing.
//
// Extracted from assemble.mjs so the single-trace report, the cross-session
// analysis, and the citation verifier all read traces the same way. There is
// exactly one parser for the warehouse log and one billing model in this repo.

import { readFileSync } from 'node:fs';
import { parseContext } from './context.mjs';
import { exactHash, astHash, extractShape } from './fingerprint.mjs';

// --- Warehouse billing model (Snowflake XS, Standard edition) --------------
export const BILLING = {
  CREDITS_PER_HOUR: 1,   // XS warehouse
  DOLLARS_PER_CREDIT: 3.0,
  MIN_BILLING_SEC: 60,   // charged on every resume
  AUTO_SUSPEND_SEC: 60,
};
export const sec2dollars = (s) =>
  (s / 3600) * BILLING.CREDITS_PER_HOUR * BILLING.DOLLARS_PER_CREDIT;

const LOG_LINE =
  /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \w+ \[(\d+)\] (LOG|ERROR|STATEMENT|HINT|WARNING|DETAIL|FATAL):\s+(.*)$/;

export function parseLog(path) {
  const lines = readFileSync(path, 'utf8').split('\n');
  const events = [];
  let cur = null;

  for (const line of lines) {
    const m = line.match(LOG_LINE);
    if (!m) {
      // continuation of a multi-line statement
      if (cur && cur.kind === 'statement') cur.text += '\n' + line;
      continue;
    }
    const [, ts, pid, , rest] = m;
    const t = new Date(ts.replace(' ', 'T')).getTime();

    if (rest.startsWith('statement: ')) {
      if (cur) events.push(cur);
      cur = { kind: 'statement', t, pid, text: rest.slice('statement: '.length) };
    } else if (rest.startsWith('duration: ')) {
      const ms = parseFloat(rest.match(/duration: ([\d.]+) ms/)?.[1] ?? '0');
      if (cur && cur.kind === 'statement' && cur.pid === pid) {
        cur.duration_ms = ms;
        events.push(cur);
        cur = null;
      }
    } else {
      if (cur) events.push(cur);
      cur = null;
    }
  }
  if (cur) events.push(cur);
  return events.filter((e) => e.kind === 'statement');
}

// Rebuild spans from the warehouse log, then join the agent-side event files.
// `eventPaths` may list several files — one per agent session.
export function reconstruct(logPath, eventPaths) {
  const spans = [];
  for (const s of parseLog(logPath)) {
    const ctx = parseContext(s.text);
    if (!ctx) continue; // untagged traffic (seeding, admin) — ignored
    const sql = s.text.replace(/\/\*agenttrace:[^*]*\*\/\s*/, '');
    spans.push({
      ...ctx,
      sql,
      start_ms: s.t,
      exec_ms: s.duration_ms ?? 0,
      exact: exactHash(sql),
      ast: astHash(sql),
      shape: extractShape(sql),
    });
  }

  const byId = new Map();
  for (const p of [eventPaths].flat().filter(Boolean)) {
    let raw;
    try {
      raw = readFileSync(p, 'utf8');
    } catch {
      continue;
    }
    for (const line of raw.split('\n').filter(Boolean)) {
      const e = JSON.parse(line);
      byId.set(e.span_id, e);
    }
  }

  for (const sp of spans) {
    const e = byId.get(sp.span_id);
    sp.used_downstream = e?.used_downstream ?? false;
    sp.grounded = e?.grounded ?? null;
    sp.result_hash = e?.result_hash;
    sp.rows = e?.rows;
    sp.label = e?.label;
    sp.question = e?.question;
  }
  spans.sort((a, b) => a.start_ms - b.start_ms);
  return spans;
}

// Apply warehouse billing to a set of spans.
// `dilation` scales elapsed wall-clock only — used by the simulated agent,
// whose think-time is compressed. Real traces pass 1.
export function bill(spans, dilation = 1) {
  if (!spans.length) return null;
  const t0 = spans[0].start_ms;
  const intervals = spans.map((s) => {
    const start = ((s.start_ms - t0) / 1000) * dilation;
    return { start, end: start + s.exec_ms / 1000, span: s };
  });

  // Group into resume windows separated by more than AUTO_SUSPEND_SEC of idle.
  const windows = [];
  let w = null;
  for (const iv of intervals) {
    if (!w || iv.start > w.lastEnd + BILLING.AUTO_SUSPEND_SEC) {
      w = { start: iv.start, lastEnd: iv.end, items: [iv] };
      windows.push(w);
    } else {
      w.lastEnd = Math.max(w.lastEnd, iv.end);
      w.items.push(iv);
    }
  }

  let billedSec = 0;
  for (const win of windows) {
    win.billed = Math.max(
      BILLING.MIN_BILLING_SEC,
      win.lastEnd - win.start + BILLING.AUTO_SUSPEND_SEC
    );
    billedSec += win.billed;
  }

  const productiveSec = intervals.reduce((a, iv) => a + (iv.end - iv.start), 0);
  const overhead = billedSec - productiveSec;
  for (const iv of intervals) {
    const prod = iv.end - iv.start;
    iv.span.billed_sec =
      prod + (productiveSec > 0 ? (prod / productiveSec) * overhead : overhead / intervals.length);
    iv.span.cost = sec2dollars(iv.span.billed_sec);
  }

  const batchedSec = Math.max(
    BILLING.MIN_BILLING_SEC,
    productiveSec + BILLING.AUTO_SUSPEND_SEC
  );

  return {
    windows, billedSec, productiveSec, overhead, batchedSec,
    elapsedSec: intervals.at(-1).end,
  };
}
