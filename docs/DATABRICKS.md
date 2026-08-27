# Databricks Genie pilot

> ✅ **Live-validated.** `adobs-databricks-check` against a real workspace
> (Free Edition, `samples.tpch`): conversation started, reached `COMPLETED`,
> Genie returned 765 chars of generated SQL, and result rows were readable
> (`lineitem`, 29,999,795 rows) — first real run, no shape-handling fixes
> needed. `agent_data_observability/databricks.py` is still where to look if
> a different workspace ever returns a shape the defensive fallbacks miss.

## Why this adapter is different, and why that matters

The other adapters inject trace context. This one cannot.

| | Postgres | Snowflake | **Genie** |
|---|---|---|---|
| Who writes the SQL | the agent | the agent | **Genie** |
| Trace injection | SQL comment | `QUERY_TAG` | **none possible** |
| How we recover it | parse server log | `ACCOUNT_USAGE` | **from the API response** |
| Per-query credit attribution | modelled | `QUERY_ATTRIBUTION_HISTORY` | **not available** |
| Sub-expression analysis | ✅ | ✅ | ✅ *(Genie returns its SQL)* |
| Value-grounding | ✅ | ✅ | ✅ |

**This is the deployment shape a real product faces.** Verification sitting above
a managed connection you do not own. Databricks terminates the connection inside
its own service, runs the query under Unity Catalog's row filters and column
masks, and hands back a result. There is no hook for us in the middle.

What survives is what matters for answer verification: Genie returns **the SQL it
generated** in the message attachments, and the **result rows** via the
query-result endpoint. So sub-expression redundancy analysis and value-grounded
citation checking both still work. What does not survive is cost attribution —
that would need `query_tags` we have no way to set, since the statement is
Genie's, not ours.

## Setup

```bash
# in .env at the repo root (gitignored), or exported in your shell
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
DATABRICKS_TOKEN=<personal access token>
DATABRICKS_GENIE_SPACE_ID=<id from the Genie space URL>
```

A personal access token is the quickest path; OAuth bearer tokens work in the
same header. The token needs access to the Genie space, and **the space's own
Unity Catalog permissions still apply** — an agent using your token sees exactly
what you would see, no more.

## Run it

```bash
adobs-databricks-check                         # preflight — same code path as the agent
adobs-databricks-agent "<question>"            # real Claude Code agent, ask_genie only
```

The preflight asks Genie one question and reports four things independently:
whether the conversation started, whether it reached `COMPLETED`, whether SQL
came back in the attachments (without it, shape analysis is impossible), and
whether result rows are readable (without them, value-grounding is impossible).
It exercises the same functions the agent uses rather than a shortcut — an
earlier preflight in this repo wrote its own version of the call and passed
while production was silently broken.

## The tool the agent sees

`ask_genie(question, intent, follows_from)` — plain English, not SQL. The agent
does not author queries here, which is a real behavioural difference from the
other conditions and worth accounting for when comparing traces: an agent that
cannot write SQL cannot fan out across query variants the way the Postgres and
Snowflake agents did.

`intent` and `follows_from` are still requested so the plan tree can be
reconstructed from the agent's own declared reasoning, exactly as elsewhere.

## Known unknowns

- **Response shapes were originally from docs, not observation** — since
  confirmed against a live workspace (see the banner above): `conversation_id`
  / `message_id`, the `attachments` array layout, and the statement-execution
  result envelope all matched on the first real preflight, no fallback paths
  needed. One workspace and one query is not exhaustive coverage, so the
  defensive handling stays in place.
- **Genie is stateful.** The server reuses one conversation for the session, so
  follow-up questions carry context. That is closer to how Genie is meant to be
  used, and it means traces are not independent the way the same-task
  replication requires.
- **Polling ceiling is 5 minutes** per question, with a bounded loop. A cold
  serverless warehouse can take a while on the first call.
- **No read-only guard**, because none is needed — Genie only reads, and Unity
  Catalog enforces access. That is the platform doing the job this harness does
  itself elsewhere.
