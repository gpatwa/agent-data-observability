"""Phase 2, actually built: materialize covering-set anchors into a local
DuckDB cache and serve the queries they subsume from it instead of the
warehouse.

Every report this project has produced ends with the same unexercised
recommendation — "materialize N rollups to serve M queries" — printed by
assemble.py, never run. `tracedb.py`'s `TracedClient` has carried an
`mode='intercept'` stub since Phase 1 that raises `NotImplementedError`
("Phase 2 — deliberately not implemented"). This module is what fills it in,
and it deliberately covers less ground than `shape.subsumes()` does.

WHY NARROWER THAN subsumes(): `subsumes()` answers "could this anchor, in
principle, serve this query" — including cases that need real re-aggregation
on replay (avg derived from sum+count, a coarser query re-summed across
several anchor rows, a bucketing-function dimension recomputed from a finer
one). Serving a cached answer INCORRECTLY is worse than not caching it, so
this module only serves a query when the anchor's already-materialized
columns answer it by direct row selection — no arithmetic, no re-grouping:

    - every measure the query needs is a LITERAL column the anchor already
      computed (rules out avg-from-sum+count: "avg(amount)" is never a
      literal member of an anchor's measures even when derivable)
    - the query asks for no further GROUP BY of its own (a single-cell
      lookup — "sum(amount) where region='EMEA' and channel='paid_search'",
      not "sum(amount) group by channel")
    - every dimension the anchor grouped by is pinned to a literal by an
      equality filter in the query, so exactly the matching anchor row(s)
      can be selected

That is precisely the dominant redundancy pattern this repo's own demo
already shows: N per-day or per-cell probes, one rollup. Anything the anchor
could only answer via re-aggregation is declined here and falls through to
the warehouse, the same "decline rather than mis-parse" rule shape.py
applies to unmodellable SQL — extended to "decline rather than mis-serve."
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import sqlglot
from sqlglot import exp

from .shape import QueryShape, _split_conjuncts, subsumes

DIALECT = "postgres"


@dataclass
class CacheEntry:
    anchor: QueryShape
    table: str
    dim_cols: dict  # groupby dimension expr -> duckdb column name ("d0", "d1", ...)
    measure_cols: dict  # measure expr -> duckdb column name ("m0", "m1", ...)
    rows_materialized: int = 0


def can_serve_from_cache(anchor: QueryShape, query: QueryShape) -> bool:
    """Stricter than subsumes(): true only when the query is answerable by
    selecting the anchor's own already-computed columns, no re-aggregation."""
    if not subsumes(anchor, query):
        return False
    if query.groupby:
        return False  # would need a further GROUP BY over anchor rows
    if not set(query.measures) <= set(anchor.measures):
        return False  # would need derivation (e.g. avg from sum+count)
    if not anchor.groupby:
        return False  # nothing to select a specific row by
    if not set(anchor.groupby) <= set(query.eq_cols):
        return False  # would need re-summing across several anchor rows
    return True


def _duckdb_type(value) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, (float, Decimal)):
        return "DOUBLE"
    if isinstance(value, datetime.datetime):
        return "TIMESTAMP"
    if isinstance(value, datetime.date):
        return "DATE"
    return "VARCHAR"


def _materialize_rows(con, table: str, cols: list[str], rows: list[dict]) -> None:
    types = []
    for c in cols:
        t = "VARCHAR"
        for r in rows:
            if r.get(c) is not None:
                t = _duckdb_type(r[c])
                break
        types.append(t)
    col_defs = ", ".join(f'"{c}" {t}' for c, t in zip(cols, types))
    con.execute(f'CREATE OR REPLACE TABLE "{table}" ({col_defs})')
    if rows:
        placeholders = ", ".join(["?"] * len(cols))
        con.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})',
                         [tuple(r.get(c) for c in cols) for r in rows])


def _extract_eq_literals(sql: str, dims: set) -> dict:
    """Parse the query's own WHERE clause and pull out the literal value of
    each top-level equality filter on one of `dims`, keyed by dimension
    expression text (matching QueryShape's canonical form)."""
    from .shape import _canon

    tree = sqlglot.parse_one(sql, dialect=DIALECT)
    where = tree.args.get("where")
    if where is None:
        return {}
    conjuncts = _split_conjuncts(where.this) or []
    out = {}
    for c in conjuncts:
        if not isinstance(c, exp.EQ):
            continue
        left, right = c.left, c.right
        if isinstance(left, exp.Column):
            key = _canon(left)
            if key in dims:
                out[key] = right.sql(dialect="duckdb")
    return out


class MaterializedCache:
    """Wraps a DuckDB connection used purely as a local result cache — not a
    warehouse pilot, and not subject to any billing model."""

    def __init__(self, duckdb_conn):
        self.con = duckdb_conn
        self.entries: list[CacheEntry] = []

    def materialize(self, pg_conn, anchor: QueryShape) -> CacheEntry:
        dim_cols = {dim: f"d{i}" for i, dim in enumerate(anchor.groupby)}
        measure_cols = {m: f"m{i}" for i, m in enumerate(anchor.measures)}
        select_parts = [f'{dim} as "{col}"' for dim, col in dim_cols.items()]
        select_parts += [f'{m} as "{col}"' for m, col in measure_cols.items()]
        where = f" where {' and '.join(anchor.filters)}" if anchor.filters else ""
        group = f" group by {', '.join(anchor.groupby)}" if anchor.groupby else ""
        sql = f"select {', '.join(select_parts)} from {anchor.table}{where}{group}"

        cur = pg_conn.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()

        table = f"anchor_{len(self.entries)}"
        _materialize_rows(self.con, table, cols, rows)

        entry = CacheEntry(anchor=anchor, table=table, dim_cols=dim_cols,
                            measure_cols=measure_cols, rows_materialized=len(rows))
        self.entries.append(entry)
        return entry

    def find(self, query: QueryShape) -> Optional[CacheEntry]:
        for e in self.entries:
            if can_serve_from_cache(e.anchor, query):
                return e
        return None

    def serve(self, entry: CacheEntry, query: QueryShape, sql: str) -> list[dict]:
        """Answer `query` (whose original text was `sql`, needed only to pull
        out literal filter values) from the materialized anchor. Returns rows
        keyed by the query's own measure expressions, so callers can't tell
        the difference from a direct warehouse result."""
        literals = _extract_eq_literals(sql, set(entry.dim_cols.keys()))
        conditions = []
        for dim, col in entry.dim_cols.items():
            if dim not in literals:
                raise ValueError(f"can_serve_from_cache approved a query missing a literal for {dim!r}")
            conditions.append(f'"{col}" = {literals[dim]}')
        where = " where " + " and ".join(conditions) if conditions else ""

        select_parts = [f'"{entry.measure_cols[m]}" as "{m}"' for m in query.measures]
        cache_sql = f'select {", ".join(select_parts)} from "{entry.table}"{where}'
        cur = self.con.execute(cache_sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
