"""MCP server exposing Databricks Genie to a real agent, with verification
wrapped around a connection we do not control.

The tool here is `ask_genie` — natural language, not SQL. That is a genuine
difference from the Postgres and Snowflake servers: the agent no longer
authors queries, so this harness cannot inject trace context, cannot enforce
a read-only guard (Genie is read-only by construction), and cannot tag for
credit attribution.

What it CAN still do, and what makes this the interesting deployment shape:
    - record the question, the SQL Genie generated, and the result rows
    - extract scalar values for answer-grounding
    - reconstruct a plan tree from agent-declared intent/follows_from

Unity Catalog enforces the caller's row filters and column masks on every
Genie call, so governance is the platform's job here rather than ours.
"""

from __future__ import annotations

import asyncio
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

from .databricks import (
    config, extract_attachments, get_query_result, rows_from_result,
    send_message, start_conversation, wait_for_message,
)

EVENTS_PATH = Path(os.environ["TRACE_EVENTS_PATH"]) if os.environ.get("TRACE_EVENTS_PATH") \
    else Path(__file__).resolve().parent.parent / "out" / "databricks-events.jsonl"
TRACE_ID = secrets.token_hex(8)
AGENT_ID = os.environ.get("AGENT_ID", "genie-analyst")
MODEL_ID = os.environ.get("AGENT_MODEL", "claude-opus-5")

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def scalar_values(rows: list[dict]) -> list:
    out: dict = {}
    for row in rows[:200]:
        for v in row.values():
            if v is None:
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
    cfg = config()
    conversation_id: str | None = None  # Genie conversations are stateful; reuse one
    seq = 0
    spans_by_label: dict[str, str] = {}

    server = Server("traced-genie")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="ask_genie",
                description=(
                    "Ask a question in plain English about the data in this Databricks Genie space. "
                    "Genie writes and runs the SQL against Unity Catalog under your permissions, and "
                    "returns both the generated SQL and the result rows. You do not write SQL yourself."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question, in plain English."},
                        "intent": {"type": "string", "description": "One short phrase describing what you are trying to learn."},
                        "follows_from": {"type": "string", "description": 'Optional id (e.g. "q3") of the answer that prompted this question.'},
                    },
                    "required": ["question", "intent"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        nonlocal seq, conversation_id
        if name != "ask_genie":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool: {name}")], isError=True,
            )

        question = (arguments.get("question") or "").strip()
        intent = arguments.get("intent")
        follows_from = arguments.get("follows_from")
        if not question:
            return types.CallToolResult(content=[types.TextContent(type="text", text="question is required")], isError=True)

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
        sql = None
        genie_text = None
        error = None
        t0 = time.perf_counter()

        try:
            if conversation_id:
                started = await asyncio.to_thread(send_message, cfg, conversation_id, question)
            else:
                started = await asyncio.to_thread(start_conversation, cfg, question)

            if conversation_id is None:
                conversation_id = started.get("conversation_id") or (started.get("conversation") or {}).get("id")
            message_id = started.get("message_id") or started.get("id") or (started.get("message") or {}).get("id")
            if not conversation_id or not message_id:
                raise RuntimeError(f"unexpected Genie response shape: {json.dumps(started)[:200]}")

            done = await asyncio.to_thread(wait_for_message, cfg, conversation_id, message_id)
            status = done.get("status") or done.get("state")
            att = extract_attachments(done)
            sql = att["sql"]
            genie_text = att["text"] or att["description"]

            if status in ("FAILED", "CANCELLED"):
                error = (done.get("error") or {}).get("message") or f"Genie returned {status}"
            elif att["attachmentId"]:
                result = await asyncio.to_thread(get_query_result, cfg, conversation_id, message_id, att["attachmentId"])
                rows = rows_from_result(result)
        except Exception as e:
            error = str(e).split("\n")[0]

        client_ms = (time.perf_counter() - t0) * 1000

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps({
                "trace_id": span["trace_id"], "span_id": span["span_id"], "parent_span_id": span["parent_span_id"],
                "label": label, "speculation_class": span["speculation_class"], "span_intent": span["span_intent"],
                "question": question,
                "sql": sql,  # Genie authored this, not the agent
                "genie_text": genie_text,
                "conversation_id": conversation_id,
                "result_hash": hashlib.sha1(json.dumps(rows, default=str).encode()).hexdigest()[:12],
                "rows": len(rows),
                "client_ms": client_ms,
                "values": scalar_values(rows),
                "error": error,
            }, default=str) + "\n")

        if error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"[{label}] Genie error: {error}")], isError=True,
            )

        shown = rows[:50]
        parts = [f"[{label}] {len(rows)} row(s)" + (" (showing first 50)" if len(rows) > 50 else "")]
        if genie_text:
            parts.append(f"Genie: {genie_text}")
        if sql:
            parts.append(f"SQL Genie ran:\n{sql}")
        parts.append(json.dumps(shown, indent=1, default=str))

        return types.CallToolResult(content=[types.TextContent(type="text", text="\n\n".join(parts))], isError=False)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
