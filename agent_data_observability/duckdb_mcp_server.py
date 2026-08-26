"""MCP server exposing a local DuckDB warehouse to a real agent.

Trace context rides in a SQL comment, same as Postgres — but unlike Postgres
there is no separate process reading an independent log after the fact.
Spans here are recorded directly by this server at call time, the same trust
model the Databricks Genie adapter uses. See duckdb_.py's module docstring
and docs/DUCKDB.md for why, and duckdb_check.py for the one place this
adapter does verify tagging against duckdb_logs() (within a live connection).

No credentials, no account, no network: the warehouse is TPC-H SF1,
generated locally on first use.
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

from .context import serialize_context
from .duckdb_ import connect, execute
from .readonly import read_only_refusal

EVENTS_PATH = Path(os.environ["TRACE_EVENTS_PATH"]) if os.environ.get("TRACE_EVENTS_PATH") \
    else Path(__file__).resolve().parent.parent / "out" / "duckdb-events.jsonl"
TRACE_ID = secrets.token_hex(8)
AGENT_ID = os.environ.get("AGENT_ID", "duckdb-analyst")
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
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[v] = True
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

    seq = 0
    spans_by_label: dict[str, str] = {}

    server = Server("traced-duckdb")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="run_sql",
                description=(
                    "Run a read-only SQL query against a local DuckDB analytics warehouse. "
                    "Returns up to 50 rows as JSON. "
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
            "attempt_n": 1,
            "retry_of": None,
        }
        spans_by_label[label] = span["span_id"]

        tagged = f"{serialize_context(span)} {sql}"
        rows: list[dict] = []
        error = None
        t0 = time.perf_counter()
        try:
            res = await asyncio.to_thread(execute, conn, tagged)
            rows = res["rows"]
        except Exception as e:
            error = str(e).split("\n")[0]
        client_ms = (time.perf_counter() - t0) * 1000

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps({
                "trace_id": span["trace_id"], "span_id": span["span_id"], "parent_span_id": span["parent_span_id"],
                "label": label, "speculation_class": span["speculation_class"], "span_intent": span["span_intent"],
                "sql": sql,
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

    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        conn.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
