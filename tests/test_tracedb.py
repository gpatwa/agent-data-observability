"""TracedClient's intercept mode — the Phase 2 wiring. Needs the same live
Postgres demo cluster as test_materialize.py.
"""

from __future__ import annotations

import pytest

from agent_data_observability.config import PG
from agent_data_observability.context import new_span, new_trace
from agent_data_observability.shape import covering_set, extract_shape
from agent_data_observability.tracedb import TracedClient

try:
    import psycopg
    from psycopg.rows import dict_row

    _conn = psycopg.connect(**PG, autocommit=True, row_factory=dict_row, connect_timeout=2)
    _conn.execute("select 1 from orders limit 1")
    _HAVE_PG = True
except Exception:
    _HAVE_PG = False

pg_required = pytest.mark.skipif(not _HAVE_PG, reason="needs a live Postgres with the demo `orders` table at .pgdata/ (see scripts/demo.sh)")


def _span():
    trace = new_trace(agent_id="test", model="test", task_intent="test")
    return new_span(trace, "test", "probe")


@pg_required
def test_intercept_mode_serves_from_cache_when_an_anchor_covers_the_query():
    import duckdb
    from agent_data_observability.materialize import MaterializedCache

    dcon = duckdb.connect(":memory:")
    cache = MaterializedCache(dcon)
    probe_sqls = [f"select count(order_id), sum(amount) from orders where order_date = '2026-07-{d:02d}'" for d in range(1, 4)]
    shapes = [extract_shape(s) for s in probe_sqls]
    anchor_shape = covering_set(shapes).anchors[0]["anchor"]
    cache.materialize(_conn, anchor_shape)

    client = TracedClient(_conn, mode="intercept", cache=cache)
    sql = probe_sqls[0]
    rows = client.run(_span(), sql)

    event = client.agent_events[-1]
    assert event["served_by"] == "cache"

    direct = _conn.execute(sql).fetchall()
    assert [float(v) for v in rows[0].values()] == [float(v) for v in direct[0].values()]
    dcon.close()


@pg_required
def test_intercept_mode_falls_through_to_the_warehouse_when_nothing_covers_the_query():
    import duckdb
    from agent_data_observability.materialize import MaterializedCache

    dcon = duckdb.connect(":memory:")
    cache = MaterializedCache(dcon)  # empty — nothing materialized

    client = TracedClient(_conn, mode="intercept", cache=cache)
    rows = client.run(_span(), "select sum(amount) from orders where order_date = '2026-07-01'")

    event = client.agent_events[-1]
    assert event["served_by"] == "warehouse"
    assert rows
    dcon.close()


@pg_required
def test_observe_mode_never_touches_the_cache_even_when_one_is_supplied():
    import duckdb
    from agent_data_observability.materialize import MaterializedCache

    dcon = duckdb.connect(":memory:")
    cache = MaterializedCache(dcon)
    probe_sqls = [f"select count(order_id), sum(amount) from orders where order_date = '2026-07-{d:02d}'" for d in range(1, 4)]
    shapes = [extract_shape(s) for s in probe_sqls]
    anchor_shape = covering_set(shapes).anchors[0]["anchor"]
    cache.materialize(_conn, anchor_shape)

    client = TracedClient(_conn, mode="observe", cache=cache)
    client.run(_span(), probe_sqls[0])

    event = client.agent_events[-1]
    assert event["served_by"] == "warehouse"
    dcon.close()
