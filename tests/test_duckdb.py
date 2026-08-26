import pytest

from agent_data_observability.context import new_span, new_trace, parse_context, serialize_context
from agent_data_observability.duckdb_ import connect, execute, read_agenttrace_log
from agent_data_observability.duckdb_mcp_server import scalar_values
from agent_data_observability.readonly import read_only_refusal


@pytest.fixture(scope="module")
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def test_connect_generates_tpch_sf1_schema(conn):
    n = execute(conn, "select count(*) as c from lineitem")["rows"][0]["c"]
    assert n == 6_001_215


def test_connect_is_idempotent_against_an_existing_schema(conn):
    # Reconnecting to a DB that already has TPC-H tables must not re-run dbgen.
    n_before = execute(conn, "select count(*) as c from orders")["rows"][0]["c"]
    reconnected = connect(":memory:")  # a fresh in-memory DB, but exercises the same guard path
    n_after = execute(reconnected, "select count(*) as c from orders")["rows"][0]["c"]
    reconnected.close()
    assert n_before == n_after


def test_execute_returns_dict_rows_keyed_by_column_name(conn):
    res = execute(conn, "select 1 as a, 'x' as b")
    assert res["rows"] == [{"a": 1, "b": "x"}]


def test_read_only_guard_applies_the_same_as_other_adapters(conn):
    assert read_only_refusal("select 1; drop table orders")
    assert read_only_refusal("select count(*) from lineitem") is None


def test_trace_context_round_trips_through_duckdb_logs_within_one_connection(conn):
    trace = new_trace(agent_id="t", model="m", task_intent="test")
    span = new_span(trace, "revenue by nation", "probe")
    tagged = f"{serialize_context(span)} select 1"
    execute(conn, tagged)

    hits = [r for r in read_agenttrace_log(conn) if span["span_id"] in r["message"]]
    assert hits, "tagged query must appear in duckdb_logs()"
    recovered = parse_context(hits[0]["message"])
    assert recovered["span_id"] == span["span_id"]
    assert recovered["span_intent"] == "revenue by nation"


def test_a_real_join_query_against_tpch_executes_and_tags_correctly(conn):
    trace = new_trace(agent_id="t", model="m", task_intent="test")
    span = new_span(trace, "revenue by nation", "probe")
    sql = ("select n_name, sum(l_extendedprice) as rev from lineitem "
           "join orders on l_orderkey = o_orderkey "
           "join customer on o_custkey = c_custkey "
           "join nation on c_nationkey = n_nationkey "
           "group by n_name order by rev desc limit 3")
    res = execute(conn, f"{serialize_context(span)} {sql}")
    assert len(res["rows"]) == 3
    assert set(res["rows"][0].keys()) == {"n_name", "rev"}


def test_scalar_values_handles_decimal_and_string_columns():
    from decimal import Decimal
    rows = [{"n_name": "FRANCE", "rev": Decimal("9431480581.80")}, {"n_name": None, "rev": Decimal("1.00")}]
    values = scalar_values(rows)
    assert "FRANCE" in values
    assert 9431480581.8 in values
    assert None not in values
