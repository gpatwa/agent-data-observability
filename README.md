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

## Results from the included workload

A simulated analyst agent answering *"why did revenue drop in July 2026?"* against 4.39M rows:

| Metric | Value | Stable across runs? |
|---|---|---|
| Queries issued for one question | 93 | yes |
| Distinct sub-plans after subsumption | **9.7%** (9 anchors) | yes |
| Results that reached the answer | 4 / 93 | yes |
| Bill that was idle warehouse time | **~99%** | yes |
| Cost per resolved task | $0.36 – $0.46 | varies with load |

### Three findings

**1. Exact-match caching is worthless here.**

| Fingerprint tier | Catches | Hit rate |
|---|---|---|
| `exact` | literal retries | 1.1% |
| `ast_hash` | alias renames, predicate reordering, whitespace | 2.2% |
| `subsumption` | probes answerable from a coarser result | **90.3%** |

Agents rarely repeat a query exactly — they *probe around a region*. Semantic caching as shipped by today's LLM gateways operates on prompts and would catch essentially none of this. The prize is in view-matching and multi-query optimization.

**2. The useful anchors are queries nobody ran.** An agent that fires 31 per-day probes never issues the `GROUP BY order_date` rollup that would answer all 31. Picking anchors from *observed* queries finds almost nothing (68 anchors for 89 queries — useless). Candidates have to be **synthesized** by lifting equality filters up into the grouping. After that, 9 anchors cover 85 of 89. The anchors are then *executed*, not estimated: roughly 650–770ms of anchor time against 2.7–4.1s of probe execution, or **72–84% less compute** for the same answers.

**3. You are paying a warehouse to watch an LLM think.** Agent think-time between probes is only a few seconds, so a 60-second auto-suspend never fires. The warehouse stays hot for the entire 6–8 minute task while doing 3–4 seconds of work. This yields a **7–9× saving from scheduling advice alone** — no query rewriting, no interception.

A fuller write-up with the reconstructed plan tree is in [`docs/design-note.html`](docs/design-note.html).

---

## What this is not

Read this section before citing any number above.

- **The agent is simulated, not a real LLM loop.** The speculation *shape* — probe fan-out, cosmetic retries, per-partition scans, a dead-end hypothesis — is modelled on the patterns described in [Intelligence is Free, Now What?](https://bair.berkeley.edu/blog/2026/07/07/intelligence-is-free-now-what/) (BAIR, July 2026) and the UC Berkeley EPIC Data Lab's [agent-first data systems](https://arxiv.org/pdf/2509.00997) work. That the 9.7% distinct-sub-plan figure lands inside their reported 10–20% range is a consistency check, **not** independent confirmation. Replacing the simulator with a real agent loop is the top open item.
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
| `src/agent-sim.mjs` | Simulated analyst agent producing a realistic speculation tree |
| `src/assemble.mjs` | Log parsing, tree reconstruction, billing model, report |
| `seed.sql` | 4.4M-row demo dataset with a planted July revenue drop |

## Open items

- [ ] Replace the simulator with a real LLM agent loop against the same schema
- [ ] Swap regex normalization for a real SQL parser (sqlglot / Calcite)
- [ ] Snowflake `QUERY_TAG` and BigQuery label adapters alongside Postgres comments
- [ ] `used_downstream` hooks for real agent frameworks
- [ ] Cost of the telemetry itself — a system that reduces a data bill shouldn't create one

## Prior art

Distinct from, and complementary to: **MCP/agent gateways** (Snowflake Cortex AI Gateway, MintMCP) which govern access rather than economics; **LLM semantic caches** (Bifrost, LiteLLM) which cache prompts rather than queries; **warehouse cost optimizers** (Keebo, Espresso AI) which tune warehouses rather than query semantics; and **semantic layers** (Cube, dbt) which govern correctness rather than spend.

## License

MIT
