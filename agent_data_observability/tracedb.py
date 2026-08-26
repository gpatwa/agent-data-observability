"""The middleware. PHASE 1 (`mode='observe'`) is out-of-path: it injects the
trace comment and forwards, and every query reaches the warehouse. PHASE 2
(`mode='intercept'`) is that same seam actually flipped in-path: a query is
first checked against a `MaterializedCache` (materialize.py) and answered
from there when it can be, correctly, with no arithmetic reconstruction — see
materialize.py's module docstring for exactly what that does and doesn't
cover. Anything the cache can't safely answer still goes to the warehouse,
identically to observe mode.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from .context import serialize_context
from .shape import extract_shape


class TracedClient:
    def __init__(self, pg_conn, mode: str = "observe", cache=None):
        self.pg = pg_conn
        self.mode = mode  # 'observe' (out of path) | 'intercept' (Phase 2: cache-first)
        self.cache = cache  # a materialize.MaterializedCache, required for 'intercept'
        self.agent_events: list[dict] = []
        self.cited: Optional[set] = None

    def run(self, span, sql: str):
        if self.mode == "intercept" and self.cache is not None:
            shape = extract_shape(sql)
            if shape is not None:
                entry = self.cache.find(shape)
                if entry is not None:
                    return self._run_cached(span, sql, shape, entry)
        return self._run_warehouse(span, sql)

    def _run_warehouse(self, span, sql: str):
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
            "served_by": "warehouse",
        })
        return rows

    def _run_cached(self, span, sql: str, shape, entry):
        # No SQL comment is injected and nothing is sent to the warehouse at
        # all — that absence is the entire point of Phase 2.
        t0 = time.perf_counter()
        rows = self.cache.serve(entry, shape, sql)
        elapsed_ms = (time.perf_counter() - t0) * 1000

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
            "rows": len(rows),
            "client_ms": elapsed_ms,
            "served_by": "cache",
            "cache_anchor_table": entry.table,
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
