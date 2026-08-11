// Preflight for the Databricks Genie pilot.
//
// Deliberately exercises the SAME code path the agent uses (config -> start
// conversation -> poll -> extract -> fetch result), because the last time a
// preflight wrote its own shortcut it passed while production was broken.

import {
  config, startConversation, waitForMessage, extractAttachments,
  getQueryResult, rowsFromResult,
} from './databricks.mjs';

const ok = (s) => `  ✓ ${s}`;
const bad = (s) => `  ✗ ${s}`;

async function main() {
  let cfg;
  try {
    cfg = config();
  } catch (e) {
    console.error(bad(e.message));
    process.exit(1);
  }

  console.log('── DATABRICKS GENIE PREFLIGHT ─────────────────────────────────');
  console.log(`  host      ${cfg.host}`);
  console.log(`  space     ${cfg.spaceId}`);
  console.log(`  token     ${cfg.token.slice(0, 4)}…${cfg.token.slice(-2)} (${cfg.token.length} chars)`);

  const question = process.argv[2] ?? 'How many rows are in the largest table in this space?';
  console.log(`\n  asking Genie: "${question}"`);

  let started;
  try {
    started = await startConversation(cfg, question);
    console.log(ok('start-conversation accepted'));
  } catch (e) {
    console.error(bad(`start-conversation failed: ${e.message}`));
    if (/401|403/.test(e.message)) {
      console.error('    Check DATABRICKS_TOKEN is valid and has access to this Genie space.');
    }
    if (/404/.test(e.message)) {
      console.error('    Check DATABRICKS_GENIE_SPACE_ID — it is the id in the Genie space URL.');
    }
    process.exit(1);
  }

  const conversationId = started.conversation_id ?? started.conversation?.id;
  const messageId = started.message_id ?? started.id ?? started.message?.id;
  if (!conversationId || !messageId) {
    console.error(bad(`unexpected response shape: ${JSON.stringify(started).slice(0, 240)}`));
    console.error('    The adapter expects conversation_id and message_id. If the API has');
    console.error('    changed shape, src/databricks.mjs is where to fix it.');
    process.exit(1);
  }
  console.log(ok(`conversation ${conversationId} · message ${messageId}`));

  let done;
  try {
    done = await waitForMessage(cfg, conversationId, messageId, { timeoutMs: 180_000 });
  } catch (e) {
    console.error(bad(e.message));
    process.exit(1);
  }
  const status = done.status ?? done.state;
  console.log(status === 'COMPLETED' ? ok(`status ${status}`) : bad(`status ${status}`));

  const att = extractAttachments(done);
  console.log(att.sql
    ? ok(`Genie returned its generated SQL (${att.sql.length} chars) — sub-expression analysis is possible`)
    : bad('no SQL in attachments — only the narrative is available, so shape analysis will not work'));
  if (att.text) console.log(ok(`narrative: ${att.text.slice(0, 80).replace(/\s+/g, ' ')}…`));

  if (!att.attachmentId) {
    console.log(bad('no query attachment — cannot fetch result rows, so value-grounding will not work'));
    process.exit(1);
  }
  try {
    const rows = rowsFromResult(await getQueryResult(cfg, conversationId, messageId, att.attachmentId));
    console.log(ok(`result rows readable (${rows.length}) — value-grounding will work`));
    if (rows[0]) console.log(`     first row: ${JSON.stringify(rows[0]).slice(0, 90)}`);
  } catch (e) {
    console.log(bad(`query-result fetch failed: ${e.message}`));
    process.exit(1);
  }

  console.log('\n  Next: node src/databricks-agent.mjs "<question>"');
  console.log('  Note: Genie authors the SQL, so there is no trace context to inject');
  console.log('  and no per-query credit attribution — see docs/DATABRICKS.md.');
}

main().catch((e) => { console.error('error:', e.message); process.exit(1); });
