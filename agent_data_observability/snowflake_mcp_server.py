"""MCP server exposing Snowflake to a real agent, with trace context carried in
QUERY_TAG rather than a SQL comment.

Compared to the Postgres server this is strictly less machinery: no log file,
no log parser, no span reconstruction. Snowflake records the tag against the
query itself and reports credits for it.

Default dataset is SNOWFLAKE_SAMPLE_DATA.TPCH_SF1, which every trial account
has. That matters for validity: TPC-H has no planted anomaly, so an agent
must do real analysis rather than find an answer someone hid for it — which
was the weakest point of every condition run so far.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .readonly import read_only_refusal
from .snowflake_ import connect, execute, set_tag

EVENTS_PATH = Path(os.environ["TRACE_EVENTS_PATH"]) if os.environ.get("TRACE_EVENTS_PATH") \
    else Path(__file__).resolve().parent.parent / "out" / "snowflake-events.jsonl"
TRACE_ID = secrets.token_hex(8)
AGENT_ID = os.environ.get("AGENT_ID", "snowflake-analyst")
MODEL_ID = os.environ.get("AGENT_MODEL", "claude-opus-5")

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def scalar_values(rows: list[dict]) -> list:
    out: dict = {}
    for row in rows[:200]:
        for v in row.values():
            if v is None:
                continue
            if isinstance(v, (datetime.date, datetime.datetime)):
                out[v.isoformat()[:10]] = True
                continue
            s = str(v)
            if _NUMERIC_RE.match(s.strip()):
                out[float(s)] = True
            elif len(s) <= 64:
                out[s] = True
        if len(out) > 400:
            break
    return list(out.keys())


async def run() -> None:
    conn = await asyncio.to_thread(connect)
    # Belt and braces: the guard below refuses non-SELECT, and the session
    # cannot write anyway. A production deployment would use a role with
    # SELECT-only grants rather than relying on either.
    await asyncio.to_thread(execute, conn, "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 120")

    seq = 0
    spans_by_label: dict[str, str] = {}

    server = Server("traced-snowflake")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="run_sql",
                description=(
                    "Run a read-only SQL query against a Snowflake analytics warehouse. "
                    "Returns up to 50 rows as JSON plus a query id you can reference later. "
                    "The database is the TPC-H sample schema: customer, orders, lineitem, part, "
                    "partsupp, supplier, nation, region. Use information_schema to inspect columns."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "The SELECT statement to run."},
                        "intent": {"type": "string", "description": "One short phrase describing what you are trying to learn."},
                        "follows_from": {"type": "string", "description": 'Optional query id (e.g. "q3") whose result prompted this one.'},
                    },
                    "required": ["sql", "intent"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        nonlocal seq
        if name != "run_sql":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool: {name}")], isError=True,
            )

        sql = arguments.get("sql") or ""
        intent = arguments.get("intent")
        follows_from = arguments.get("follows_from")

        refusal = read_only_refusal(sql)
        if refusal:
            return types.CallToolResult(content=[types.TextContent(type="text", text=refusal)], isError=True)

        seq += 1
        label = f"q{seq}"
        span = {
            "trace_id": TRACE_ID,
            "span_id": secrets.token_hex(6),
            "parent_span_id": spans_by_label.get(follows_from) if follows_from else None,
            "agent_id": AGENT_ID,
            "model_id": MODEL_ID,
            "span_intent": intent or label,
            "speculation_class": "refine" if follows_from else "probe",
        }
        spans_by_label[label] = span["span_id"]

        rows: list[dict] = []
        error = None
        query_id = None
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(set_tag, conn, span)  # native trace context — no SQL comment
            res = await asyncio.to_thread(execute, conn, sql)
            rows = res["rows"]
            query_id = res["queryId"]  # joins to ACCOUNT_USAGE later
        except Exception as e:
            error = str(e).split("\n")[0]
        client_ms = (time.perf_counter() - t0) * 1000

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps({
                "trace_id": span["trace_id"], "span_id": span["span_id"], "parent_span_id": span["parent_span_id"],
                "label": label, "speculation_class": span["speculation_class"], "span_intent": span["span_intent"],
                "snowflake_query_id": query_id,
                "result_hash": hashlib.sha1(json.dumps(rows, default=str).encode()).hexdigest()[:12],
                "rows": len(rows), "client_ms": client_ms, "values": scalar_values(rows), "error": error,
            }, default=str) + "\n")

        if error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"[{label}] SQL error: {error}")], isError=True,
            )
        shown = rows[:50]
        text = (
            f"[{label}] {len(rows)} row(s)" + (" (showing first 50)" if len(rows) > 50 else "")
            + f"\n{json.dumps(shown, indent=1, default=str)}"
        )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=False)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
