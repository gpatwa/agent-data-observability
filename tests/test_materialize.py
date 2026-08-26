"""Phase 2 tests. The pure predicate is tested without a database; the
materialize/serve round-trip needs a live Postgres with the demo schema
(orders/refunds) — the same cluster scripts/demo.sh manages at .pgdata/.
Run `./scripts/demo.sh` once (or just start that cluster) before this file
will do more than skip.
"""

from __future__ import annotations

import pytest

from agent_data_observability.config import PG
from agent_data_observability.shape import extract_shape, covering_set

try:
    import psycopg
    from psycopg.rows import dict_row

    _conn = psycopg.connect(**PG, autocommit=True, row_factory=dict_row, connect_timeout=2)
    _conn.execute("select 1 from orders limit 1")
    _HAVE_PG = True
except Exception:
    _HAVE_PG = False

pg_required = pytest.mark.skipif(not _HAVE_PG, reason="needs a live Postgres with the demo `orders` table at .pgdata/ (see scripts/demo.sh)")


def test_can_serve_from_cache_requires_matching_measures():
    from agent_data_observability.materialize import can_serve_from_cache

    anchor = extract_shape("select order_date, sum(amount), count(order_id) from orders group by order_date")
    query_avg = extract_shape("select avg(amount) from orders where order_date = '2026-07-05'")
    # avg is derivable from sum+count per subsumes(), but not a literal column
    # the anchor already has — this prototype declines rather than derive it.
    assert not can_serve_from_cache(anchor, query_avg)


def test_can_serve_from_cache_declines_a_query_with_its_own_groupby():
    from agent_data_observability.materialize import can_serve_from_cache

    anchor = extract_shape("select region, channel, sum(amount) from orders group by region, channel")
    query = extract_shape("select channel, sum(amount) from orders where region = 'EMEA' group by channel")
    assert not can_serve_from_cache(anchor, query)


def test_can_serve_from_cache_accepts_a_single_cell_lookup_on_a_matching_rollup():
    from agent_data_observability.materialize import can_serve_from_cache

    anchor = extract_shape("select order_date, sum(amount), count(order_id) from orders group by order_date")
    query = extract_shape("select sum(amount), count(order_id) from orders where order_date = '2026-07-05'")
    assert can_serve_from_cache(anchor, query)


@pg_required
def test_a_daily_rollup_correctly_serves_every_probe_it_covers():
    import duckdb
    from agent_data_observability.materialize import MaterializedCache, can_serve_from_cache

    dcon = duckdb.connect(":memory:")
    cache = MaterializedCache(dcon)

    probe_sqls = [f"select count(order_id), sum(amount) from orders where order_date = '2026-07-{d:02d}'" for d in range(1, 6)]
    probe_shapes = [extract_shape(s) for s in probe_sqls]
    cover = covering_set(probe_shapes)
    anchor_shape = cover.anchors[0]["anchor"]

    entry = cache.materialize(_conn, anchor_shape)
    assert entry.rows_materialized > 0

    for sql, shape in zip(probe_sqls, probe_shapes):
        assert can_serve_from_cache(anchor_shape, shape)
        found = cache.find(shape)
        served = cache.serve(found, shape, sql)
        direct = _conn.execute(sql).fetchall()
        assert [float(v) for v in served[0].values()] == [float(v) for v in direct[0].values()]

    dcon.close()


@pg_required
def test_a_two_dimension_rollup_correctly_serves_every_cell_it_covers():
    import duckdb
    from agent_data_observability.materialize import MaterializedCache, can_serve_from_cache

    dcon = duckdb.connect(":memory:")
    cache = MaterializedCache(dcon)

    july = "order_date >= '2026-07-01' and order_date <= '2026-07-31'"
    cell_sqls = [
        f"select sum(amount) from orders where {july} and region = '{r}' and channel = '{c}'"
        for r in ("AMER", "EMEA") for c in ("paid_search", "organic")
    ]
    rollup_sql = f"select region, channel, sum(amount) from orders where {july} group by region, channel"
    all_shapes = [extract_shape(s) for s in cell_sqls + [rollup_sql]]
    cover = covering_set(all_shapes)
    anchor_shape = cover.anchors[0]["anchor"]

    entry = cache.materialize(_conn, anchor_shape)
    assert entry.rows_materialized > 0

    for sql in cell_sqls:
        shape = extract_shape(sql)
        assert can_serve_from_cache(anchor_shape, shape)
        served = cache.serve(cache.find(shape), shape, sql)
        direct = _conn.execute(sql).fetchall()
        assert abs(float(list(served[0].values())[0]) - float(list(direct[0].values())[0])) < 1e-6

    dcon.close()
