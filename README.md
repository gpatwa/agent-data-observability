# agent-data-observability

**See what your AI agents cost you on the warehouse — per agent, per question — and whether that spend produced a correct answer.**

Agents now write SQL against Snowflake, Databricks and Postgres on their own. The tools that watch this split down the middle. Warehouse cost tools (Select.dev, Keebo, Espresso AI) see compute, but not which agent caused it or why. LLM observability (Langfuse, LangSmith, Arize, Datadog) sees model calls, but treats each query as one opaque span. Nobody connects **agent → question → query → credits → answer**, and nobody checks whether the spend actually reached a correct answer.

This repo does that, from a record the warehouse already keeps, with nothing in the data path.

## What it does today

Each of these is built and measured, not proposed:

| | What you get | Measured |
|---|---|---|
| **Cost per agent and per question** | Trace context rides in a SQL comment or Snowflake `QUERY_TAG`. Every query is attributed to the agent, session and reasoning step that issued it | Snowflake: **$0.0897** per resolved task from `QUERY_ATTRIBUTION_HISTORY`, against $0.073 modelled |
| **Which spend reached the answer** | Values from each result are checked against the agent's final answer. The agent's own claim about what it used is not trusted | Caught an agent claiming 3 queries when it used 4. Live run on DuckDB: 2/2 claims grounded |
| **Where spend repeats** | Every query is parsed into its parts (scan, filter, measure, grouping), so repetition shows up even when the SQL text differs | 25 agents on one question: **11.8%** of query parts distinct, against 65.3% of whole queries |
| **Cutting it** | Shared rollups are materialized once and answer many different agent queries, with each answer checked against the warehouse | 44 of 89 queries served from 9 rollups, **44/44** verified, ~30x lower latency |

It runs against Postgres, Snowflake, Databricks Genie and DuckDB. Each adapter documents what it can and can't independently verify.

**Not built yet, and next:** one trace that joins LLM token cost to warehouse credits for the same question. The two halves exist today; the per-run LLM cost is recorded and Snowflake credits are measured, but they don't yet land in one report. See [Roadmap](#roadmap).

📉 **[Read the research behind it](https://gpatwa.github.io/agent-data-observability/)**

---

## Proof: the research behind it

This started as a test of a published claim: [Intelligence is Free, Now What?](https://bair.berkeley.edu/blog/2026/07/07/intelligence-is-free-now-what/) (BAIR, 2026) and the UC Berkeley EPIC Data Lab's [agent-first data systems](https://arxiv.org/abs/2509.00997) paper say agents issue vast numbers of overlapping speculative queries, that only 10–20% of sub-plans are distinct, and that there is a large prize in sharing that computation.

I built the tracing, ran six conditions, and published that **the claim did not reproduce. That was wrong, and the error was mine.** I measured a different quantity than the one the claim is about. Run properly — *N agents attempting the **same** task*, redundancy counted over **sub-expressions** — it reproduced: 17.7% distinct on the first 8-attempt run, and **11.8%** on a 25-attempt rerun, inside the stated range. The direction is stable; the exact percentage depends on how many attempts you count.

The measurement record matters for the product: the cost and redundancy numbers above come from the same harness, and the bugs it hit are listed in [What I got wrong](#what-i-got-wrong).

### The correction

The published measurement is specific. From the EPIC Lab paper: the BIRD text-to-SQL benchmark, **50 independent attempts per task**, redundancy defined as *"the proportion of distinct sub-expressions relative to total sub-expressions across multiple agent attempts."*

Everything I ran differed on **both** axes:

| | The published claim | What I measured first |
|---|---|---|
| Setup | N agents, **same** task | agents on **different** questions |
| Unit | **sub-expressions** in the plan | **whole queries** |
| Verdict | 10–20% distinct | "42.9%, doesn't reproduce" |

So I refuted a neighbouring claim and reported it as the claim. Running it properly, with 8 agents on one question (the first run):

| Level | Distinct | Reading |
|---|---|---|
| Whole queries, exact SQL | 54/55 = **98.2%** | essentially no repetition |
| Whole queries, AST-normalized | 51/55 = **92.7%** | still none |
| **Sub-expressions (all)** | 74/419 = **17.7%** | **massive sharing** |

Broken out by sub-plan piece:

| Piece | Total | Distinct | Distinct % |
|---|---|---|---|
| table scan | 47 | 2 | **4.3%** |
| filter predicate | 115 | 13 | **11.3%** |
| measure (`sum(x)`, `count(*)`) | 117 | 10 | **8.5%** |
| grouping | 46 | 10 | 21.7% |
| filtered scan | 47 | 12 | 25.5% |
| whole aggregate | 47 | 27 | 57.4% |

**Both readings are true simultaneously**, and that is the actual finding. Eight agents asked the same question 55 different ways — but underneath, they scanned the same table with the same 13 predicates computing the same 10 measures. The redundancy is mostly *below* the level of the query (at 25 attempts some whole queries repeat too, but far less than their parts).

#### It moves with n, so I reran it

One 8-attempt run is a point estimate, and the sub-expression number is not stable at that size. Same question, same harness, same model family, rerun later:

| Attempts | Queries | Exact SQL distinct | AST-normalized distinct | **Sub-expressions distinct** |
|---|---|---|---|---|
| 8 (first run) | 55 | 98.2% | 92.7% | **17.7%** |
| 4 (rerun) | 20 | 85.0% | 80.0% | 33.3% |
| 8 (rerun) | 44 | 97.7% | 97.7% | 26.5% |
| **25 (rerun)** | **118** | **76.3%** | **65.3%** | **11.8%** |

Distinct share *falls* as attempts are added, because repeats accumulate; the two 8-attempt runs also differ from each other by 9 points, so run-to-run agent variance is large at this size. At 25 attempts the all-sub-expression figure is **11.8%, inside the paper's 10-20% band** (scan 1.9%, measure 5.7%, filter 7.3%, filtered scan 18.7%, grouping 15.2%, whole aggregate 42.1%). That is still half the paper's 50 attempts, on a different model and dataset, and the sub-expressions are approximated from query shape rather than decomposed from a real plan.

Reproduce: `adobs-same-task --attempts 25`. Saved output: [`docs/runs/same-task-report.txt`](docs/runs/same-task-report.txt) (first run), [`docs/runs/same-task-25-report.txt`](docs/runs/same-task-25-report.txt) (25-attempt rerun, $2.97).

### What that implies

**Result caching captures only a fraction of this, and I overstated it.** I first wrote that a cache keyed on the query — what Redshift, Snowflake and every LLM gateway ship — "hits almost nothing", on 92.7% distinct whole queries at 8 attempts. At 25 attempts whole queries are 65.3% distinct, so a query-keyed cache would have served about a third of them (41 of 118) after the first occurrence. That is real, but the sub-expression level is still where the sharing is: 11.8% distinct versus 65.3%. The larger prize needs **multi-query optimization, shared scans and partial-result reuse**, which is what the paper proposes and what I spent six conditions arguing wasn't needed.

It also explains the human/pipeline baseline below rather than contradicting it. Those workloads repeat *whole queries*; agents repeat *fragments*. They need different machinery.

**That machinery is now built and measured, not just proposed.** [`docs/PHASE2.md`](docs/PHASE2.md) materializes the covering-set rollups this repo's reports have always recommended into a local DuckDB cache, then answers the queries they cover by direct row selection — no re-derivation, so a wrong answer isn't possible by construction, only a declined one. Replayed against the simulated-agent demo trace: 44 of 89 aggregate queries answered from 9 materialized rollups, all 44 verified byte-for-byte against a fresh Postgres execution, 44 real warehouse round-trips avoided, ~30x measured (not modelled) latency.

### Findings that still stand

These were measured correctly and are unaffected — they are about different questions, not the same task:

- **Agents on different questions share little.** 8 concurrent sessions on overlapping-but-distinct questions: 13% cross-session redundancy at whole-query level. A delegating coordinator: 9.1%.
- **Agents waste little.** 34 of 41 results across eight sessions reached the answer, verified by value-grounding rather than self-report. 25/25 for the coordinator.
- **Weaker models do not fan out.** Haiku 4.5 issued 5 queries to Opus 5's 6 on an identical question.
- **The idle-tax finding was an artifact of n=1.** 96.7% of a lone agent's bill is idle warehouse time; with 8 concurrent agents sharing a warehouse it falls to **23.7%**.
- **Human/pipeline traffic repeats whole queries heavily.** [Redset](https://github.com/amazon-science/redset) — 18.9M production Redshift SELECTs across 20 clusters — scored with this repo's metric: **91.3% median** redundancy, 70% of clusters above 80%. (CC BY-NC 4.0, attributed to Amazon.)

### Measured cost, at last

Every dollar figure this project published was **modelled** — Snowflake billing rules applied to Postgres execution times. The Snowflake pilot replaces that with Snowflake's own `QUERY_ATTRIBUTION_HISTORY`:

| | Per resolved task |
|---|---|
| **Measured** (4 attributed queries, 0.029907 credits @ $3) | **$0.0897** |
| Modelled (published figure) | $0.073 |
| Ratio | **1.23×** |

The modelled number was **low by 23%** — the right order of magnitude, wrong in the direction of understating cost. Good enough that the ratios in this repo stand; not good enough to quote as a dollar figure without this caveat.

Reproduce: `adobs-snowflake-cost --hours 72`. Saved: [`docs/runs/snowflake-measured-cost.txt`](docs/runs/snowflake-measured-cost.txt).

Only 4 of 7 tagged queries had credits attributed — Snowflake attributes compute to queries that consumed meaningful warehouse time, so cheap metadata lookups contribute nothing. n=1 trace, TPC-H on an XS warehouse.


## How it works

Trace context is injected as a [sqlcommenter](https://google.github.io/sqlcommenter/)-style SQL comment; the warehouse logs it verbatim; the plan tree is reconstructed offline.

```
2026-07-28 23:41:07.882 PDT [16233] LOG:  statement:
  /*agenttrace:t=673e90a1…,s=a17f2b,p=6c9e04,a=claude-code-analyst,c=refine,i=revenue%20by%20region*/
  SELECT date_trunc('month', order_date), region, sum(amount) FROM orders GROUP BY 1, 2
```

### Verifying citations, not trusting them

Whether a query's result reached the answer is the field the waste analysis rests on, and asking the agent is the agent grading its own homework. Scalar values from each result are matched against the answer text with numeric tolerance for the rounding models do in prose. It catches errors both ways: one agent claimed 3 queries and had used 4; another claimed 6 and had used 6.

## Run it

Python 3.11+, local PostgreSQL, and an authenticated `claude` CLI.

```bash
uv venv && uv pip install -e ".[dev]"           # or: pip install -e ".[dev]"
pytest                                          # unit tests, no database
./scripts/e2e.sh                                # log -> trace pipeline, real Postgres
./scripts/demo.sh                               # simulated agent, 4.4M rows

adobs-same-task --attempts 8                    # THE REPLICATION
adobs-real-agent "..."                          # one agent, one question
adobs-real-agent "..." --subagents              # delegating coordinator
adobs-real-agent "..." --wide                   # 120-table schema, hidden
adobs-cross-session --concurrency 4             # 8 agents, different questions
./scripts/redset-baseline.sh                    # Redset human/pipeline baseline
adobs-phase2-demo                               # materialize + replay Phase 2, measured before/after
```

```bash
adobs-snowflake-check                           # Snowflake, agent authors SQL
adobs-databricks-check                          # Databricks Genie, GENIE authors SQL
adobs-duckdb-check                              # DuckDB, zero setup, local TPC-H
```

Warehouse pilots: [`docs/SNOWFLAKE.md`](docs/SNOWFLAKE.md) (measured cost), [`docs/DATABRICKS.md`](docs/DATABRICKS.md) (managed connection, live-validated), and [`docs/DUCKDB.md`](docs/DUCKDB.md) (zero-setup, no independent trace verification).

---

## What I got wrong

The most useful part of this repo. Nine bugs and one framing error; **most failed silently, and four moved a headline number in the direction I wanted.**

- **Measured the wrong unit and published a refutation.** Whole queries instead of sub-expressions, different questions instead of the same task. The finding inverted once corrected.
- **`ALTER SESSION SET QUERY_TAG = ?` does not bind in Snowflake.** It set the tag to the literal `"?"` and returned success — an entire agent run produced untraceable queries with no error anywhere.
- **A preflight that tested a different code path than production.** It wrote its own literal tag and reported "✓ round-trips" while every real query was tagged `?`. A check that doesn't exercise the real path is worse than no check.
- **Named the subagent tool `Task` when it is `Agent`.** `--allowedTools` doesn't error on unknown names, so the coordinator condition ran with delegation silently disabled and looked like a valid trace.
- **Regex-"parsed" SQL.** Couldn't read `GROUP BY 1, 2`, `date_trunc()`, casts or aliases, and emitted dimensions literally named `"1"`.
- **Divided by the wrong denominator** — counted unmodellable queries as deduplicated. 51% → 20%.
- **Applied the simulator's 100× time dilation to a real trace.** Reported 2708s and 7 warehouse resumes; truth was 27s and 1.
- **Substring-matched numeric values.** Postgres returns `numeric` as a string, so `"69.33124…"` never matched an answer saying `$69.33`. Grounding under-reported 3/6 when it was 6/6.
- **Line-anchored the citation regex** — models write `**CITED: q1**`, scored as citing nothing.
- **A read-only guard that only checked the start of the string.** `select 1; drop table x` passed it and the DROP executed against a canary table.

## What this is not

- **Small n.** 25 attempts at most, not the paper's 50, and the headline number shifted by 15 points between 8 and 25. One dataset, two models.
- **Sub-expressions are approximated** from the query shape, not decomposed from a real plan. The direction is clear; the exact percentage is not authoritative.
- **The parser models ~1 query in 4–7 of real analytics.** On the Snowflake TPC-H run, 1 of 4 — and the modellable one was a `min/max` date check while the three that answered the question all had 3–4 joins. Joins, CTEs, subqueries, `OR` and `HAVING` are declined rather than mis-parsed.
- **Not production software.** Postgres-oriented, result values written to disk in plaintext, no auth or multi-tenancy. See below.

## Not production software

- **The server is the database, not a wrapper.** One hardcoded client, serialising every agent.
- **Result values are written to `out/*.jsonl` in plaintext** for grounding — an exfiltration surface anywhere real.
- **No authentication, multi-tenancy, quotas or retention.** Trace context is an unauthenticated SQL comment, so an agent can forge its own `intent` — it cannot underpin chargeback.
- **Run it against a SELECT-only database role.** The application-layer guard is a second line, not a boundary.

## Prior art, and what I would use instead

- **[OpenTelemetry database semantic conventions](https://opentelemetry.io/docs/specs/semconv/db/database-spans/) + sqlcommenter** — what `context.py` and `trace.py` are, as a spec. Using it deletes the log parser and the span assembler, since any OTel backend renders the trace.
- **[sqlglot](https://github.com/tobymao/sqlglot)** — now what this repo uses (it moved to Python for exactly this). 30+ dialects, a real AST, and column-level lineage; on the simulated-agent demo run it modelled 89/93 query shapes, up from the ~1-in-4 ceiling the old regex/node-sql-parser approach hit on real analytics SQL.
- **[ADBC](https://arrow.apache.org/adbc/current/index.html) / [Ibis](https://ibis-project.org/)** for connecting many warehouses.

## Why nobody does this yet (Sept 2026 scan)

Checked four adjacent categories for anything already doing this. Nothing was:

- **Agent/LLM observability platforms** (Langfuse, LangSmith, Arize, Braintrust, Datadog LLM Observability) trace the model-call graph — prompts, tool calls, latency, token cost, evals. A SQL tool call is one opaque span to them; nothing looks inside the query text for redundancy against other spans.
- **Warehouse cost tools** (Select.dev, Keebo, Espresso AI) optimize compute sizing and autoscaling for whatever workload happens to run on the warehouse. Query semantics and agent authorship aren't inputs — a redundant query just runs faster on a better-sized warehouse.
- **MCP/LLM gateway semantic caching** (Bifrost and others) matches a query to a restated version of *itself* — exact-hash or embedding similarity, 1:1. None do the N:1 "one rollup answers many genuinely different probes" that `shape.covering_set()` does.
- The closest published relative is [*Semantic Caching for OLAP via LLM-Based Query Canonicalization*](https://arxiv.org/pdf/2602.19811) (2026): an LLM canonicalizes syntactically different but semantically *identical* queries onto one form. The mechanism differs from `shape.subsumes()` in the part that matters — an LLM call on every cache decision (latency, cost, and correctness that's probabilistic) versus a deterministic sqlglot AST parse that only serves a query when the answer already sits in a materialized anchor's own columns, verified against live Postgres before being trusted (see [Phase 2](docs/PHASE2.md)).
- Even without automated tooling, the problem is real enough that [OpenAI's own data-agent team hand-restricted overlapping, redundant tool calls](https://openai.com/index/inside-our-in-house-data-agent/) rather than measuring and fixing the redundancy — which is roughly where the field stood outside this repo, as of this scan.

## Roadmap

Ordered by what the cost-and-correctness position needs. Demand is not yet validated: I have measured agent spend on my own runs, not in anyone's production warehouse.

**Next**
- **Cost per question, end to end.** One trace from the LLM call through the tool call to the warehouse query and its credits, so a question's total cost is LLM tokens plus compute. Emitted as OpenTelemetry, so it lands in stacks companies already run rather than a parallel one. *Done when* one agent question reports token cost and measured Snowflake credits in a single trace.

**Then, if teams want it**
- **Cost controls.** Per-agent and per-team attribution and budgets, with the savings from shared rollups reported in dollars rather than milliseconds.
- **Data-access audit.** Which agent read which tables, on whose behalf, and whether the result was used. The traces already carry the agent and the SQL; this turns them into an audit view.

### Later, only with demand: pipeline observability

**Not built yet** — the trace primitive and the verification discipline both generalize past agents, and this is the sequence if they do. dbt already auto-tags every query it issues; Databricks shipped Query Tags into `system.query.history` (public preview, June 2026); Snowflake `QUERY_TAG` is already wired here — pipeline tooling is emitting exactly the kind of tags `context.py` invents for agents. That turns the first phase into "read tags that already exist," not "build pipeline tracing."

**The wedge, and where I'd stop.** Freshness, volume and distribution monitoring is already owned by Monte Carlo, Bigeye, Elementary, Soda, Datafold — rebuilding it here produces a worse version of a solved thing and gives up the one property that makes this repo credible: small, measured, honest about its limits. The differentiated asset is **observed versus declared**, and agents that structurally cannot assert past their evidence — the same rule `verify_citations.py` already enforces on agent citations. Each phase below exists because it needs that property; phase 6 is deliberately the thinnest slice of monitoring that triage can't proceed without, not a monitoring platform.

Continuing the existing phase numbers (Phase 1 = out-of-path tracing, [Phase 2](docs/PHASE2.md) = materialized covering sets, both built and measured):

3. **Unify producers** — fold dbt/Databricks/Snowflake query tags into the same span model as agent spans, reusing `context.py`/`trace.py` unchanged. *Done when* one warehouse log yields a single run tree with both agent and dbt-model spans, checked against a real dbt run.
4. **Observed lineage, diffed against declared lineage** — `sqlglot.lineage` over the tagged corpus versus dbt's manifest; disagreement is the finding. The honest cost: `shape.py` currently declines joins, CTEs and subqueries by design, and lineage needs exactly those — this is where the parser's scope has to genuinely expand, not just get reused. *Done when* lineage for a hand-checked column sample is exact and the manifest diff is reported, not silently reconciled.
5. **Schema-change impact analysis** — a proposed `DROP`/rename/retype resolves to a deterministic, evidence-backed consumer set from observed lineage. The strongest of these because the answer is checkable against real queries in the log. *Done when* a planted column rename produces the exact affected-consumer set with zero false negatives.
6. **Minimum triage substrate** — per-table last-write time, row counts, job/run state, pulled from metadata that already exists (`ACCOUNT_USAGE`, `information_schema`, `pg_stat`). No alerting, no thresholds, no anomaly models — those are someone else's product. *Done when* one query answers "what changed near time T" across deploys, schema, freshness and failures on a shared axis.
7. **Read-only triage agent** — proposes ranked, evidence-linked hypotheses over an MCP server exposing the substrate read-only; `verify_citations.py`'s mechanism rejects any hypothesis without a supporting span or evidence row. Remediation stays behind human approval. *Done when* every hypothesis on a seeded incident is either evidence-linked or explicitly declined, reported as a ratio.
8. **Deterministic gates for self-service onboarding** — the agent drafts pipeline plans, tests and config; CI and policy checks it doesn't control stay authoritative. *Done when* an agent-drafted config reaches production only via a check it cannot influence, demonstrated against a deliberately bad draft.

## Layout

| Path | What it does |
|---|---|
| `agent_data_observability/same_task.py` | **The replication** — N agents, one task, sub-expression redundancy |
| `agent_data_observability/shape.py` | sqlglot-based query shape, subsumption, candidate synthesis |
| `agent_data_observability/trace.py` | Log parsing, span reconstruction, billing model |
| `agent_data_observability/context.py` | Trace context, sqlcommenter-style |
| `agent_data_observability/mcp_db_server.py` · `agent_data_observability/snowflake_mcp_server.py` | Traced `run_sql` tools |
| `agent_data_observability/databricks_genie_mcp_server.py` | Traced `ask_genie` — verification over a connection we don't own |
| `agent_data_observability/duckdb_mcp_server.py` | Traced `run_sql` over a local, zero-setup TPC-H warehouse |
| `agent_data_observability/real_agent.py` · `agent_data_observability/snowflake_agent.py` · `agent_data_observability/duckdb_agent.py` | Drive real Claude Code agents |
| `agent_data_observability/verify_citations.py` | Value-grounded citation verification |
| `agent_data_observability/cross_session.py` | N agents, different questions |
| `agent_data_observability/materialize.py` · `agent_data_observability/tracedb.py` | **Phase 2** — materialize covering-set rollups into DuckDB, serve queries from them (`TracedClient(mode="intercept")`) |
| `agent_data_observability/phase2_demo.py` | Replays a completed trace through Phase 2 and reports measured before/after |
| `scripts/redset-baseline.sh` | Human/pipeline baseline from Redset |
| `tests/` | Unit tests + an end-to-end pipeline suite (pytest) |

## License

MIT
