"""Trace context: mint IDs for the agent plan tree and serialize them into a
SQL comment (sqlcommenter-style) so the warehouse logs them verbatim.

This is the ONLY thing that touches the data path, and all it does is prepend
a comment. No rewriting, no interception, no added latency.
"""

from __future__ import annotations

import re
import secrets
from typing import Optional
from urllib.parse import quote, unquote


def _id(n: int = 8) -> str:
    return secrets.token_hex(n)


def new_trace(agent_id: str, model: str, task_intent: str) -> dict:
    return {
        "trace_id": _id(8),
        "agent_id": agent_id,
        "model_id": model,
        "task_intent": task_intent,
        "span_stack": [],
    }


def new_span(trace: dict, intent: str, speculation_class: str,
             parent: Optional[str] = None, attempt: int = 1, retry_of: Optional[str] = None) -> dict:
    return {
        "trace_id": trace["trace_id"],
        "span_id": _id(6),
        "parent_span_id": parent,
        "agent_id": trace["agent_id"],
        "model_id": trace["model_id"],
        "task_intent": trace["task_intent"],
        "span_intent": intent,
        "speculation_class": speculation_class,  # probe | refine | final
        "attempt_n": attempt,
        "retry_of": retry_of,
    }


def serialize_context(span: dict) -> str:
    """sqlcommenter-style serialization. Values are URL-encoded so that
    quotes, spaces and `*/` in intent text can never break out of the
    comment."""
    fields = {
        "t": span["trace_id"],
        "s": span["span_id"],
        "p": span.get("parent_span_id") if span.get("parent_span_id") is not None else "-",
        "a": span["agent_id"],
        "m": span["model_id"],
        "c": span["speculation_class"],
        "n": str(span["attempt_n"]),
        "r": span.get("retry_of") if span.get("retry_of") is not None else "-",
        "i": span["span_intent"],
    }
    kv = ",".join(f"{k}={quote(str(v), safe='')}" for k, v in fields.items())
    return f"/*agenttrace:{kv}*/"


_CONTEXT_RE = re.compile(r"/\*agenttrace:([^*]*)\*/")


def parse_context(comment: str) -> Optional[dict]:
    m = _CONTEXT_RE.search(comment)
    if not m:
        return None
    out = {}
    for pair in m.group(1).split(","):
        eq = pair.find("=")
        if eq == -1:
            continue
        out[pair[:eq]] = unquote(pair[eq + 1:])
    return {
        "trace_id": out.get("t"),
        "span_id": out.get("s"),
        "parent_span_id": None if out.get("p") == "-" else out.get("p"),
        "agent_id": out.get("a"),
        "model_id": out.get("m"),
        "speculation_class": out.get("c"),
        "attempt_n": int(out["n"]) if out.get("n") is not None else None,
        "retry_of": None if out.get("r") == "-" else out.get("r"),
        "span_intent": out.get("i"),
    }
