"""A simulated data agent answering "Why did revenue drop in July 2026?"

The shape of the speculation is what matters, and it mirrors what the BAIR
post describes: wide fan-out of near-duplicate probes, retries with cosmetic
differences, per-partition scans that one rollup would have covered, and a
dead-end hypothesis whose results never reach the answer.

Time is compressed: the agent "thinks" for THINK_MS/DILATION of real time
between queries. assemble.py scales elapsed time back up by DILATION before
applying real warehouse billing rules.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import DILATION, PG
from .context import new_span, new_trace
from .tracedb import TracedClient

JULY = "order_date >= '2026-07-01' and order_date <= '2026-07-31'"


def think(ms: float) -> None:
    time.sleep((ms / DILATION) / 1000)


def main() -> None:
    conn = psycopg.connect(**PG, autocommit=True, row_factory=dict_row)
    db = TracedClient(conn, mode="observe")

    trace = new_trace(
        agent_id="revenue-analyst-v3",
        model="claude-opus-5",
        task_intent="Why did revenue drop in July 2026?",
    )

    root = new_span(trace, "root", "probe")
    cited = []

    # --- Phase 1: schema discovery. Cheap, necessary, never cited. --------
    for t in ("orders", "refunds"):
        s = new_span(trace, f"discover schema {t}", "probe", parent=root["span_id"])
        db.run(s, f"select column_name, data_type from information_schema.columns where table_name = '{t}'")
        think(1500)
        p = new_span(trace, f"peek rows {t}", "probe", parent=root["span_id"])
        db.run(p, f"select * from {t} limit 5")
        think(1200)

    # --- Phase 2: confirm the drop is real. Cited. -------------------------
    h0 = new_span(trace, "confirm monthly drop", "refine", parent=root["span_id"])
    db.run(h0, "select sum(amount) from orders where order_date >= '2026-06-01' and order_date <= '2026-06-30'")
    think(2500)
    h0b = new_span(trace, "confirm monthly drop (july)", "refine", parent=root["span_id"])
    db.run(h0b, f"select sum(amount) from orders where {JULY}")
    cited += [h0["span_id"], h0b["span_id"]]
    think(3000)

    # --- Phase 3: hypothesis "fewer orders" — 31 per-day probes. -----------
    # Every one of these is subsumed by a single GROUP BY order_date.
    hA = new_span(trace, "hypothesis: order volume fell", "refine", parent=root["span_id"])
    db.run(hA, f"select count(order_id) from orders where {JULY}")
    think(2000)
    for d in range(1, 32):
        day = f"2026-07-{d:02d}"
        s = new_span(trace, f"daily volume {day}", "probe", parent=hA["span_id"])
        db.run(s, f"select count(order_id), sum(amount) from orders where order_date = '{day}'")
        think(900)

    # --- Phase 4: hypothesis "AOV fell" — retried 3x with cosmetic edits. --
    hB = new_span(trace, "hypothesis: AOV fell", "refine", parent=root["span_id"])
    db.run(hB, "select avg(amount) from orders where order_date >= '2026-06-01' and order_date <= '2026-06-30'")
    think(1800)
    aov_variants = [
        f"select avg(amount) from orders where {JULY}",
        "SELECT   avg(amount)  FROM orders o  WHERE o.order_date <= '2026-07-31' AND o.order_date >= '2026-07-01'",
        "select avg(amount) from orders as t where t.order_date >= '2026-07-01' and t.order_date <= '2026-07-31'",
    ]
    prev = None
    for i, variant in enumerate(aov_variants):
        s = new_span(trace, "avg order value july", "refine", parent=hB["span_id"], attempt=i + 1, retry_of=prev)
        db.run(s, variant)
        prev = s["span_id"]
        think(2000)

    # --- Phase 5: hypothesis "region/channel mix" — the real cause. --------
    hC = new_span(trace, "hypothesis: mix shift", "refine", parent=root["span_id"])
    db.run(hC, f"select region, sum(amount) from orders where {JULY} group by region")
    think(2200)
    regions = ["AMER", "EMEA", "APAC"]
    channels = ["paid_search", "organic", "partner", "email"]
    # 12 per-cell probes, all subsumed by one GROUP BY region, channel.
    for r in regions:
        for c in channels:
            s = new_span(trace, f"cell {r}/{c}", "probe", parent=hC["span_id"])
            db.run(s, f"select sum(amount) from orders where {JULY} and region = '{r}' and channel = '{c}'")
            think(800)
    # ...and then it runs the rollup anyway, having already paid for the cells.
    roll = new_span(trace, "rollup region x channel", "refine", parent=hC["span_id"])
    db.run(roll, f"select region, channel, sum(amount) from orders where {JULY} group by region, channel")
    cited.append(roll["span_id"])
    think(3500)

    # --- Phase 6: dead end — refunds. Never cited. -------------------------
    hD = new_span(trace, "hypothesis: refunds spiked", "probe", parent=root["span_id"])
    db.run(hD, "select count(refund_id) from refunds")
    think(1500)
    for q in (
        "select count(refund_id), sum(amount) from refunds where refund_date >= '2026-07-01' and refund_date <= '2026-07-31'",
        "select count(refund_id), sum(amount) from refunds where refund_date >= '2026-06-01' and refund_date <= '2026-06-30'",
        "select refund_date, sum(amount) from refunds where refund_date >= '2026-07-01' and refund_date <= '2026-07-31' group by refund_date",
    ):
        s = new_span(trace, "refund check", "probe", parent=hD["span_id"])
        db.run(s, q)
        think(1800)

    # --- Phase 7: narrow to the culprit, then confirm. ----------------------
    hE = new_span(trace, "isolate EMEA paid_search", "refine", parent=root["span_id"])
    db.run(hE, f"select sum(amount) from orders where {JULY} and region = 'EMEA' and channel = 'paid_search'")
    think(2000)
    for d in range(1, 32):
        day = f"2026-07-{d:02d}"
        s = new_span(trace, f"emea paid_search {day}", "probe", parent=hE["span_id"])
        db.run(s, f"select sum(amount) from orders where order_date = '{day}' and region = 'EMEA' and channel = 'paid_search'")
        think(700)

    fin = new_span(trace, "final confirming query", "final", parent=root["span_id"])
    db.run(fin, f"select order_date, sum(amount) from orders where {JULY} and region = 'EMEA' and channel = 'paid_search' group by order_date order by order_date")
    cited.append(fin["span_id"])

    db.cite_results(cited)
    out_path = Path(__file__).resolve().parent.parent / "out" / "agent-events.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(e) for e in db.dump_events()))

    conn.close()
    print(f"agent finished: {len(db.agent_events)} queries issued, {len(cited)} results cited in the answer")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)
