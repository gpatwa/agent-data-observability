# agent-data-observability

**A harness for measuring what AI agents actually do to your data warehouse — and the negative result it produced.**

The premise, from [Intelligence is Free, Now What?](https://bair.berkeley.edu/blog/2026/07/07/intelligence-is-free-now-what/) (BAIR, 2026) and the UC Berkeley EPIC Data Lab's [agent-first data systems](https://arxiv.org/pdf/2509.00997) work: agents issue vast numbers of near-duplicate speculative queries, only 10–20% of their sub-plans are distinct, and there is a large optimization prize in deduplicating that traffic.

I built the trace primitive needed to measure it, then measured it against real agents.

**The prize did not show up.** This repo is the harness, the evidence, and an honest account of what failed.

---

## Headline

| | Simulated naive agent | Real agent (Opus 5) | Real agent (Haiku 4.5) | 8 concurrent real agents |
|---|---|---|---|---|
| Queries for one question | 93 | 6 | 5 | 41 total |
| Distinct sub-plans | 9.7% | 57% | 60% | — |
| Results that reached the answer | 4 / 93 | **6 / 6** | 4 / 5 | **34 / 41** |
| Idle share of warehouse bill | ~99% | 96.7% | — | **23.7%** |
| Cost per task | $0.36–0.46 | $0.073 | $0.073 | $0.116 |

**Cross-session redundancy: 13.0%.** Sharing rollups across eight agents' worth of overlapping questions eliminated 3 of 23 per-session anchors. Best-case materialization was 20 anchors serving 25 queries — a **20% reduction**, not the ~90% the simulation implied.

---

## The four claims, and how each died

**1. Agents waste most of their queries on speculation that never reaches the answer.** *Refuted.*
The simulated agent used 4 of 93 results. Real agents used **34 of 41** across eight sessions, and this is verified rather than self-reported — see "Verifying citations" below. One session out of eight (`refunds`, 3/9 grounded) showed real waste; it is the exception, not the pattern.

**2. Cross-session sharing is where the redundancy really lives.** *Too small to build on.*
This was the strongest surviving reframe after the single-agent result: no agent need be wasteful, only for different people to ask related questions of the same tables. Eight agents asking deliberately overlapping questions about the same two tables and date range yielded **13%**. Materialization still helps — 20 anchors serve 25 queries — but that is a normal data-warehouse optimization, not an agent-specific one.

**3. Weaker models will fan out, and cheap intelligence means weak models at scale.** *Refuted.*
This was the strongest theoretical argument, and directly what "intelligence is free" implies. Haiku 4.5 on the identical question issued **5 queries to Opus's 6**, with the same tight investigative structure. No fan-out.

**4. You are paying a warehouse to watch an LLM think.** *An artifact of measuring one agent.*
True for a lone agent on a dedicated warehouse: 96.7% of the bill was idle. But with **8 concurrent agents sharing one warehouse, idle fell to 23.7%** — 851 of 1116 billed seconds were productive. Agents' think-time gaps interleave. The finding was real and the conclusion was wrong.

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
node src/cross-session.mjs --concurrency 4     # 8 agents, ~$2 of LLM spend
```

The demo creates a throwaway cluster on port 55432; it does not touch an existing Postgres instance. Raw output from every run described above is in [`docs/runs/`](docs/runs/).

---

## What I got wrong along the way

These are in the repo history, and they are the most useful part of it.

- **Applied the simulator's 100× think-time dilation to a real trace.** Reported a 2708-second task with 7 warehouse resumes; the truth was 27 seconds and 1 resume. Dilation is now an explicit CLI argument defaulting to 1.
- **Substring-matched numeric values.** Postgres returns `numeric` as a string, so result values were compared as text against prose. `"69.33124..."` never appears in an answer that says `$69.33`, so grounding was under-reported as 3/6 when it was really 6/6.
- **Regex-"parsed" SQL.** The original extractor could not read `GROUP BY 1, 2`, `date_trunc()`, casts, or aliases — exactly what real agents write — and emitted dimensions literally named `"1"` and `"2"`. Every covering-set number computed on real SQL before [`src/shape.mjs`](src/shape.mjs) was noise.
- **Divided by the wrong denominator.** Reported "51% dedup" by counting queries the parser *couldn't model* as deduplicated. Against queries the anchors can actually serve, it is 20%.
- **Line-anchored the citation regex.** Models write `**CITED: q1, q4**`; a `/^CITED:/` scored that run as citing nothing.

Every one of these moved a headline number, and three of them moved it in the flattering direction.

---

## What this is not

- **Small n.** One question shape, one dataset, two models, eight concurrent sessions. Not a study.
- **The dataset has a single planted cause.** Real investigations are messier and may fan out more.
- **The schema is two tables.** Tool-surface confusion on a 200-table warehouse is where agents plausibly flail, and this does not test it.
- **No multi-agent coordinator.** A coordinator spawning parallel subagents is the redundancy thesis's best remaining home and is untested here.
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
| `test/` | 31 tests, weighted toward negative subsumption cases |

## If you want to revive the thesis

The two conditions this did not test, in order of promise: a **multi-agent coordinator** spawning parallel subagents over one warehouse, and a **wide schema** (100+ tables) where the agent must search for the right table before it can query it. Both are a config change away in this harness. If redundancy is anywhere, it is there.

## License

MIT
