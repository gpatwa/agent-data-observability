"""MCP server exposing the traced warehouse to a real LLM agent.

This is the production shape of the middleware: the agent talks to a tool,
the tool injects trace context as a SQL comment, and the warehouse logs it.
The agent never sees the trace machinery.

Lineage here is AGENT-DECLARED rather than harness-assigned: the tool schema
asks the model for `intent` and `follows_from`, so the plan tree is the
agent's own account of its reasoning, not our reconstruction of it.
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
import psycopg
from mcp.server import Server
from mcp.server.stdio import stdio_server
from psycopg.rows import dict_row

from .config import PG
from .context import serialize_context
from .readonly import read_only_refusal

EVENTS_PATH = Path(os.environ["TRACE_EVENTS_PATH"]) if os.environ.get("TRACE_EVENTS_PATH") \
    else Path(__file__).resolve().parent.parent / "out" / "agent-events.jsonl"
TRACE_ID = secrets.token_hex(8)
AGENT_ID = os.environ.get("AGENT_ID", "claude-code-analyst")
MODEL_ID = os.environ.get("AGENT_MODEL", "claude-opus-5")
QUESTION = os.environ.get("TRACE_QUESTION")

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def scalar_values(rows: list[dict]) -> list:
    """Scalar values from a result set, used later to verify — rather than
    trust — which query results actually reached the agent's final answer."""
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
            # psycopg returns numeric/bigint columns as Decimal or str
            # depending on adapter config. Keeping them as strings makes them
            # substring-matched later, which never fires against rounded
            # prose ("69.33" vs "69.331240..."). Coerce to float so the
            # verifier compares them numerically, with tolerance.
            s = str(v)
            if _NUMERIC_RE.match(s.strip()):
                out[float(s)] = True
            elif len(s) <= 64:
                out[s] = True
        if len(out) > 400:
            break
    return list(out.keys())


async def run() -> None:
    conn = await asyncio.to_thread(psycopg.connect, **PG, autocommit=True, row_factory=dict_row)
    # Defence in depth: even if the parser is fooled, the session cannot write.
    # A production deployment would use a role with SELECT-only grants instead
    # of relying on a session setting the agent could in principle reset.
    conn.execute("set default_transaction_read_only = on")
    conn.execute("set statement_timeout = 30000")

    seq = 0
    spans_by_label: dict[str, str] = {}

    server = Server("traced-warehouse")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        schema_hint = (
            "The warehouse contains many tables and you do not know the schema. "
            "Use information_schema.tables and information_schema.columns to discover "
            "what exists before querying it."
            if os.environ.get("WIDE_SCHEMA")
            else "Tables: orders(order_id, order_date, region, channel, customer_id, amount), "
            "refunds(refund_id, order_id, refund_date, amount)."
        )
        return [
            types.Tool(
                name="run_sql",
                description=(
                    "Run a read-only SQL query against the analytics warehouse (PostgreSQL). "
                    "Returns up to 50 rows as JSON, plus a query id you can reference later. "
                    + schema_hint
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "The SELECT statement to run."},
                        "intent": {
                            "type": "string",
                            "description": (
                                "One short phrase describing what you are trying to learn with this "
                                'query, e.g. "daily revenue for July" or "check whether refunds spiked".'
                            ),
                        },
                        "follows_from": {
                            "type": "string",
                            "description": (
                                'Optional. The query id (e.g. "q3") whose result prompted this query. '
                                "Omit for a query that starts a new line of investigation."
                            ),
                        },
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
            # Classified from the agent's own declaration: a query that starts
            # a new line of investigation is a probe; one that follows a
            # prior result refines.
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
            cur = await asyncio.to_thread(conn.execute, tagged)
            rows = await asyncio.to_thread(cur.fetchall)
        except Exception as e:
            error = str(e).split("\n")[0]
        client_ms = (time.perf_counter() - t0) * 1000

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps({
                "trace_id": span["trace_id"],
                "span_id": span["span_id"],
                "parent_span_id": span["parent_span_id"],
                "label": label,
                "speculation_class": span["speculation_class"],
                "span_intent": span["span_intent"],
                "attempt_n": 1,
                "retry_of": None,
                "result_hash": hashlib.sha1(json.dumps(rows, default=str).encode()).hexdigest()[:12],
                "rows": len(rows),
                "client_ms": client_ms,
                "values": scalar_values(rows),
                "question": QUESTION,
                "error": error,
            }, default=str) + "\n")

        if error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"[{label}] SQL error: {error}")], isError=True,
            )
        shown = rows[:50]
        text = (
            f"[{label}] {len(rows)} row(s)"
            + (" (showing first 50)" if len(rows) > 50 else "")
            + f"\n{json.dumps(shown, indent=1, default=str)}"
        )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=False)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
