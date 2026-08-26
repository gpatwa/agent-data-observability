"""DuckDB connection + TPC-H fixture, for a warehouse pilot that needs no
account, no credentials, and no network access.

WHY THIS ADAPTER IS DIFFERENT FROM POSTGRES AND SNOWFLAKE, AND WHY THAT MATTERS:

Postgres and Snowflake both let trace context be recovered from a log the
*warehouse* writes independently of our own bookkeeping — a separate backend
process (Postgres) or a managed service's own history views (Snowflake). That
independence is what makes "nothing in the data path" a real claim rather
than a description of our own client code.

DuckDB is embedded: the "warehouse" is a library loaded into the same process
that runs the query. It does have its own query-log facility
(`SET enable_logging=true` / `duckdb_logs()`), and trace context riding in a
SQL comment survives into it — but that log is scoped to the live connection
and does NOT survive the connection closing, even against a persistent
on-disk database (verified empirically: a fresh connection to the same file
sees zero rows). So there is no cross-process recovery step here the way
`trace.reconstruct()` gives Postgres, or `ACCOUNT_USAGE` gives Snowflake.

What this condition actually gets, honestly: the same trust model as the
Databricks Genie adapter — spans recorded directly by our own code at call
time, not independently verified against a warehouse-owned record. The
preflight (duckdb_check.py) still round-trips a tagged query through
`duckdb_logs()`, but only within one live connection, as a sanity check that
tagging works — not as a recovery mechanism.

Default dataset is TPC-H SF1, generated on first use via DuckDB's built-in
`tpch` extension — same schema and scale as Snowflake's
SNOWFLAKE_SAMPLE_DATA.TPCH_SF1, so results are comparable across the two
pilots, and no download or account is required to get it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".duckdb" / "warehouse.duckdb"


def db_path() -> str:
    p = os.environ.get("DUCKDB_PATH")
    if p:
        return p
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return str(DEFAULT_PATH)


def connect(path: str | None = None):
    import duckdb

    conn = duckdb.connect(path or db_path())
    conn.execute("SET enable_logging = true")
    conn.execute("SET logging_storage = 'memory'")

    conn.execute("INSTALL tpch")
    conn.execute("LOAD tpch")
    existing = {r[0] for r in conn.execute("select table_name from information_schema.tables").fetchall()}
    if "lineitem" not in existing:
        conn.execute("CALL dbgen(sf=1)")
    return conn


def execute(conn, sql_text: str) -> dict:
    cur = conn.execute(sql_text)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"rows": rows}


def read_agenttrace_log(conn) -> list[dict]:
    """Query rows logged so far in the CURRENT connection whose text carries
    trace context. Only meaningful for the life of this connection — see the
    module docstring. Used by the preflight round-trip check."""
    rows = conn.execute(
        "select context_id, timestamp, message from duckdb_logs() "
        "where type = 'QueryLog' and message like '%agenttrace%' order by timestamp"
    ).fetchall()
    return [{"context_id": r[0], "timestamp": r[1], "message": r[2]} for r in rows]
