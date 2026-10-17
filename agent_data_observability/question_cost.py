"""Total cost of one agent question: LLM tokens plus the warehouse compute
its queries burned, in one trace.

The two halves come from different records. The LLM side is what the
`claude` CLI reported for the run. The warehouse side is found by trace ID in
Snowflake's own query history, not taken from the agent's event log, and the
two are reconciled: a query the warehouse saw but the agent didn't report
(or the reverse) is shown, not dropped.

Warehouse cost is reported three ways, because they differ by an order of
magnitude and a FinOps reader will ask which one is "the" cost:

    attributed   QUERY_ATTRIBUTION_HISTORY: credits Snowflake assigns to each
                 query. Measured, but excludes idle time and the per-resume
                 minimum, so it understates what you pay.
    billed est.  The warehouse's billing rules (60 s minimum per resume, runs
                 until auto-suspend) applied to Snowflake's own query start and
                 end times. Modelled, and assumes the warehouse was suspended
                 before the first query.
    metered      WAREHOUSE_METERING_HISTORY for the hours the run touched.
                 Measured, but hourly, so it includes anything else that ran on
                 the warehouse in those hours: an upper bound, not this
                 question's cost.

Credits come from ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY, which lags hours.
Run this right after the agent and the warehouse side reads "pending"; run it
again later and the same trace fills in measured credits. Nothing is
estimated in the meantime.

    adobs-question-cost <tag> [--credit-price 3.0] [--otlp-endpoint http://localhost:4318]
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional

from . import otel

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
_HEX = re.compile(r"^[0-9a-f]{32}$")


def load_run(tag: str) -> tuple[dict, list[dict]]:
    run = json.loads((OUT / f"sf-{tag}-run.json").read_text())
    events_path = OUT / f"sf-{tag}-events.jsonl"
    events = [json.loads(l) for l in events_path.read_text().split("\n") if l] if events_path.exists() else []
    return run, events


_QH_COLUMNS = """QUERY_ID, QUERY_TAG, QUERY_TEXT, START_TIME, END_TIME, TOTAL_ELAPSED_TIME,
               EXECUTION_TIME, BYTES_SCANNED, WAREHOUSE_NAME, WAREHOUSE_SIZE, EXECUTION_STATUS"""
# INFORMATION_SCHEMA.QUERY_HISTORY is near real-time but only covers 7 days;
# ACCOUNT_USAGE.QUERY_HISTORY keeps a year but lags up to ~45 minutes. A
# credits rerun naturally happens later, so both are needed.
_INFO_SCHEMA_DAYS = 6


def fetch_warehouse(conn, trace_id: str, since_unix_ms: float) -> list[dict]:
    """SELECTs Snowflake recorded under this trace ID, found by QUERY_TAG."""
    import time

    from .snowflake_ import execute

    if not _HEX.match(trace_id):
        raise ValueError(f"not a 32-hex trace id: {trace_id!r}")
    since = datetime.datetime.fromtimestamp(since_unix_ms / 1000 - 300, tz=datetime.timezone.utc)
    pattern = f'%"t":"{trace_id}"%'
    age_days = (time.time() * 1000 - since_unix_ms) / 86_400_000
    if age_days < _INFO_SCHEMA_DAYS:
        return execute(conn, f"""
            select {_QH_COLUMNS}
            from table(information_schema.query_history(
              end_time_range_start => %s::timestamp_ltz, result_limit => 10000))
            where QUERY_TAG like %s and QUERY_TYPE = 'SELECT'
            order by START_TIME""", [since.isoformat(), pattern])["rows"]
    return execute(conn, f"""
        select {_QH_COLUMNS}
        from snowflake.account_usage.query_history
        where START_TIME >= %s::timestamp_ltz and QUERY_TAG like %s and QUERY_TYPE = 'SELECT'
        order by START_TIME""", [since.isoformat(), pattern])["rows"]


def fetch_credits(conn, query_ids: list[str]) -> dict[str, float]:
    from .snowflake_ import execute

    if not query_ids:
        return {}
    marks = ", ".join(["%s"] * len(query_ids))
    rows = execute(conn, f"""
        select QUERY_ID, CREDITS_ATTRIBUTED_COMPUTE
        from snowflake.account_usage.query_attribution_history
        where QUERY_ID in ({marks})""", query_ids)["rows"]
    return {r["QUERY_ID"]: float(r["CREDITS_ATTRIBUTED_COMPUTE"] or 0) for r in rows}


CREDITS_PER_HOUR = {
    "X-Small": 1, "Small": 2, "Medium": 4, "Large": 8, "X-Large": 16,
    "2X-Large": 32, "3X-Large": 64, "4X-Large": 128, "5X-Large": 256, "6X-Large": 512,
}
MIN_BILLED_SEC = 60


def fetch_warehouse_config(conn, name: str) -> Optional[dict]:
    from .snowflake_ import execute

    rows = execute(conn, "show warehouses like %s", [name])["rows"]
    if not rows:
        return None
    r = {k.lower(): v for k, v in rows[0].items()}
    return {"name": r.get("name"), "size": r.get("size"), "auto_suspend_sec": int(r.get("auto_suspend") or 0)}


def fetch_metered(conn, name: str, start_unix_ms: float, end_unix_ms: float) -> Optional[float]:
    from .snowflake_ import execute

    s = datetime.datetime.fromtimestamp(start_unix_ms / 1000, tz=datetime.timezone.utc)
    e = datetime.datetime.fromtimestamp(end_unix_ms / 1000, tz=datetime.timezone.utc)
    rows = execute(conn, """
        select sum(CREDITS_USED) CR, count(*) N
        from snowflake.account_usage.warehouse_metering_history
        where WAREHOUSE_NAME = %s and START_TIME < %s::timestamp_tz and END_TIME > %s::timestamp_tz""",
        [name, e.isoformat(), s.isoformat()])["rows"]
    if not rows or not rows[0]["N"]:
        return None
    return float(rows[0]["CR"] or 0)


def billed_estimate(intervals: list[tuple[float, float]], auto_suspend_sec: int,
                    credits_per_hour: float) -> dict:
    """Snowflake's billing rules applied to query start/end times (ms). A new
    resume starts when a query begins after the warehouse would have
    auto-suspended; each resume bills max(60 s, busy span + auto-suspend)."""
    if not intervals:
        return {"resumes": 0, "billed_sec": 0.0, "credits": 0.0}
    windows = []
    for start, end in sorted(intervals):
        if windows and start <= windows[-1][1] + auto_suspend_sec * 1000:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    billed = sum(max(MIN_BILLED_SEC, (e - s) / 1000 + auto_suspend_sec) for s, e in windows)
    return {"resumes": len(windows), "billed_sec": billed, "credits": billed / 3600 * credits_per_hour}


def _ms(ts) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts.timestamp() * 1000
    return float(ts)


def build(run: dict, events: list[dict], wh_rows: list[dict], credits: dict[str, float],
          credit_price: float, warehouse_config: Optional[dict] = None,
          metered_credits: Optional[float] = None) -> dict:
    """Join the agent's record, the warehouse's record and credits into one
    summary plus OTLP spans. Pure: no I/O."""
    trace_id, root_id = run["trace_id"], run["root_span_id"]
    by_qid = {e["snowflake_query_id"]: e for e in events if e.get("snowflake_query_id")}
    wh_by_qid = {r["QUERY_ID"]: r for r in wh_rows}

    queries = []
    for qid in list(wh_by_qid) + [q for q in by_qid if q not in wh_by_qid]:
        e, w = by_qid.get(qid), wh_by_qid.get(qid)
        c = credits.get(qid)
        queries.append({
            "query_id": qid,
            "label": e.get("label") if e else None,
            "intent": e.get("span_intent") if e else None,
            "span_id": e["span_id"] if e else hashlib.sha1(qid.encode()).hexdigest()[:16],
            "parent_span_id": (e.get("parent_span_id") if e else None) or root_id,
            "seen_by": "both" if (e and w) else ("warehouse" if w else "agent"),
            "sql": (w or {}).get("QUERY_TEXT") or (e or {}).get("sql"),
            "start_ms": _ms((w or {}).get("START_TIME")) or (e or {}).get("started_unix_ms"),
            "end_ms": _ms((w or {}).get("END_TIME")) or (((e or {}).get("started_unix_ms") or 0) + ((e or {}).get("client_ms") or 0)),
            "execution_ms": (w or {}).get("EXECUTION_TIME"),
            "bytes_scanned": (w or {}).get("BYTES_SCANNED"),
            "warehouse": (w or {}).get("WAREHOUSE_NAME"),
            "warehouse_size": (w or {}).get("WAREHOUSE_SIZE"),
            "credits": c,
            "grounded": e.get("grounded") if e else None,
            "error": (e or {}).get("error"),
        })

    have = [q for q in queries if q["seen_by"] != "agent"]
    measured = [q for q in have if q["credits"] is not None]
    if have and len(measured) == len(have):
        status = "measured"
    elif measured:
        status = "partial"
    else:
        status = "pending"
    wh_credits = sum(q["credits"] for q in measured)
    wh_cost = wh_credits * credit_price
    llm = run.get("llm") or {}
    llm_cost = llm.get("cost_usd")
    usage = llm.get("usage") or {}
    input_tokens = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))

    billed = None
    if warehouse_config and warehouse_config.get("size") in CREDITS_PER_HOUR:
        spans_ms = [(q["start_ms"], q["end_ms"]) for q in have if q["start_ms"] and q["end_ms"]]
        billed = billed_estimate(spans_ms, warehouse_config["auto_suspend_sec"],
                                 CREDITS_PER_HOUR[warehouse_config["size"]])
        billed.update(auto_suspend_sec=warehouse_config["auto_suspend_sec"], size=warehouse_config["size"],
                      cost_usd=billed["credits"] * credit_price)

    summary = {
        "trace_id": trace_id, "tag": run.get("tag"), "question": run.get("question"),
        "llm_cost_usd": llm_cost, "input_tokens": input_tokens, "output_tokens": usage.get("output_tokens"),
        "warehouse_credits": wh_credits, "warehouse_cost_usd": wh_cost, "credits_status": status,
        "total_cost_usd": (llm_cost or 0) + wh_cost if status == "measured" else None,
        "billed_estimate": billed,
        "total_billed_est_usd": (llm_cost or 0) + billed["cost_usd"] if billed else None,
        "metered_credits": metered_credits,
        "metered_cost_usd": metered_credits * credit_price if metered_credits is not None else None,
        "queries": queries,
        "agent_only": sum(q["seen_by"] == "agent" for q in queries),
        "warehouse_only": sum(q["seen_by"] == "warehouse" for q in queries),
    }

    spans = [otel.span(
        trace_id, root_id, None, f"invoke_agent {run.get('agent_id')}", otel.SPAN_KIND_INTERNAL,
        run["started_unix_ms"], run["ended_unix_ms"], {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": run.get("agent_id"),
            "gen_ai.request.model": ",".join((llm.get("model_usage") or {}).keys()) or None,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            "agentdata.question": run.get("question"),
            "agentdata.llm.cost_usd": float(llm_cost) if llm_cost is not None else None,
            "agentdata.llm.turns": llm.get("num_turns"),
            "agentdata.warehouse.credits": wh_credits,
            "agentdata.warehouse.cost_usd": wh_cost,
            "agentdata.warehouse.credits_status": status,
            "agentdata.total_cost_usd": summary["total_cost_usd"],
            "agentdata.warehouse.billed_est.credits": billed["credits"] if billed else None,
            "agentdata.warehouse.billed_est.resumes": billed["resumes"] if billed else None,
            "agentdata.warehouse.billed_est.auto_suspend_sec": billed["auto_suspend_sec"] if billed else None,
            "agentdata.total_billed_est_usd": summary["total_billed_est_usd"],
            "agentdata.warehouse.metered_hour.credits": metered_credits,
            "agentdata.queries": len(queries),
        })]
    for q in queries:
        spans.append(otel.span(
            trace_id, q["span_id"], q["parent_span_id"],
            f"SELECT {q['label'] or q['query_id'][:8]}", otel.SPAN_KIND_CLIENT,
            q["start_ms"] or run["started_unix_ms"], q["end_ms"] or run["started_unix_ms"], {
                "db.system.name": "snowflake",
                "db.operation.name": "SELECT",
                "db.query.text": q["sql"],
                "snowflake.query_id": q["query_id"],
                "snowflake.warehouse.name": q["warehouse"],
                "snowflake.warehouse.size": q["warehouse_size"],
                "agentdata.intent": q["intent"],
                "agentdata.seen_by": q["seen_by"],
                "agentdata.execution_ms": q["execution_ms"],
                "agentdata.bytes_scanned": q["bytes_scanned"],
                "agentdata.credits": q["credits"],
                "agentdata.cost_usd": q["credits"] * credit_price if q["credits"] is not None else None,
                "agentdata.grounded": q["grounded"],
            }, error=q["error"]))
    summary["otlp"] = otel.payload("agent-data-observability", spans)
    return summary


def _usd(v) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print("usage: adobs-question-cost <tag> [--credit-price 3.0] [--otlp-endpoint URL]", file=sys.stderr)
        sys.exit(1)
    tag = args[0]
    price = float(args[args.index("--credit-price") + 1]) if "--credit-price" in args else 3.0
    endpoint = args[args.index("--otlp-endpoint") + 1] if "--otlp-endpoint" in args else None

    from .snowflake_ import connect

    run, events = load_run(tag)
    conn = connect()
    wh = fetch_warehouse(conn, run["trace_id"], run["started_unix_ms"])
    credits = fetch_credits(conn, [r["QUERY_ID"] for r in wh])
    wh_name = next((r["WAREHOUSE_NAME"] for r in wh if r.get("WAREHOUSE_NAME")), None)
    config = fetch_warehouse_config(conn, wh_name) if wh_name else None
    metered = fetch_metered(conn, wh_name, run["started_unix_ms"], run["ended_unix_ms"]) if wh_name else None
    conn.close()
    s = build(run, events, wh, credits, price, config, metered)

    print(f"── QUESTION COST · trace {s['trace_id']} ─────────────────────")
    print(f"  question         {s['question']}")
    print(f"  LLM              {_usd(s['llm_cost_usd'])}   ({s['input_tokens']:,} in / {s['output_tokens'] or 0:,} out tokens, as reported by the claude CLI)")
    if s["credits_status"] == "pending":
        print("  attributed       pending — QUERY_ATTRIBUTION_HISTORY lags hours; rerun later")
    else:
        print(f"  attributed       {_usd(s['warehouse_cost_usd'])}   ({s['warehouse_credits']:.6f} cr, {s['credits_status']}; excludes idle and the per-resume minimum)")
    b = s["billed_estimate"]
    if b:
        print(f"  billed est.      {_usd(b['cost_usd'])}   ({b['credits']:.6f} cr: {b['resumes']} resume(s), {b['billed_sec']:.0f} s billed, "
              f"auto-suspend {b['auto_suspend_sec']} s, {b['size']}; modelled from Snowflake's query times, assumes a cold start)")
    if s["metered_credits"] is not None:
        print(f"  metered hour(s)  {_usd(s['metered_cost_usd'])}   ({s['metered_credits']:.6f} cr — everything on the warehouse in those hours; an upper bound)")
    print(f"  total            {_usd(s['total_cost_usd']) if s['total_cost_usd'] is not None else 'not yet — attributed side ' + s['credits_status']}  (LLM + attributed)")
    if s["total_billed_est_usd"] is not None:
        print(f"                   {_usd(s['total_billed_est_usd'])}  (LLM + billed est.)")
    print(f"\n  queries          {len(s['queries'])} "
          f"(warehouse-only {s['warehouse_only']}, agent-only {s['agent_only']})")
    for q in s["queries"]:
        cred = "pending" if q["credits"] is None else f"{q['credits']:.6f} cr"
        print(f"   {(q['label'] or '?'):<4} {q['seen_by']:<9} {str(q['execution_ms'] or '-'):>6} ms  {cred:>14}  {(q['intent'] or '')[:44]}")

    path = OUT / f"sf-{tag}-trace.otlp.json"
    path.write_text(json.dumps(s["otlp"], indent=1))
    print(f"\n  OTLP trace       {path}")
    if endpoint:
        otel.post(endpoint, s["otlp"])
        print(f"  exported to      {endpoint}/v1/traces")


if __name__ == "__main__":
    main()
