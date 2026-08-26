"""Runs Phase 2 for real against a completed trace and reports what actually
happened — not the modelled recommendation `assemble.py` has always printed.

Takes a trace this repo already has evidence for (the simulated-agent demo,
by default), computes its covering set exactly as `assemble.py` does,
materializes each anchor into a local DuckDB cache by executing it against
Postgres ONCE, then replays every original query through
`tracedb.TracedClient(mode='intercept')` — the real code path an agent would
use — and separately re-executes the same query directly against Postgres,
so every cached answer is checked against a fresh warehouse answer rather
than assumed correct.

    adobs-phase2-demo [logPath] [eventsPath]

Defaults to the saved simulated-agent demo trace if no path is given and
`out/agent-events.jsonl` exists; otherwise run `./scripts/demo.sh` first.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import psycopg
from psycopg.rows import dict_row

from .config import PG
from .materialize import MaterializedCache
from .shape import covering_set
from .trace import reconstruct
from .tracedb import TracedClient

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / ".pgdata" / "pglog" / "queries.log"
DEFAULT_EVENTS = ROOT / "out" / "agent-events.jsonl"


def usd(n: float) -> str:
    return f"${n:.4f}" if n < 1 else f"${n:.2f}"


def _values_match(a: list[dict], b: list[dict], tol: float = 1e-6) -> bool:
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        va, vb = list(ra.values()), list(rb.values())
        if len(va) != len(vb):
            return False
        for x, y in zip(va, vb):
            try:
                if abs(float(x) - float(y)) > tol:
                    return False
            except (TypeError, ValueError):
                if x != y:
                    return False
    return True


def main() -> None:
    args = sys.argv[1:]
    log_path = Path(args[0]) if len(args) > 0 else DEFAULT_LOG
    events_path = Path(args[1]) if len(args) > 1 else DEFAULT_EVENTS
    if not log_path.exists() or not events_path.exists():
        print(f"error: need a completed trace. Run ./scripts/demo.sh first, or pass "
              f"<logPath> <eventsPath>.\n  looked for: {log_path}\n              {events_path}", file=sys.stderr)
        sys.exit(1)

    spans = reconstruct(str(log_path), [str(events_path)])
    if not spans:
        print("no tagged spans found in the given trace", file=sys.stderr)
        sys.exit(1)

    shapes = [s["shape"] for s in spans]
    cover = covering_set(shapes)

    pg = psycopg.connect(**PG, autocommit=True, row_factory=dict_row)
    dcon = duckdb.connect(":memory:")
    cache = MaterializedCache(dcon)

    print("=" * 78)
    print(f"PHASE 2 — materialize {len(cover.anchors)} anchor(s), replay {cover.total} servable quer(y/ies)")
    print("=" * 78)

    print("\n── MATERIALIZING ────────────────────────────────────────────")
    materialize_ms = 0.0
    for a in cover.anchors:
        t0 = time.perf_counter()
        entry = cache.materialize(pg, a["anchor"])
        ms = (time.perf_counter() - t0) * 1000
        materialize_ms += ms
        dims = ", ".join(a["anchor"].groupby) or "(none)"
        print(f"  {entry.table}: {entry.rows_materialized} rows, dims=[{dims}], "
              f"covers {len(a['covers'])} historical quer(y/ies), {ms:.1f}ms to build")

    print("\n── REPLAYING (through TracedClient, mode='intercept') ─────────")
    client = TracedClient(pg, mode="intercept", cache=cache)
    servable = 0
    declined = 0
    verified_ok = 0
    direct_ms_total = 0.0
    cached_ms_total = 0.0

    for s in spans:
        sql, shape = s.get("sql"), s.get("shape")
        if shape is None:
            continue

        t0 = time.perf_counter()
        direct_rows = pg.execute(sql).fetchall()
        direct_ms = (time.perf_counter() - t0) * 1000

        replay_span = {
            "trace_id": s["trace_id"], "span_id": s["span_id"], "parent_span_id": s["parent_span_id"],
            "agent_id": s.get("agent_id"), "model_id": s.get("model_id"),
            "speculation_class": s["speculation_class"], "span_intent": s["span_intent"],
            "attempt_n": s.get("attempt_n", 1), "retry_of": s.get("retry_of"),
        }
        via_client = client.run(replay_span, sql)
        event = client.agent_events[-1]

        if event["served_by"] != "cache":
            declined += 1  # would still hit the warehouse under Phase 2, same as Phase 1
            continue
        servable += 1
        direct_ms_total += direct_ms
        cached_ms_total += event["client_ms"]
        if _values_match(direct_rows, via_client):
            verified_ok += 1
        else:
            print(f"  MISMATCH on {s['span_id']}: cache={via_client} direct={direct_rows}")

    print("\n── RESULT ───────────────────────────────────────────────────")
    total_modellable = sum(1 for s in shapes if s is not None)
    print(f"  historical queries                {len(spans)}")
    print(f"  modellable (aggregate) queries     {total_modellable}")
    print(f"  answerable from the cache          {servable}  (no re-aggregation needed — see materialize.py)")
    print(f"  declined, still hits the warehouse {declined}  (needs re-aggregation, has its own GROUP BY,")
    print("                                       or is one of the anchor-defining queries itself)")
    print(f"  verified correct vs live Postgres  {verified_ok}/{servable}")
    print(f"  Postgres executions to materialize {len(cover.anchors)}   ({materialize_ms:.0f}ms total)")
    print(f"  Postgres executions AVOIDED        {servable}   (would have been {servable} separate warehouse round-trips)")
    if servable:
        print(f"\n  measured latency, direct Postgres  {direct_ms_total:.1f}ms total  ({direct_ms_total / servable:.2f}ms/query avg)")
        print(f"  measured latency, served from cache {cached_ms_total:.1f}ms total  ({cached_ms_total / servable:.2f}ms/query avg)")
        print(f"  speedup                            {direct_ms_total / max(cached_ms_total, 1e-6):.1f}x")
    print("\n  This is Phase 1's printed recommendation ('materialize N rollups to")
    print("  serve M queries') actually executed and checked, not modelled.")

    pg.close()
    dcon.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
