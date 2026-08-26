# DuckDB pilot

The zero-setup condition: no account, no credentials, no network access. The
warehouse is a local TPC-H SF1 database, generated on first use by DuckDB's
built-in `tpch` extension — same schema and scale as Snowflake's
`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`, so results are comparable across the two
pilots without either requiring the other.

---

## Why this adapter is different, and what it can't prove

Postgres and Snowflake both let trace context be recovered from a log the
*warehouse* writes independently of this harness's own bookkeeping — a
separate backend process (Postgres), or a managed service's own history views
(Snowflake `ACCOUNT_USAGE`). That independence is what makes "nothing in the
data path" a real claim about those two conditions, not just a description of
the client code.

DuckDB is embedded: the "warehouse" is a library loaded into the same process
that runs the query. It does have its own query-log facility
(`SET enable_logging = true`, queryable via `duckdb_logs()`), and a trace
context riding in a SQL comment does survive into it — but that log is scoped
to the live connection and does **not** survive the connection closing, even
against a persistent on-disk database. Verified directly: open a file-based
DuckDB, log a tagged query, close the connection, reopen the same file from a
fresh connection — `duckdb_logs()` comes back empty.

So there is no cross-process recovery step here the way `trace.reconstruct()`
gives Postgres, or `ACCOUNT_USAGE` gives Snowflake. What this condition
actually gets is the same trust model as the Databricks Genie adapter: spans
recorded directly by `duckdb_mcp_server.py` at call time, not independently
verified against a warehouse-owned record after the fact. `duckdb_check.py`
still round-trips a tagged query through `duckdb_logs()` — but only within
one live connection, as a sanity check that tagging and execution work, not
as a recovery mechanism.

| | Postgres | Snowflake | Databricks Genie | **DuckDB** |
|---|---|---|---|---|
| Who writes the SQL | the agent | the agent | Genie | the agent |
| Trace injection | SQL comment | `QUERY_TAG` | none possible | SQL comment |
| Recovered from | independent server log | `ACCOUNT_USAGE` | the API response | **agent-side events only** |
| Cost/credit attribution | modelled | measured | not available | **not applicable — no billing model** |
| Setup required | local cluster | account + credentials | workspace + credentials | **none** |

---

## Setup

Nothing to configure. The first run creates `.duckdb/warehouse.duckdb`
(gitignored) and generates TPC-H SF1 into it — a few seconds, once.

Optional: set `DUCKDB_PATH` to use a different file (or `:memory:` for a
throwaway database that regenerates TPC-H on every run).

## Run it

```bash
adobs-duckdb-check                              # preflight — schema + tag round-trip
adobs-duckdb-agent "Which nation generates the most revenue?"
```

## What this does not do

- No cost model. DuckDB is not billed per query or per warehouse-second, so
  there is nothing analogous to the Snowflake pilot's credit attribution —
  this condition is for redundancy/shape analysis only.
- No independent trace verification — see above. Treat this condition's
  `used_downstream`/`grounded` fields with the same caution as the Databricks
  Genie adapter's: they come from the same process that also decided what to
  record, not from a third-party record of what actually ran.
- Result values are still written to `out/*.jsonl` in plaintext for the
  answer-grounding check, same caveat as every other adapter — see the "Not
  production software" section of the README.

Not to be confused with [`PHASE2.md`](PHASE2.md), which also uses DuckDB but
for a different job — a local materialized-rollup cache in front of the
Postgres condition, not a warehouse pilot in its own right.
