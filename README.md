# agent-data-observability

**A harness for measuring what AI agents do to your data warehouse — and a correction to what I first published with it.**

The premise, from [Intelligence is Free, Now What?](https://bair.berkeley.edu/blog/2026/07/07/intelligence-is-free-now-what/) (BAIR, 2026) and the UC Berkeley EPIC Data Lab's [agent-first data systems](https://arxiv.org/abs/2509.00997) paper: agents issue vast numbers of overlapping speculative queries, only 10–20% of sub-plans are distinct, and there is a large prize in sharing that computation.

I built the tracing, ran six conditions, and published that **the claim did not reproduce.**

**That was wrong, and the error was mine.** I measured a different quantity than the one the claim is about. When I finally ran the published experiment — *N agents attempting the **same** task*, redundancy counted over **sub-expressions** — it reproduced at **17.7% distinct**, inside the stated range.

📉 **[Read the findings](https://gpatwa.github.io/agent-data-observability/)**

---

## The correction

The published measurement is specific. From the EPIC Lab paper: the BIRD text-to-SQL benchmark, **50 independent attempts per task**, redundancy defined as *"the proportion of distinct sub-expressions relative to total sub-expressions across multiple agent attempts."*

Everything I ran differed on **both** axes:

| | The published claim | What I measured first |
|---|---|---|
| Setup | N agents, **same** task | agents on **different** questions |
| Unit | **sub-expressions** in the plan | **whole queries** |
| Verdict | 10–20% distinct | "42.9%, doesn't reproduce" |

So I refuted a neighbouring claim and reported it as the claim. Running it properly, with 8 agents on one question:

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

**Both readings are true simultaneously**, and that is the actual finding. Eight agents asked the same question 55 different ways — but underneath, they scanned the same table with the same 13 predicates computing the same 10 measures. The redundancy is entirely *below* the level of the query.

Reproduce: `adobs-same-task --attempts 8`. Saved output: [`docs/runs/same-task-report.txt`](docs/runs/same-task-report.txt).

## What that implies

**Result caching cannot capture this.** At 92.7% distinct whole queries, a cache keyed on the query — which is what Redshift, Snowflake and every LLM gateway ship — hits almost nothing. The prize needs **multi-query optimization, shared scans and partial-result reuse**, which is exactly what the paper proposes and what I spent six conditions arguing wasn't needed.

It also explains the human/pipeline baseline below rather than contradicting it. Those workloads repeat *whole queries*; agents repeat *fragments*. They need different machinery.

**That machinery is now built and measured, not just proposed.** [`docs/PHASE2.md`](docs/PHASE2.md) materializes the covering-set rollups this repo's reports have always recommended into a local DuckDB cache, then answers the queries they cover by direct row selection — no re-derivation, so a wrong answer isn't possible by construction, only a declined one. Replayed against the simulated-agent demo trace: 44 of 89 aggregate queries answered from 9 materialized rollups, all 44 verified byte-for-byte against a fresh Postgres execution, 44 real warehouse round-trips avoided, ~30x measured (not modelled) latency.

## Findings that still stand

These were measured correctly and are unaffected — they are about different questions, not the same task:

- **Agents on different questions share little.** 8 concurrent sessions on overlapping-but-distinct questions: 13% cross-session redundancy at whole-query level. A delegating coordinator: 9.1%.
- **Agents waste little.** 34 of 41 results across eight sessions reached the answer, verified by value-grounding rather than self-report. 25/25 for the coordinator.
- **Weaker models do not fan out.** Haiku 4.5 issued 5 queries to Opus 5's 6 on an identical question.
- **The idle-tax finding was an artifact of n=1.** 96.7% of a lone agent's bill is idle warehouse time; with 8 concurrent agents sharing a warehouse it falls to **23.7%**.
- **Human/pipeline traffic repeats whole queries heavily.** [Redset](https://github.com/amazon-science/redset) — 18.9M production Redshift SELECTs across 20 clusters — scored with this repo's metric: **91.3% median** redundancy, 70% of clusters above 80%. (CC BY-NC 4.0, attributed to Amazon.)

## Measured cost, at last

Every dollar figure this project published was **modelled** — Snowflake billing rules applied to Postgres execution times. The Snowflake pilot replaces that with Snowflake's own `QUERY_ATTRIBUTION_HISTORY`:

| | Per resolved task |
|---|---|
| **Measured** (4 attributed queries, 0.029907 credits @ $3) | **$0.0897** |
| Modelled (published figure) | $0.073 |
| Ratio | **1.23×** |

The modelled number was **low by 23%** — the right order of magnitude, wrong in the direction of understating cost. Good enough that the ratios in this repo stand; not good enough to quote as a dollar figure without this caveat.

Reproduce: `adobs-snowflake-cost --hours 72`. Saved: [`docs/runs/snowflake-measured-cost.txt`](docs/runs/snowflake-measured-cost.txt).

Only 4 of 7 tagged queries had credits attributed — Snowflake attributes compute to queries that consumed meaningful warehouse time, so cheap metadata lookups contribute nothing. n=1 trace, TPC-H on an XS warehouse.

## What survived as a tool

The **trace primitive**. Query lineage, per-agent cost attribution, and verified answer-grounding all reconstruct from a log the warehouse already writes, with nothing in the data path. On Snowflake it is simpler still — trace context rides in the native `QUERY_TAG`, so there is no log parsing at all.

---

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

- **Small n.** 8 attempts, not the paper's 50. One dataset, two models.
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
- Warehouse cost tools (Select.dev, Keebo, Espresso AI) optimize warehouses, not query semantics; MCP gateways (Snowflake Cortex AI Gateway, MintMCP) govern access, not economics.

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
