# Phase 2: materialize and serve, actually built

Every report this project has produced ends with the same recommendation,
printed and never run:

> Materialize N rollups to serve M of T aggregate queries.

`tracedb.py`'s `TracedClient` has carried the seam for this since Phase 1 —
`mode='intercept'`, documented as "Phase 2, not built." This is that filled
in: covering-set anchors get materialized into a local DuckDB cache, and
queries they cover are answered from it instead of the warehouse — through
the same `TracedClient.run()` an agent's tool calls already go through.

This is **not** the query-result caching the README's findings say cannot
work here (92.7% distinct whole queries — a cache keyed on exact query text
hits almost nothing). It is the "multi-query optimization, shared scans and
partial-result reuse" those findings say is actually needed: one materialized
rollup answers many *different* queries via subsumption, not one query
answering itself.

## What it actually does, and the line it deliberately does not cross

`shape.subsumes()` can tell you an anchor *could* answer a query in the
general case — including ones that need real arithmetic on replay: `avg`
derived from `sum`+`count`, a query re-summed across several anchor rows, a
bucketed dimension recomputed from a finer one. Serving a wrong answer from
cache is worse than not caching, so `materialize.py`'s `can_serve_from_cache`
is strictly narrower than `subsumes()`: it only serves a query when the
answer is already sitting in the anchor's materialized columns, retrievable
by direct row selection — no arithmetic, no re-grouping.

Concretely, a query is cache-servable only when:

- every measure it needs is a **literal** column the anchor already computed
  (rules out `avg(amount)` being served from an anchor that has `sum(amount)`
  and `count(*)` — those are what it's derived *from*, not the same column)
- the query has no `GROUP BY` of its own — it's a single-cell lookup
  (`sum(amount) where region='EMEA' and channel='paid_search'`, not
  `sum(amount) group by channel`)
- every dimension the anchor grouped by is pinned to a literal by an equality
  filter in the query, so exactly the matching row can be selected

Anything else — including cases `subsumes()` would approve — falls through
to the warehouse, identically to Phase 1. That is the same "decline rather
than mis-parse" rule `shape.py` applies to unmodellable SQL, extended here to
"decline rather than mis-serve."

## Run it

```bash
./scripts/demo.sh                # produces a trace with real redundancy
adobs-phase2-demo                # materialize its anchors, replay it, measure
```

`adobs-phase2-demo` takes an optional `<logPath> <eventsPath>`; it defaults
to the saved simulated-agent demo trace. For every query it counts as
cache-servable, it also re-executes the same query directly against Postgres
and checks the two answers match before counting it as verified — nothing is
reported as correct on the strength of the caching logic alone.

Measured against the simulated-agent demo trace (93 queries, 89 modellable):

```
answerable from the cache          44  (no re-aggregation needed)
declined, still hits the warehouse 45  (needs re-aggregation, has its own
                                        GROUP BY, or is an anchor itself)
verified correct vs live Postgres  44/44
Postgres executions to materialize 9    (~1s total)
Postgres executions AVOIDED        44
measured latency, direct Postgres  ~1055ms total  (~24ms/query avg)
measured latency, served from cache ~35ms total   (~0.8ms/query avg)
speedup                            ~30x
```

## Using it directly

`pg_conn` must be a psycopg connection using `row_factory=dict_row` —
`MaterializedCache.materialize()` reads rows by column name.

```python
from agent_data_observability.shape import covering_set
from agent_data_observability.materialize import MaterializedCache
from agent_data_observability.tracedb import TracedClient
import duckdb

cache = MaterializedCache(duckdb.connect(":memory:"))
cover = covering_set(shapes)          # from a completed trace, or precomputed
for a in cover.anchors:
    cache.materialize(pg_conn, a["anchor"])   # one Postgres execution per anchor

client = TracedClient(pg_conn, mode="intercept", cache=cache)
rows = client.run(span, sql)          # served from cache when possible, else the warehouse
```

`client.agent_events[-1]["served_by"]` is `"cache"` or `"warehouse"` for
every call, so a live agent run can report its own hit rate the same way
`phase2_demo.py` does for a replayed one.

## What this is not

- **Not online.** Anchors are decided from a completed trace's covering set
  (or precomputed from history), not chosen live as queries arrive. Deciding
  *when* a pattern is worth materializing mid-run is a harder, genuinely
  different problem — out of scope for this prototype.
- **Not a general query optimizer.** The replayable subset above is narrow on
  purpose. It happens to cover the two redundancy patterns this repo's own
  demo actually produces (a per-day fan-out, a per-cell fan-out) — that is
  not a coincidence, but it is not proof the same coverage holds elsewhere.
- **No invalidation.** The cache is built once per process from a point-in-
  time query and never refreshed. A production version needs a staleness
  policy this prototype does not have.
- **DuckDB here is purely a local cache**, unrelated to the DuckDB warehouse
  pilot in [`DUCKDB.md`](DUCKDB.md) — same library, different job.
