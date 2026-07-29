// Drives a REAL LLM agent (Claude Code, headless) against the traced warehouse.
//
// The agent's only tool is `run_sql`, served by src/mcp-db-server.mjs. Every
// query it issues is tagged and lands in the Postgres log; the plan tree is
// reconstructed from that log exactly as in the simulated run.
//
// Usage:  node src/real-agent.mjs "Why did revenue drop in July 2026?"

import { spawn } from 'node:child_process';
import { writeFileSync, readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ROOT = new URL('..', import.meta.url);
const EVENTS = fileURLToPath(new URL('out/agent-events.jsonl', ROOT));
const MCP_CONFIG = fileURLToPath(new URL('out/mcp-config.json', ROOT));
const SERVER = fileURLToPath(new URL('src/mcp-db-server.mjs', ROOT));

const TASK = process.argv[2] ?? 'Why did revenue drop in July 2026?';

const SYSTEM_APPEND = `
You are a data analyst with access to one tool: run_sql, against a PostgreSQL
analytics warehouse. You have no filesystem and no other tools.

Work the question until you can name a specific cause with evidence.

Two requirements on how you use run_sql:
  - Always pass a short "intent" describing what you are trying to learn.
  - When a query was prompted by an earlier result, pass that query's id as
    "follows_from" (e.g. follows_from: "q3"). This records your reasoning chain.

End your final answer with a line in exactly this format, listing the query ids
whose results actually support your conclusion:

CITED: q1, q4, q9
`.trim();

function run() {
  writeFileSync(
    MCP_CONFIG,
    JSON.stringify(
      { mcpServers: { 'traced-warehouse': { command: 'node', args: [SERVER] } } },
      null,
      2
    )
  );
  writeFileSync(EVENTS, '');

  const args = [
    '-p', TASK,
    '--mcp-config', MCP_CONFIG,
    '--allowedTools', 'mcp__traced-warehouse__run_sql',
    '--append-system-prompt', SYSTEM_APPEND,
    '--output-format', 'json',
  ];

  console.log(`==> launching real agent: "${TASK}"`);
  const child = spawn('claude', args, { stdio: ['ignore', 'pipe', 'inherit'] });

  let out = '';
  child.stdout.on('data', (d) => { out += d; });

  child.on('close', (code) => {
    if (code !== 0) {
      console.error(`claude exited ${code}`);
      process.exit(code ?? 1);
    }
    let answer = out;
    try {
      const parsed = JSON.parse(out);
      answer = parsed.result ?? out;
      if (parsed.total_cost_usd != null) {
        console.log(`\n==> agent LLM cost: $${parsed.total_cost_usd.toFixed(4)}, turns: ${parsed.num_turns}`);
      }
    } catch { /* not JSON — treat the whole stdout as the answer */ }

    console.log('\n──── AGENT ANSWER ───────────────────────────────────────────────\n');
    console.log(answer.trim());
    console.log('\n─────────────────────────────────────────────────────────────────');

    markCited(answer);
  });
}

// The half the warehouse cannot see: which results actually reached the answer.
function markCited(answer) {
  if (!existsSync(EVENTS)) return;
  const m = answer.match(/^\s*CITED:\s*(.+)$/mi);
  const cited = new Set(
    m ? m[1].split(/[,\s]+/).map((s) => s.trim().toLowerCase()).filter(Boolean) : []
  );

  const events = readFileSync(EVENTS, 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l));

  for (const e of events) e.used_downstream = cited.has(e.label);
  writeFileSync(EVENTS, events.map((e) => JSON.stringify(e)).join('\n'));

  const unmatched = [...cited].filter((c) => !events.some((e) => e.label === c));
  console.log(`\n==> ${events.length} queries issued, ${events.filter((e) => e.used_downstream).length} cited by the agent`);
  if (!m) console.log('    (no CITED line found — used_downstream recorded as false for all)');
  if (unmatched.length) console.log(`    (cited but never issued: ${unmatched.join(', ')})`);
}

run();
