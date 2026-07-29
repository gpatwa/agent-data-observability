// Drives a REAL LLM agent (Claude Code, headless) against the traced warehouse.
//
// The agent's only tool is `run_sql`, served by src/mcp-db-server.mjs. Every
// query it issues is tagged and lands in the Postgres log; the plan tree is
// reconstructed from that log exactly as for the simulated agent.
//
//   node src/real-agent.mjs "Why did revenue drop in July 2026?"
//   node src/real-agent.mjs "..." --model claude-haiku-4-5 --tag haiku
//
// Exported as runAgent() so cross-session.mjs can fan out over many questions.

import { spawn } from 'node:child_process';
import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { verify, parseCited } from './verify-citations.mjs';

const ROOT = new URL('..', import.meta.url);
const OUT = fileURLToPath(new URL('out/', ROOT));
const SERVER = fileURLToPath(new URL('src/mcp-db-server.mjs', ROOT));

const SYSTEM_APPEND = `
You are a data analyst with access to one tool: run_sql, against a PostgreSQL
analytics warehouse. You have no filesystem and no other tools.

Work the question until you can answer it with evidence.

Two requirements on how you use run_sql:
  - Always pass a short "intent" describing what you are trying to learn.
  - When a query was prompted by an earlier result, pass that query's id as
    "follows_from" (e.g. follows_from: "q3"). This records your reasoning chain.

End your final answer with a line in exactly this format, listing the query ids
whose results actually support your conclusion:

CITED: q1, q4, q9
`.trim();

export function runAgent({ question, model = null, tag = 'agent' }) {
  mkdirSync(OUT, { recursive: true });
  const eventsPath = `${OUT}${tag}-events.jsonl`;
  const answerPath = `${OUT}${tag}-answer.txt`;
  const configPath = `${OUT}${tag}-mcp.json`;

  writeFileSync(
    configPath,
    JSON.stringify({
      mcpServers: {
        'traced-warehouse': {
          command: 'node',
          args: [SERVER],
          env: {
            TRACE_EVENTS_PATH: eventsPath,
            TRACE_QUESTION: question,
            AGENT_MODEL: model ?? 'claude-opus-5',
            AGENT_ID: `claude-code-${tag}`,
          },
        },
      },
    })
  );
  writeFileSync(eventsPath, '');

  const args = [
    '-p', question,
    '--mcp-config', configPath,
    '--allowedTools', 'mcp__traced-warehouse__run_sql',
    '--append-system-prompt', SYSTEM_APPEND,
    '--output-format', 'json',
  ];
  if (model) args.push('--model', model);

  return new Promise((resolve) => {
    const child = spawn('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; });

    child.on('close', (code) => {
      let answer = out;
      let cost = null;
      let turns = null;
      try {
        const parsed = JSON.parse(out);
        answer = parsed.result ?? out;
        cost = parsed.total_cost_usd ?? null;
        turns = parsed.num_turns ?? null;
      } catch { /* not JSON — treat stdout as the answer */ }

      if (code !== 0) {
        console.error(`  [${tag}] claude exited ${code}: ${err.slice(0, 300)}`);
        return resolve({ tag, question, model, ok: false, queries: 0 });
      }

      writeFileSync(answerPath, answer);
      const stats = scoreRun(eventsPath, answer);
      resolve({ tag, question, model, ok: true, cost, turns, answerPath, eventsPath, ...stats });
    });
  });
}

// Records both the agent's claim and the verified grounding for each query.
function scoreRun(eventsPath, answer) {
  if (!existsSync(eventsPath)) return { queries: 0 };
  const events = readFileSync(eventsPath, 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l));
  if (!events.length) return { queries: 0 };

  const claimed = parseCited(answer) ?? new Set();
  for (const e of events) e.used_downstream = claimed.has(e.label);

  const verified = verify(events, answer);
  writeFileSync(eventsPath, verified.map((e) => JSON.stringify(e)).join('\n'));

  return {
    queries: verified.length,
    claimed: verified.filter((e) => e.used_downstream).length,
    grounded: verified.filter((e) => e.grounded).length,
    unique: verified.filter((e) => e.uniquely_grounded).length,
  };
}

// --- CLI ------------------------------------------------------------------
if (process.argv[1]?.endsWith('real-agent.mjs')) {
  const question = process.argv[2] ?? 'Why did revenue drop in July 2026?';
  const modelIdx = process.argv.indexOf('--model');
  const tagIdx = process.argv.indexOf('--tag');
  const model = modelIdx > -1 ? process.argv[modelIdx + 1] : null;
  const tag = tagIdx > -1 ? process.argv[tagIdx + 1] : 'agent';

  console.log(`==> ${tag}: "${question}"${model ? ` [${model}]` : ''}`);
  const r = await runAgent({ question, model, tag });
  if (!r.ok) process.exit(1);
  console.log(`\n${readFileSync(r.answerPath, 'utf8').trim()}\n`);
  console.log(`==> ${r.queries} queries · claimed ${r.claimed} · grounded ${r.grounded} · uniquely grounded ${r.unique}`);
  if (r.cost != null) console.log(`==> LLM cost $${r.cost.toFixed(4)}, ${r.turns} turns`);
}
