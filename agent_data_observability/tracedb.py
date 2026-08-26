"""The middleware. In PHASE 1 (this file) it is out-of-path: it injects the
trace comment and forwards. The same seam is where PHASE 2 would flip to
in-path mode (dedup / cache / approximate) behind a config flag."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from .context import serialize_context


class TracedClient:
    def __init__(self, pg_conn, mode: str = "observe"):
        self.pg = pg_conn
        self.mode = mode  # 'observe' (out of path) | 'intercept' (phase 2, not built)
        self.agent_events: list[dict] = []
        self.cited: Optional[set] = None

    def run(self, span, sql: str):
        if self.mode != "observe":
            raise NotImplementedError("intercept mode is Phase 2 — deliberately not implemented")
        tagged = f"{serialize_context(span)} {sql}"
        t0 = time.perf_counter()
        cur = self.pg.cursor()
        cur.execute(tagged)
        try:
            rows = cur.fetchall()
        except Exception:
            rows = []
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Agent-side event. This is the half the warehouse CANNOT see, and it
        # is what makes `used_downstream` computable later.
        result_hash = hashlib.sha1(json.dumps(rows, default=str).encode()).hexdigest()[:12]

        self.agent_events.append({
            "trace_id": span["trace_id"],
            "span_id": span["span_id"],
            "parent_span_id": span["parent_span_id"],
            "speculation_class": span["speculation_class"],
            "span_intent": span["span_intent"],
            "attempt_n": span["attempt_n"],
            "retry_of": span["retry_of"],
            "result_hash": result_hash,
            "rows": cur.rowcount,
            "client_ms": elapsed_ms,
        })
        return rows

    def cite_results(self, span_ids) -> None:
        """Called by the agent when it composes its final answer: records
        which upstream results actually informed the response."""
        self.cited = set(span_ids)

    def dump_events(self) -> list[dict]:
        return [
            {**e, "used_downstream": (e["span_id"] in self.cited) if self.cited is not None else None}
            for e in self.agent_events
        ]
