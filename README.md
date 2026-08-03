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

Reproduce: `node src/same-task.mjs --attempts 8`. Saved output: [`docs/runs/same-task-report.txt`](docs/runs/same-task-report.txt).

## What that implies

**Result caching cannot capture this.** At 92.7% distinct whole queries, a cache keyed on the query — which is what Redshift, Snowflake and every LLM gateway ship — hits almost nothing. The prize needs **multi-query optimization, shared scans and partial-result reuse**, which is exactly what the paper proposes and what I spent six conditions arguing wasn't needed.

It also explains the human/pipeline baseline below rather than contradicting it. Those workloads repeat *whole queries*; agents repeat *fragments*. They need different machinery.

## Findings that still stand

These were measured correctly and are unaffected — they are about different questions, not the same task:

- **Agents on different questions share little.** 8 concurrent sessions on overlapping-but-distinct questions: 13% cross-session redundancy at whole-query level. A delegating coordinator: 9.1%.
- **Agents waste little.** 34 of 41 results across eight sessions reached the answer, verified by value-grounding rather than self-report. 25/25 for the coordinator.
- **Weaker models do not fan out.** Haiku 4.5 issued 5 queries to Opus 5's 6 on an identical question.
- **The idle-tax finding was an artifact of n=1.** 96.7% of a lone agent's bill is idle warehouse time; with 8 concurrent agents sharing a warehouse it falls to **23.7%**.
- **Human/pipeline traffic repeats whole queries heavily.** [Redset](https://github.com/amazon-science/redset) — 18.9M production Redshift SELECTs across 20 clusters — scored with this repo's metric: **91.3% median** redundancy, 70% of clusters above 80%. (CC BY-NC 4.0, attributed to Amazon.)

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

Node 20+, local PostgreSQL, and an authenticated `claude` CLI.

```bash
npm install
npm test                                        # 37 unit tests, no database
npm run test:e2e                                # log -> trace pipeline, real Postgres
./scripts/demo.sh                               # simulated agent, 4.4M rows

node src/same-task.mjs --attempts 8             # THE REPLICATION
node src/real-agent.mjs "..."                   # one agent, one question
node src/real-agent.mjs "..." --subagents       # delegating coordinator
node src/real-agent.mjs "..." --wide            # 120-table schema, hidden
node src/cross-session.mjs --concurrency 4      # 8 agents, different questions
npm run baseline                                # Redset human/pipeline baseline
```

Snowflake pilot (measured rather than modelled cost): [`docs/SNOWFLAKE.md`](docs/SNOWFLAKE.md).

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

- **[OpenTelemetry database semantic conventions](https://opentelemetry.io/docs/specs/semconv/db/database-spans/) + sqlcommenter** — what `context.mjs` and `trace.mjs` are, as a spec. Using it deletes the log parser and the span assembler, since any OTel backend renders the trace.
- **[sqlglot](https://github.com/tobymao/sqlglot)** instead of node-sql-parser — 30+ dialects, an optimizer, and column-level lineage. This is what would lift the join ceiling above.
- **[ADBC](https://arrow.apache.org/adbc/current/index.html) / [Ibis](https://ibis-project.org/)** for connecting many warehouses.
- Warehouse cost tools (Select.dev, Keebo, Espresso AI) optimize warehouses, not query semantics; MCP gateways (Snowflake Cortex AI Gateway, MintMCP) govern access, not economics.

## Layout

| Path | What it does |
|---|---|
| `src/same-task.mjs` | **The replication** — N agents, one task, sub-expression redundancy |
| `src/shape.mjs` | AST query shape, subsumption, candidate synthesis |
| `src/trace.mjs` | Log parsing, span reconstruction, billing model |
| `src/context.mjs` | Trace context, sqlcommenter-style |
| `src/mcp-db-server.mjs` · `src/snowflake-mcp-server.mjs` | Traced `run_sql` tools |
| `src/real-agent.mjs` · `src/snowflake-agent.mjs` | Drive real Claude Code agents |
| `src/verify-citations.mjs` | Value-grounded citation verification |
| `src/cross-session.mjs` | N agents, different questions |
| `scripts/redset-baseline.sh` | Human/pipeline baseline from Redset |
| `test/` | 37 unit tests + an end-to-end pipeline suite |

## License

MIT
