"""End-to-end test of the actual claim this repo makes: an agent's queries go
into a warehouse, and the whole plan tree comes back out of the warehouse's
own log with nothing in the data path.

The unit tests cover pure functions. Nothing covered the pipeline, which is
where the interesting failures were (log parsing, dilation, denominators).

Needs a live Postgres with log_statement=all whose log file is readable, and
E2E_LOG_PATH pointing at it. scripts/e2e.sh manages that cluster.
Run: E2E_LOG_PATH=.pgdata-e2e/pglog/queries.log pytest tests/test_e2e_pipeline.py
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

import psycopg
import pytest

from agent_data_observability.config import PG
from agent_data_observability.context import new_span, new_trace, serialize_context
from agent_data_observability.readonly import read_only_refusal
from agent_data_observability.shape import covering_set, subsumes
from agent_data_observability.trace import bill, reconstruct, sec2dollars

LOG_PATH = os.environ.get("E2E_LOG_PATH")
pytestmark = pytest.mark.skipif(not LOG_PATH, reason="E2E_LOG_PATH must point at the cluster query log (see scripts/e2e.sh)")


@pytest.fixture(scope="module")
def fixture():
    conn = psycopg.connect(**PG, autocommit=True)

    # Small fixture — this test is about the pipeline, not query performance.
    conn.execute("drop table if exists e2e_orders")
    conn.execute("""create table e2e_orders (
        order_id bigserial primary key, order_date date, region text, channel text, amount numeric(10,2))""")
    conn.execute("""insert into e2e_orders (order_date, region, channel, amount)
        select (DATE '2026-07-01' + (n % 20))::date,
               (ARRAY['AMER','EMEA','APAC'])[1 + (n % 3)],
               (ARRAY['paid_search','organic','email'])[1 + (n % 3)],
               (n % 400 + 10)::numeric(10,2)
        from generate_series(1, 4000) n""")
    conn.execute("analyze e2e_orders")

    # Truncate the log so this test sees only its own traffic.
    Path(LOG_PATH).write_text("")

    trace = new_trace(agent_id="e2e-agent", model="test-model", task_intent="e2e pipeline check")
    root = new_span(trace, "total revenue", "probe")
    events_path = Path(tempfile.mkdtemp(prefix="adoe2e-")) / "events.jsonl"

    # A deliberate fan-out: five single-day probes that ONE rollup should serve.
    plan = [(root, "select sum(amount) from e2e_orders")]
    for d in (1, 2, 3, 4, 5):
        plan.append((
            new_span(trace, f"day {d}", "probe", parent=root["span_id"]),
            f"select sum(amount), count(*) from e2e_orders where order_date = '2026-07-0{d}'",
        ))

    issued = []
    events = []
    for span, sql in plan:
        assert read_only_refusal(sql) is None, "fixture SQL must pass the read-only guard"
        conn.execute(f"{serialize_context(span)} {sql}")
        issued.append({"span_id": span["span_id"], "sql": sql})
        events.append(json.dumps({
            "trace_id": span["trace_id"], "span_id": span["span_id"], "parent_span_id": span["parent_span_id"],
            "label": span["span_intent"], "used_downstream": True, "grounded": True, "values": [],
        }))
        time.sleep(0.06)  # distinct log timestamps
    events_path.write_text("\n".join(events))

    yield {"issued": issued, "events_path": str(events_path)}

    conn.execute("drop table if exists e2e_orders")
    conn.close()


def test_every_issued_query_is_recovered_from_the_warehouse_log(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    assert len(spans) == len(fixture["issued"]), \
        f"issued {len(fixture['issued'])} tagged queries, recovered {len(spans)}"
    span_ids = {s["span_id"] for s in spans}
    for i in fixture["issued"]:
        assert i["span_id"] in span_ids, f"span {i['span_id']} missing from the log"


def test_trace_context_survives_the_round_trip_through_sql_comments(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    assert len({s["trace_id"] for s in spans}) == 1, "one trace expected"
    assert all(s["agent_id"] == "e2e-agent" for s in spans)
    # The SQL comes back without the injected comment.
    assert all("agenttrace" not in s["sql"] for s in spans), "comment must be stripped from recovered SQL"


def test_parent_links_resolve_to_spans_that_exist(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    ids = {s["span_id"] for s in spans}
    children = [s for s in spans if s["parent_span_id"]]
    assert len(children) >= 5, "expected the fan-out children"
    for c in children:
        assert c["parent_span_id"] in ids, "dangling parent link"


def test_billing_is_internally_consistent(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    b = bill(spans, 1)
    assert b["productiveSec"] > 0, "queries must record execution time"
    assert b["billedSec"] >= b["productiveSec"], "billed time cannot be less than productive time"
    assert b["billedSec"] >= 60, "the 60s minimum must apply at least once"
    assert round((b["overhead"] + b["productiveSec"]) * 1000) == round(b["billedSec"] * 1000), \
        "overhead + productive must equal billed"
    summed = sum(s["cost"] for s in spans)
    assert abs(summed - sec2dollars(b["billedSec"])) < 1e-6, \
        "per-span attributed cost must sum to the total bill"


def test_dilation_scales_elapsed_time_but_never_execution_time(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    a = bill(spans, 1)
    b = bill(spans, 100)
    assert abs(a["productiveSec"] - b["productiveSec"]) < 1e-9, \
        "query execution time must not be scaled by dilation"
    assert b["elapsedSec"] > a["elapsedSec"], "elapsed wall-clock should scale"


def test_the_day_fan_out_collapses_to_a_single_synthesized_rollup(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    day_shapes = [s["shape"] for s in spans if re.search(r"order_date = ", s["sql"])]
    assert len(day_shapes) == 5
    cover = covering_set(day_shapes)
    assert cover.covered_count == 5, "all five day probes should be covered"
    assert len(cover.anchors) == 1, "one GROUP BY order_date rollup should serve all five"
    assert cover.anchors[0]["anchor"].synthetic, "and it should be synthesized — the agent never ran it"


def test_no_anchor_ever_claims_a_query_it_cannot_actually_serve(fixture):
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    shapes = [s["shape"] for s in spans]
    cover = covering_set(shapes)
    for a in cover.anchors:
        for i in a["covers"]:
            assert subsumes(a["anchor"], shapes[i]), "anchor covers a query it does not subsume"


def test_untagged_traffic_is_ignored_not_mis_attributed(fixture):
    conn = psycopg.connect(**PG, autocommit=True)
    conn.execute("select count(*) from e2e_orders")  # no trace comment
    conn.close()
    time.sleep(0.12)
    spans = reconstruct(LOG_PATH, [fixture["events_path"]])
    assert len(spans) == len(fixture["issued"]), "untagged query must not appear as a span"
