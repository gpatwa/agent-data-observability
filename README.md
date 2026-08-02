# agent-data-observability

**A harness for measuring what AI agents actually do to your data warehouse — and the negative result it produced.**

The premise, from [Intelligence is Free, Now What?](https://bair.berkeley.edu/blog/2026/07/07/intelligence-is-free-now-what/) (BAIR, 2026) and the UC Berkeley EPIC Data Lab's [agent-first data systems](https://arxiv.org/pdf/2509.00997) work: agents issue vast numbers of near-duplicate speculative queries, only 10–20% of their sub-plans are distinct, and there is a large optimization prize in deduplicating that traffic.

I built the trace primitive needed to measure it, then measured it against real agents.

**The prize did not show up in agent traffic.** It showed up somewhere else entirely: real human and pipeline warehouse traffic is **~10x more redundant than anything the agents produced**. This repo is the harness, the evidence, and an honest account of what failed.

📉 **[Read the findings](https://gpatwa.github.io/agent-data-observability/)** — six conditions, one chart, and the six bugs I hit getting there.

---

## Headline

Six conditions, chosen to give the thesis its best shot. Redundancy is measured against queries a rollup could actually serve — schema lookups, joins and CTEs are excluded rather than counted as deduplicated.

| Condition | Queries | Servable | Anchors | **Redundancy** | Grounded |
|---|---|---|---|---|---|
| Simulated naive agent | 93 | 89 | 9 | **~90%** | 4 / 93 |
| Real agent, Opus 5 | 6 | 7 | 4 | 42.9% | 6 / 6 |
| Real agent, Haiku 4.5 | 5 | 3 | 3 | 0% | 4 / 5 |
| 8 concurrent sessions | 41 | 25 | 20 | 20.0% | 34 / 41 |
| Wide schema, 120 tables | 17 | 7 | 4 | 42.9% | 16 / 17 |
| **Coordinator + subagents** | 25 | 11 | 10 | **9.1%** | 25 / 25 |
| **Human/pipeline baseline** (Redset) | 18.9M | — | — | **91.3%** median | n/a |

Only the simulator — the one built from the published description rather than observed — reaches the redundancy the thesis needs. **The two conditions most like the thesis's own premise, concurrent sessions and a delegating coordinator, produced the least of it.**

And the last row is the one that reframes everything.

**Cross-session redundancy: 13.0%.** Sharing rollups across eight agents' worth of overlapping questions eliminated 3 of 23 per-session anchors.

---

## The six claims, and how each died

**1. Agents waste most of their queries on speculation that never reaches the answer.** *Refuted.*
The simulated agent used 4 of 93 results. Real agents used **34 of 41** across eight sessions, and this is verified rather than self-reported — see "Verifying citations" below. One session out of eight (`refunds`, 3/9 grounded) showed real waste; it is the exception, not the pattern.

**2. Cross-session sharing is where the redundancy really lives.** *Too small to build on.*
This was the strongest surviving reframe after the single-agent result: no agent need be wasteful, only for different people to ask related questions of the same tables. Eight agents asking deliberately overlapping questions about the same two tables and date range yielded **13%**. Materialization still helps — 20 anchors serve 25 queries — but that is a normal data-warehouse optimization, not an agent-specific one.

**3. Weaker models will fan out, and cheap intelligence means weak models at scale.** *Refuted.*
This was the strongest theoretical argument, and directly what "intelligence is free" implies. Haiku 4.5 on the identical question issued **5 queries to Opus's 6**, with the same tight investigative structure. No fan-out.

**4. You are paying a warehouse to watch an LLM think.** *An artifact of measuring one agent.*
True for a lone agent on a dedicated warehouse: 96.7% of the bill was idle. But with **8 concurrent agents sharing one warehouse, idle fell to 23.7%** — 851 of 1116 billed seconds were productive. Agents' think-time gaps interleave. The finding was real and the conclusion was wrong.

**5. A coordinator delegating to parallel subagents will duplicate work.** *Refuted — and it went the other way.*
This was the thesis's best remaining home: independent investigators, no shared context, each re-deriving what the others already computed. The coordinator issued 25 queries across delegated subagents and produced **9.1% redundancy — the lowest of any condition tested**, with 25/25 results grounded in the answer. Delegation bought *more distinct* work, not duplicated work, because each subagent was handed a genuinely different angle.

**6. A wide schema will make agents flail.** *Partly true, and it doesn't help.*
Burying `orders`/`refunds` in 120 plausible decoy tables and withholding the schema nearly tripled query count, 6 → **17**. But the extra queries were `information_schema` lookups, and redundancy among *servable* queries was **42.9% — identical to the narrow-schema run**. Schema discovery is real traffic that a rollup cache cannot serve; it is cacheable only in the trivial sense that a catalog is static.

## The baseline that changes the conclusion

Every number above was measured on agent traffic, with nothing to compare it against. [Redset](https://github.com/amazon-science/redset) — Amazon's published trace of real Amazon Redshift production fleets — provides the missing control.

Scored with **this repo's own metric** (`1 - distinct/total`) over **18.9M SELECT queries across 20 production clusters**:

| | Redundancy |
|---|---|
| Median cluster | **91.3%** |
| p25 – p75 | 71.7% – 96.6% |
| Range | 58.1% – 100% |
| Clusters above 80% | **70%** |

Against agents at 0–42.9%, and a delegating coordinator at 9.1%.

**So the deduplication prize is real, and it is enormous — it just does not belong to agents.** It belongs to the human dashboards and scheduled pipelines the warehouse is already running, where the same queries repeat by construction. Redshift ships result caching for exactly this reason (`was_cached` is a column in the trace).

That inverts the original thesis rather than merely refuting it. Building agent-specific query deduplication means **optimizing the least redundant traffic in the warehouse.** An agent investigating a question asks a sequence of different questions; a dashboard asks the same one every fifteen minutes forever.

Reproduce with `npm run baseline` (streams from S3, stores nothing locally). Saved output: [`docs/runs/redset-baseline.txt`](docs/runs/redset-baseline.txt).

**Caveats, and they matter:**
- Redset carries **no SQL text**, so the shape/subsumption analysis cannot run on it. `feature_fingerprint` is "a proxy for query-likeness, not based on text" — closest to this repo's `ast_hash` tier, but not the same function. The comparison is like-for-like in *metric definition*, not in *fingerprint definition*.
- It is Redshift, 2024, human + scheduled-pipeline traffic. Scheduled ETL repeats by design, and that is part of the point — but it means these are different *kinds* of workload, not the same workload with different drivers.
- 20 of 200 provisioned instances, `SELECT` only, clusters with ≥100 queries.
- **Redset is CC BY-NC 4.0 — non-commercial use only.** Attribute Amazon; do not reuse these numbers in a commercial product without checking the licence.

## What survived

The **trace primitive**. Query lineage, per-agent cost attribution, and verified answer-grounding all reconstruct cleanly from a log the warehouse already writes, with nothing in the data path. `cost per resolved task` — the number that decides whether an agent deployment survives budget review — becomes measurable where it wasn't before.

That's an observability tool, not an optimization platform. **The optimization layer in this repo is measured, small, and deliberately not built out further.**

---

## How it works

Trace context is injected as a [sqlcommenter](https://google.github.io/sqlcommenter/)-style SQL comment. The warehouse logs it verbatim. The plan tree is reconstructed offline.

```
2026-07-28 23:41:07.882 PDT [16233] LOG:  statement:
  /*agenttrace:t=673e90a1…,s=a17f2b,p=6c9e04,a=claude-code-analyst,c=refine,i=revenue%20by%20region*/
  SELECT date_trunc('month', order_date), region, sum(amount) FROM orders GROUP BY 1, 2
```

Zero added latency, zero interception, nothing to trust. For real agents the warehouse is exposed as a single `run_sql` MCP tool, and lineage is **self-declared** — the tool asks the model for `intent` and `follows_from`.

### Verifying citations, not trusting them

`used_downstream` — did this query's result reach the answer? — is the field the whole waste analysis rests on, and the obvious implementation (ask the agent) is the agent grading its own homework. So it is checked against evidence: scalar values from each result set are matched against the final answer, with numeric tolerance for the rounding models do in prose (`$319.9M` for `319875432.11`).

It catches errors in both directions. Haiku claimed 3 queries and was grounded in 4 — one query it used but never claimed. Opus claimed 6 and all 6 were grounded, 5 of them by a value no other query produced.

---

## Run it

Requires Node 20+, a local PostgreSQL (`initdb`/`pg_ctl`/`psql` on PATH), and — for real agents — an authenticated `claude` CLI.

```bash
npm install
npm test                  # 31 unit tests, no database needed
./scripts/demo.sh         # seeds 4.4M rows, runs the simulated agent
node src/real-agent.mjs "Why did revenue drop in July 2026?"
node src/real-agent.mjs "..." --model claude-haiku-4-5 --tag haiku
node src/real-agent.mjs "..." --wide --tag wide        # 120-table schema, hidden
node src/real-agent.mjs "..." --subagents --tag coord  # delegating coordinator
node src/cross-session.mjs --concurrency 4            # 8 agents, ~$2 of LLM spend

psql -f seed-wide.sql   # adds the 120 decoy tables (needed for --wide)
```

The demo creates a throwaway cluster on port 55432; it does not touch an existing Postgres instance. Raw output from every run described above is in [`docs/runs/`](docs/runs/).

---

## What I got wrong along the way

These are in the repo history, and they are the most useful part of it.

- **Applied the simulator's 100× think-time dilation to a real trace.** Reported a 2708-second task with 7 warehouse resumes; the truth was 27 seconds and 1 resume. Dilation is now an explicit CLI argument defaulting to 1.
- **Substring-matched numeric values.** Postgres returns `numeric` as a string, so result values were compared as text against prose. `"69.33124..."` never appears in an answer that says `$69.33`, so grounding was under-reported as 3/6 when it was really 6/6.
- **Regex-"parsed" SQL.** The original extractor could not read `GROUP BY 1, 2`, `date_trunc()`, casts, or aliases — exactly what real agents write — and emitted dimensions literally named `"1"` and `"2"`. Every covering-set number computed on real SQL before [`src/shape.mjs`](src/shape.mjs) was noise.
- **Divided by the wrong denominator.** Reported "51% dedup" by counting queries the parser *couldn't model* as deduplicated. Against queries the anchors can actually serve, it is 20%.
- **Named the subagent tool `Task` instead of `Agent`.** The coordinator condition ran with delegation silently disabled — `--allowedTools` does not error on an unknown tool name, so the model simply never delegated and the run looked like a perfectly valid single-agent trace. Caught only by checking backend connections and turn count against expectation, then probing which tool name actually spawns subagents. The first coordinator result was invalid and was rerun.
- **Line-anchored the citation regex.** Models write `**CITED: q1, q4**`; a `/^CITED:/` scored that run as citing nothing.

Every one of these moved a headline number, and three of them moved it in the flattering direction.

---

## Not production software

This is a measurement harness. It is **not** ready to point at a production warehouse, and the gaps are structural rather than polish:

- **Postgres only.** Snowflake pricing is *modelled*, not measured. No `QUERY_TAG` / BigQuery label adapters.
- **The server is the database, not a wrapper.** It holds one `pg.Client` with a hardcoded connection and serialises every agent through it. A real deployment must wrap the warehouse client you already have.
- **Result values are written to disk in plaintext** (`out/*-events.jsonl`) for the grounding check. That is warehouse data in a log file — a data-exfiltration surface and a compliance problem anywhere real.
- **Only ~1 in 7 realistic analytics queries can be modelled at all.** Joins, CTEs, subqueries, `OR`, `HAVING` and window functions are all declined. Real analytics SQL is mostly joins, so the covering-set analysis would say almost nothing about a production workload.
- **No authentication, multi-tenancy, quotas, or retention.** Trace context is an unauthenticated SQL comment — an agent can forge its own `intent` and `follows_from`, so this cannot underpin chargeback.
- **Events are JSONL on local disk** and log parsing reads whole files into memory.

The read-only guard was also genuinely broken until recently — it pattern-matched the start of the string, so `select 1; drop table x` passed and the DROP executed. It now parses and requires exactly one SELECT, and the session sets `default_transaction_read_only`. **Even so, run it against a SELECT-only database role.** An application-layer allowlist in front of a read-write connection is not a security boundary.

## Prior art, and what I would use instead

I hand-rolled two things that already have standards. If you are evaluating this repo, you should know I am aware of that rather than assume I wasn't.

**[OpenTelemetry database semantic conventions](https://opentelemetry.io/docs/specs/semconv/db/database-spans/) + [sqlcommenter](https://opentelemetry-python.readthedocs.io/en/latest/examples/sqlcommenter/README.html).** This is what `src/context.mjs` and `src/trace.mjs` are, reinvented. The OTel spec covers `db.query.text`, sanitization, query summarization, context propagation and sqlcommenter explicitly. My `/*agenttrace:t=…,s=…,p=…*/` is a nonstandard spelling of `/*traceparent='00-…'*/`. Using the spec would delete the log parser, the span assembler and the report renderer, because any OTel backend — Tempo, Honeycomb, Datadog, Jaeger — renders the trace for free. The agent dimension is just span attributes.

**[sqlglot](https://github.com/tobymao/sqlglot) instead of node-sql-parser.** 30+ dialects including Snowflake, Spark/Databricks and BigQuery, plus a transpiler, an optimizer and column-level lineage. The 1-in-7 modelling rate reported above is partly this repo's parser hitting its ceiling. sqlglot would raise it *and* provide multi-warehouse dialect support in the same move. It is Python, so it means a sidecar or a port.

**[ADBC](https://arrow.apache.org/adbc/current/index.html) for connecting many warehouses.** Vendor-neutral, Arrow-native, DB-API compliant so Ibis and SQLAlchemy sit on top of it; dbt Fusion standardized on it. [Ibis](https://ibis-project.org/) if you want to express a query once across backends rather than only connect to them.

**Adjacent, but solving something else:** warehouse cost tools (Select.dev, Keebo, Espresso AI) optimize warehouses rather than query semantics and have no notion of which agent or reasoning step issued a query; MCP gateways (Snowflake's Cortex AI Gateway, MintMCP) govern access rather than economics. I could not find anything doing per-agent query lineage.

**If I started again:** OTel + sqlcommenter for the trace, sqlglot for the shapes, and Snowflake `QUERY_TAG` joined to [`QUERY_ATTRIBUTION_HISTORY`](https://docs.snowflake.com/en/sql-reference/account-usage/query_attribution_history) for *measured* rather than modelled cost — which would also let me check the cost figures published here, with less code than they currently take.

## What this is not

- **Small n.** One dataset, two models, six conditions, one run each. Not a study — a cheap screen.
- **The dataset has a single planted cause.** Real investigations are messier and may fan out more.
- **`unmodelled` is not zero.** The parser declined 12 of 41 real queries rather than guess. Excluding lowers reported coverage; mis-parsing would inflate it. Excluding is the honest failure, but it means coverage figures are lower bounds.
- **Cost figures model Snowflake, run on Postgres.** Snowflake XS billing rules applied to real Postgres execution times. Treat the ratios as the finding, not the dollars.

## Layout

| Path | What it does |
|---|---|
| `src/context.mjs` | Trace context; sqlcommenter-style serialization |
| `src/shape.mjs` | **AST-based** query shape, subsumption, candidate synthesis |
| `src/fingerprint.mjs` | Exact/normalized hashing; re-exports shape.mjs |
| `src/trace.mjs` | Log parsing, span reconstruction, Snowflake billing model |
| `src/mcp-db-server.mjs` | MCP server exposing `run_sql`; injects trace context |
| `src/real-agent.mjs` | Drives a real Claude Code agent headlessly |
| `src/cross-session.mjs` | Fans out N agents; covering set across the union of traces |
| `src/verify-citations.mjs` | Value-grounded citation verification |
| `src/agent-sim.mjs` | Simulated naive agent — a deterministic fixture, not evidence |
| `seed-wide.sql` | 120-table decoy schema for the wide-schema condition |
| `test/` | 31 tests, weighted toward negative subsumption cases |

## If you want to revive the thesis

Both conditions I thought most likely to revive it — a delegating coordinator and a 120-table schema — have now been run, and both came back negative. What remains untested, in order of promise:

- **Genuinely ambiguous questions with no single cause.** Every run here had one planted answer to find.
- **A much larger warehouse** where individual queries are slow enough that materialization pays for itself on latency alone, independent of redundancy.
- **Agents with memory across sessions**, which might re-derive prior context rather than re-reading it.

`node src/real-agent.mjs "<question>" [--wide] [--subagents] [--model X]` runs any of them.

## License

MIT
