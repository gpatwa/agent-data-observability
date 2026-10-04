"""OTLP/JSON trace encoding, with no SDK dependency.

Spans here are built after the fact from records that already exist (the
agent run, the warehouse's query history), so their IDs and timestamps are
fixed before encoding. Writing OTLP/JSON directly keeps those IDs exactly as
they appear in QUERY_TAG; an SDK would mint its own. The output can be
POSTed to any OTLP/HTTP collector at `/v1/traces`.

OTLP/JSON differs from protobuf-JSON in two ways that matter: trace and span
IDs are lowercase hex (not base64), and 64-bit integers are strings.
"""

from __future__ import annotations

import json
from typing import Optional

import requests

SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_ERROR = 2


def attr(key: str, value) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def span(trace_id: str, span_id: str, parent_span_id: Optional[str], name: str, kind: int,
         start_ms: float, end_ms: float, attributes: dict, error: Optional[str] = None) -> dict:
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(int(start_ms * 1_000_000)),
        "endTimeUnixNano": str(int(max(end_ms, start_ms) * 1_000_000)),
        "attributes": [attr(k, v) for k, v in attributes.items() if v is not None],
    }
    if parent_span_id:
        s["parentSpanId"] = parent_span_id
    if error:
        s["status"] = {"code": STATUS_ERROR, "message": error}
    return s


def payload(service_name: str, spans: list[dict]) -> dict:
    return {
        "resourceSpans": [{
            "resource": {"attributes": [attr("service.name", service_name)]},
            "scopeSpans": [{
                "scope": {"name": "agent-data-observability"},
                "spans": spans,
            }],
        }],
    }


def post(endpoint: str, body: dict) -> int:
    """POST to an OTLP/HTTP collector. `endpoint` is the base URL, e.g.
    http://localhost:4318; `/v1/traces` is appended."""
    url = endpoint.rstrip("/") + "/v1/traces"
    res = requests.post(url, data=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=10)
    res.raise_for_status()
    return res.status_code
