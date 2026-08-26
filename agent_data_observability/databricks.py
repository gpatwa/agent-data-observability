"""Databricks Genie adapter.

ARCHITECTURALLY DIFFERENT FROM THE OTHERS, AND THAT IS THE POINT.

Postgres: we inject trace context into the SQL and parse the server log.
Snowflake: we set QUERY_TAG and read it back from ACCOUNT_USAGE.
Genie:    we control NEITHER. Genie authors the SQL and runs it under its own
          session inside Databricks. There is no hook to inject anything.

This is the shape any real product faces — verification sitting ABOVE a
managed connection you do not own. What we still get is everything that
matters for answer-grounding:
    - the question asked
    - the SQL Genie generated (returned in the message attachments)
    - the result rows

So sub-expression analysis and value-grounding both still work; per-query
credit attribution does not, because that would need query_tags we cannot set.
system.query.history does expose query_tags as MAP<STRING,STRING> now, but
only for statements whose caller sets them — Genie's are its own.

Credentials from the environment only:
    DATABRICKS_HOST            https://<workspace>.cloud.databricks.com
    DATABRICKS_TOKEN           personal access token (or OAuth bearer)
    DATABRICKS_GENIE_SPACE_ID  the Genie space to query
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from . import env  # noqa: F401  (loads .env; shell env still wins)

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}
PENDING = {
    "IN_PROGRESS", "PENDING_WAREHOUSE", "EXECUTING_QUERY", "FETCHING_METADATA",
    "FILTERING_CONTEXT", "ASKING_AI", "SUBMITTING_QUERY",
}


def config() -> dict:
    import os

    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN")
    space_id = os.environ.get("DATABRICKS_GENIE_SPACE_ID")
    missing = [name for name, v in (
        ("DATABRICKS_HOST", host), ("DATABRICKS_TOKEN", token), ("DATABRICKS_GENIE_SPACE_ID", space_id),
    ) if not v]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)}. See docs/DATABRICKS.md.")
    if not host.startswith("https://"):
        raise RuntimeError(f"DATABRICKS_HOST must start with https:// (got {host[:40]})")
    return {"host": host, "token": token, "spaceId": space_id}


def _api(cfg: dict, path: str, method: str = "GET", body: Optional[dict] = None) -> dict:
    res = requests.request(
        method, f"{cfg['host']}{path}",
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
        json=body if body is not None else None,
        timeout=30,
    )
    if not res.ok:
        # Surface the API's own message — Databricks errors are specific and a
        # generic "request failed" wrapper throws that detail away.
        detail = res.text[:300]
        try:
            detail = res.json().get("message", detail)
        except ValueError:
            pass
        raise RuntimeError(f"{res.status_code} {path.split('?')[0]}: {detail}")
    return res.json() if res.text else {}


def start_conversation(cfg: dict, content: str) -> dict:
    return _api(cfg, f"/api/2.0/genie/spaces/{cfg['spaceId']}/start-conversation", "POST", {"content": content})


def send_message(cfg: dict, conversation_id: str, content: str) -> dict:
    return _api(
        cfg, f"/api/2.0/genie/spaces/{cfg['spaceId']}/conversations/{conversation_id}/messages",
        "POST", {"content": content},
    )


def get_message(cfg: dict, conversation_id: str, message_id: str) -> dict:
    return _api(cfg, f"/api/2.0/genie/spaces/{cfg['spaceId']}/conversations/{conversation_id}/messages/{message_id}")


def get_query_result(cfg: dict, conversation_id: str, message_id: str, attachment_id: str) -> dict:
    return _api(
        cfg,
        f"/api/2.0/genie/spaces/{cfg['spaceId']}/conversations/{conversation_id}"
        f"/messages/{message_id}/query-result/{attachment_id}",
    )


def wait_for_message(cfg: dict, conversation_id: str, message_id: str,
                      timeout_s: float = 300, interval_s: float = 2) -> dict:
    """Poll until the message reaches a terminal status. Genie can take
    minutes on a cold warehouse, so the ceiling is generous but bounded — an
    unbounded poll against a stuck message is how a harness hangs forever."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = get_message(cfg, conversation_id, message_id)
        status = last.get("status") or last.get("state")
        if status in TERMINAL:
            return last
        if status and status not in PENDING:
            # Unknown status: return rather than spin, and let the caller decide.
            return last
        time.sleep(interval_s)
    raise TimeoutError(f"Genie message {message_id} did not finish within {timeout_s}s")


def extract_attachments(message: Optional[dict]) -> dict:
    """Pull the parts we care about out of Genie's response shape."""
    out = {"text": None, "sql": None, "attachmentId": None, "description": None}
    for a in (message or {}).get("attachments") or []:
        text_content = (a.get("text") or {}).get("content")
        if text_content and not out["text"]:
            out["text"] = text_content
        query = a.get("query")
        if query:
            out["sql"] = query.get("query") or query.get("statement")
            out["description"] = query.get("description")
            out["attachmentId"] = a.get("attachment_id") or a.get("id")
    return out


def rows_from_result(result: Optional[dict]) -> list[dict]:
    """Genie returns results in statement-execution shape: a manifest of
    column names plus a data_array of row arrays. Normalise to objects so the
    rest of the harness (value grounding especially) sees the same thing it
    does from Postgres and Snowflake."""
    sr = (result or {}).get("statement_response", result) if result else {}
    sr = sr or {}
    cols = [c.get("name") for c in ((sr.get("manifest") or {}).get("schema") or {}).get("columns") or []]
    data = (sr.get("result") or {}).get("data_array")
    if data is None:
        data = (sr.get("result") or {}).get("data_typed_array") or []
    if not cols:
        return []
    out = []
    for row in data:
        if isinstance(row, list):
            vals = row
        else:
            vals = [v.get("str") if isinstance(v, dict) else v for v in (row.get("values") or [])]
        out.append({c: (vals[i] if i < len(vals) else None) for i, c in enumerate(cols)})
    return out
