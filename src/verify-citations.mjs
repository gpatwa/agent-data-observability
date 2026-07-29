// Phase 0a — verify `used_downstream` instead of trusting it.
//
// The agent self-reports which queries it cited ("CITED: q1, q4"). That is the
// agent grading its own homework, and the "zero speculation waste" finding
// rested on it. This module checks the claim against evidence: does a value
// from that query's result set actually appear in the final answer?
//
// Two strengths of evidence:
//   grounded         — some value from this result appears in the answer
//   uniquely_grounded— a value appears that NO other query's result contained
//
// `uniquely_grounded` is the strong signal. `grounded` alone is weak: a value
// like a row count of 6000 may appear in many result sets at once.

const MAGNITUDE = { k: 1e3, m: 1e6, b: 1e9, bn: 1e9, t: 1e12 };

// The agent's own claim about which queries it used. Models wrap this line in
// markdown ("**CITED: q1, q4**"), so tolerate emphasis and list markers —
// a line-anchored /^CITED:/ silently scores such a run as citing nothing.
export function parseCited(answer) {
  const m = answer.match(/^[\s*_#>\-]*CITED:\s*(.+?)[\s*_]*$/mi);
  if (!m) return null;
  return new Set(
    m[1].split(/[,\s]+/).map((s) => s.trim().replace(/[^a-z0-9]/gi, '').toLowerCase()).filter(Boolean)
  );
}

// Pull numbers out of prose, expanding $319.9M -> 319900000 and stripping
// thousands separators. Percentages are kept as their literal number.
export function numbersInText(text) {
  const out = [];
  const re = /(-?\d[\d,]*\.?\d*)\s*(k|m|b|bn|t)?\b/gi;
  let m;
  while ((m = re.exec(text))) {
    const base = Number(m[1].replace(/,/g, ''));
    if (!Number.isFinite(base)) continue;
    out.push(base);
    const suf = m[2]?.toLowerCase();
    if (suf && MAGNITUDE[suf]) out.push(base * MAGNITUDE[suf]);
  }
  return out;
}

// A result value counts as present if the answer contains a number within
// tolerance. 2% absorbs the rounding agents do when they write prose
// ("$319.9M" for 319875432.11, "~$150" for 149.87).
function numberPresent(value, answerNumbers, tol = 0.02) {
  for (const a of answerNumbers) {
    const denom = Math.max(Math.abs(value), 1);
    if (Math.abs(a - value) / denom <= tol) return true;
  }
  return false;
}

function stringPresent(value, answerLower) {
  const v = String(value).trim();
  if (v.length < 3) return false;              // too short to be evidence
  if (answerLower.includes(v.toLowerCase())) return true;
  // ISO dates also appear written out: 2026-07-12 -> "July 12"
  const d = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (d) {
    const month = new Date(`${v}T00:00:00Z`).toLocaleString('en-US', {
      month: 'long', timeZone: 'UTC',
    });
    const day = Number(d[3]);
    if (answerLower.includes(`${month.toLowerCase()} ${day}`)) return true;
  }
  return false;
}

// Values captured before the numeric-string fix (and any future producer that
// hands us "123.45") must compare numerically, not as substrings.
const normalizeValue = (v) =>
  typeof v === 'string' && /^-?\d+(\.\d+)?$/.test(v.trim()) ? Number(v) : v;

export function verify(events, answer) {
  const answerLower = answer.toLowerCase();
  const answerNumbers = numbersInText(answer);

  // How many result sets contained each value? Values seen everywhere are not
  // evidence that any particular query reached the answer.
  const valueFreq = new Map();
  for (const e of events) {
    for (const v of new Set((e.values ?? []).map(normalizeValue))) {
      valueFreq.set(v, (valueFreq.get(v) ?? 0) + 1);
    }
  }

  return events.map((e) => {
    let grounded = false;
    let uniquely = false;
    const hits = [];
    for (const v of (e.values ?? []).map(normalizeValue)) {
      const present =
        typeof v === 'number'
          ? numberPresent(v, answerNumbers)
          : stringPresent(v, answerLower);
      if (!present) continue;
      grounded = true;
      if ((valueFreq.get(v) ?? 0) === 1) {
        uniquely = true;
        if (hits.length < 4) hits.push(v);
      } else if (hits.length < 4) hits.push(v);
    }
    return { ...e, grounded, uniquely_grounded: uniquely, evidence: hits };
  });
}

// --- CLI ------------------------------------------------------------------
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop())) {
  const { readFileSync, writeFileSync } = await import('node:fs');
  const [eventsPath, answerPath] = process.argv.slice(2);
  if (!eventsPath || !answerPath) {
    console.error('usage: node src/verify-citations.mjs <events.jsonl> <answer.txt>');
    process.exit(1);
  }
  const events = readFileSync(eventsPath, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  const answer = readFileSync(answerPath, 'utf8');
  const claimed = parseCited(answer);
  if (claimed) for (const e of events) e.used_downstream = claimed.has(e.label);
  const verified = verify(events, answer);

  const selfCited = verified.filter((e) => e.used_downstream).length;
  const grounded = verified.filter((e) => e.grounded).length;
  const unique = verified.filter((e) => e.uniquely_grounded).length;

  console.log('── CITATION VERIFICATION ─────────────────────────────────────');
  console.log(`  queries                          ${verified.length}`);
  console.log(`  self-reported as cited           ${selfCited}`);
  console.log(`  grounded (any value in answer)   ${grounded}`);
  console.log(`  uniquely grounded (strong)       ${unique}`);
  console.log('');
  for (const e of verified) {
    const flag =
      e.used_downstream && !e.grounded ? ' ← CLAIMED BUT UNGROUNDED'
      : !e.used_downstream && e.uniquely_grounded ? ' ← used but not claimed'
      : '';
    console.log(
      `  ${(e.label ?? '?').padEnd(4)} claim=${e.used_downstream ? 'Y' : 'n'} ` +
      `grounded=${e.grounded ? 'Y' : 'n'} unique=${e.uniquely_grounded ? 'Y' : 'n'}  ` +
      `${(e.span_intent ?? '').slice(0, 46)}${flag}`
    );
  }

  writeFileSync(eventsPath, verified.map((e) => JSON.stringify(e)).join('\n'));
}
