# agent-data-observability

**A trace primitive for agent→database traffic, and the cost analysis it makes possible.**

AI agents issue orders of magnitude more database queries than the humans they replace, and most of those queries are redundant speculation. Warehouse query logs can tell you *what* ran. They cannot tell you *which reasoning step* ran it — so "redundant" stays undefinable, and every warehouse cost tool is optimizing warehouses instead of queries.

This repo implements the missing layer and measures what it finds.

---

## The argument

```
identity → plan lineage → redundancy classification → dedup/approximation → savings
```

Each link requires the one before it. Existing cost tools (Keebo, Espresso AI, SELECT.dev) start at step four, because steps one through three aren't recoverable from query history alone. Observability isn't a go-to-market wedge that happens to come first here — it's the technical precondition.

## How it works

Trace context is injected as a [sqlcommenter](https://google.github.io/sqlcommenter/)-style SQL comment. The warehouse logs it verbatim. The plan tree is reconstructed **offline from a log file the warehouse already writes** — nothing sits in the data path.

```
2026-07-28 23:41:07.882 PDT [16233] LOG:  statement:
  /*agenttrace:t=0ae579c0…,s=a17f2b,p=6c9e04,a=revenue-analyst-v3,c=probe,i=daily%20volume…*/
  select count(order_id), sum(amount) from orders where order_date = '2026-07-12'
```

Zero added latency, zero interception, nothing to trust. The same middleware seam is where an in-path mode (dedup / serve-from-cache / approximate-first) would later flip on behind a config flag.

One field cannot come from the database at all: `used_downstream`, whether a query's result actually reached the agent's final answer. That requires agent-side instrumentation, and it's what turns "you ran 93 queries" into a number a budget owner reacts to.

## Run it

Requires Node 20+ and a local PostgreSQL installation (`initdb`, `pg_ctl`, `psql` on PATH). The demo script creates a throwaway cluster on port 55432 — it does not touch any existing Postgres instance.

```bash
npm install && ./scripts/demo.sh
```

This seeds 4.4M rows, runs a simulated analyst agent, and prints the reconstructed trace with cost attribution. Takes a couple of minutes, mostly seeding.

---

## Results

Two agents answered the same question — *"why did revenue drop in July 2026?"* — against the same 4.39M rows, traced identically.

| | Simulated agent | **Real agent** (Claude Opus 5 via Claude Code) |
|---|---|---|
| Queries issued | 93 | **7** |
| Distinct sub-plans after subsumption | 9.7% | **57.1%** |
| Results that reached the answer | 4 / 93 | **7 / 7** |
| Bill that was idle warehouse time | ~99% | **96.7%** |
| Cost per resolved task | $0.36–$0.46 | **$0.073** |
| Saving from batching advice alone | 7–9× | **1.4×** |

**The real agent largely refutes the simulated one.** It found the planted cause — a per-order value collapse confined to EMEA × paid_search starting 2026-07-12 — in seven queries, wrote its own `GROUP BY` rollups instead of fanning out per-day probes, and cited every result it retrieved. The speculation pattern the simulator models, and which motivates most of the optimization thesis, did not appear.

Reproduce with `./scripts/demo.sh` (simulated) and `node src/real-agent.mjs` (real, requires an authenticated `claude` CLI). Raw output from the real run: [`docs/runs/real-agent-report.txt`](docs/runs/real-agent-report.txt).

### What held up, and what didn't

**Did not reproduce — speculation waste.** The simulator spent 85% of its bill on queries whose results never reached the answer. The real agent's figure was **zero**. A capable model with a well-described tool does not appear to flail the way the fan-out model assumes.

**Did not reproduce at strength — redundancy.** Subsumption caught 90.3% of the simulated workload and **42.9%** of the real one. More importantly, the real agent's covering anchors were *observed* queries, not synthesized ones — it had already written the rollups a materialization recommender would have suggested. The synthesized-candidate insight, which is the strongest technical idea here, had almost nothing to do on a real trace. Treat the 42.9% as soft besides: the shape extractor mishandles the positional `GROUP BY 1, 2` and `date_trunc()` expressions real agents write (visible in the anchor SQL in the saved report), so the real-trace covering set is less trustworthy than the simulated one.

**Reproduced, directionally — the idle tax.** 96.7% of the modelled bill was warehouse time spent waiting on the model rather than executing SQL. That survives, and it is the finding that does not depend on agents being wasteful — it only requires them to be *slow between queries*, which is intrinsic. But the magnitude collapses with the query count: batching saves 1.4× on the real trace, not 7–9×, and the whole task costs $0.073.

**Still true regardless: exact-match caching is worthless here.** Literal SQL matching caught 0% of the real workload and 1.1% of the simulated one; AST normalization added ~1–2%. Whatever value exists in this layer is in view-matching, not in the prompt-level semantic caching today's LLM gateways ship.

### What that means

The observability primitive stands — the trace reconstructed cleanly from the Postgres log for both agents, and cost-per-resolved-task is measurable where it wasn't before. The *optimization* thesis built on top of it is much weaker than the simulation suggested: on this evidence, the money is in scheduling and warehouse economics, not in deduplicating agent speculation.

A fuller write-up with the reconstructed plan tree is in [`docs/design-note.html`](docs/design-note.html) (written against the simulated run — read this section first).

---

## What this is not

Read this section before citing any number above.

- **One real run, one task, one harness.** The real-agent result is n=1: a single question, with a single planted single-cause answer, run through Claude Code with Claude Opus 5 and one well-described tool. A vaguer question, a weaker model, a worse tool description, or a harness that encourages parallel fan-out could all produce a very different shape. Do not read "agents don't speculate" into it — read "this agent, on this task, didn't."
- **The simulator models a naive agent, and says so.** Its speculation *shape* — probe fan-out, cosmetic retries, per-partition scans, a dead-end hypothesis — is modelled on the patterns described in [Intelligence is Free, Now What?](https://bair.berkeley.edu/blog/2026/07/07/intelligence-is-free-now-what/) (BAIR, July 2026) and the UC Berkeley EPIC Data Lab's [agent-first data systems](https://arxiv.org/pdf/2509.00997) work. Its 9.7% distinct-sub-plan figure landing inside their reported 10–20% range was a consistency check, never independent confirmation — and the real run above did not reproduce it. The simulator is kept because it is a deterministic fixture for the fingerprinting code, not because it is evidence.
- **Lineage is recorded differently in the two runs.** The simulator's plan tree is assigned by the harness. The real agent's is *self-declared* — the `run_sql` tool asks the model for `intent` and `follows_from`, so the tree is the agent's own account of its reasoning and inherits whatever inaccuracy that carries.
- **Fingerprinting uses a tokenizer, not a SQL parser.** Tiers 2 and 3 use regex normalization and a structured extractor for the common aggregate shape. That's an appropriate shortcut for sizing a prize; a production version needs sqlglot or Calcite behind the same interface, and the hit rates will move.
- **Cost figures model Snowflake but run on Postgres.** Billing is computed under Snowflake XS rules ($3/credit, 60s minimum, 60s auto-suspend) applied to real Postgres execution times. Treat the *ratios* as the finding, not the absolute dollars.
- **Agent think-time is compressed 100× in the simulation** and scaled back up before billing. Query execution time is real and never scaled. See `src/config.mjs`.
- **Single trace, single warehouse size.** No claim of generality across workloads.
- **Numbers move between runs.** Query execution time varies with machine load, and wall-clock elapsed time varies with it, so dollar figures and the savings multipliers shift run to run (observed: $0.36–$0.46 per task, 7–9× batching, 72–84% compute reduction). The structural results — 93 queries, 9 covering anchors, 4 cited results, ~99% idle — reproduced exactly. Trust the structure, not the decimals.

## Layout

| Path | What it does |
|---|---|
| `src/context.mjs` | Trace context; sqlcommenter-style serialization and parsing |
| `src/tracedb.mjs` | The middleware. Out-of-path in this version; the seam for in-path mode |
| `src/fingerprint.mjs` | Three-tier fingerprinting, subsumption, candidate synthesis, greedy cover |
| `src/agent-sim.mjs` | Simulated analyst agent — a deterministic fixture, not evidence |
| `src/real-agent.mjs` | Drives a real Claude Code agent headlessly against the traced warehouse |
| `src/mcp-db-server.mjs` | MCP server exposing `run_sql` to a real agent; injects trace context |
| `src/assemble.mjs` | Log parsing, tree reconstruction, billing model, report |
| `seed.sql` | 4.4M-row demo dataset with a planted July revenue drop |

## Open items

- [x] ~~Replace the simulator with a real LLM agent loop~~ — done, and it changed the conclusions
- [ ] More real runs: vaguer questions, multi-cause data, weaker models, parallel-fan-out harnesses. n=1 is not a result
- [ ] Swap regex normalization for a real SQL parser (sqlglot / Calcite) — required before any covering-set number on real agent SQL is trustworthy
- [ ] Snowflake `QUERY_TAG` and BigQuery label adapters alongside Postgres comments
- [ ] `used_downstream` hooks for real agent frameworks
- [ ] Cost of the telemetry itself — a system that reduces a data bill shouldn't create one

## Prior art

Distinct from, and complementary to: **MCP/agent gateways** (Snowflake Cortex AI Gateway, MintMCP) which govern access rather than economics; **LLM semantic caches** (Bifrost, LiteLLM) which cache prompts rather than queries; **warehouse cost optimizers** (Keebo, Espresso AI) which tune warehouses rather than query semantics; and **semantic layers** (Cube, dbt) which govern correctness rather than spend.

## License

MIT
