"""Shared trace reconstruction and billing.

Extracted so the single-trace report, the cross-session analysis, and the
citation verifier all read traces the same way. There is exactly one parser
for the warehouse log and one billing model in this repo.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .context import parse_context
from .shape import exact_hash, ast_hash, extract_shape

# --- Warehouse billing model (Snowflake XS, Standard edition) --------------
BILLING = {
    "CREDITS_PER_HOUR": 1,       # XS warehouse
    "DOLLARS_PER_CREDIT": 3.0,
    "MIN_BILLING_SEC": 60,       # charged on every resume
    "AUTO_SUSPEND_SEC": 60,
}


def sec2dollars(s: float) -> float:
    return (s / 3600) * BILLING["CREDITS_PER_HOUR"] * BILLING["DOLLARS_PER_CREDIT"]


_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \S+ \[(\d+)\] "
    r"(LOG|ERROR|STATEMENT|HINT|WARNING|DETAIL|FATAL):\s+(.*)$"
)
_DURATION_RE = re.compile(r"duration: ([\d.]+) ms")


def _ts_to_ms(ts: str) -> float:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
    return dt.timestamp() * 1000


def parse_log(path) -> list[dict]:
    lines = Path(path).read_text().split("\n")
    events = []
    cur = None

    for line in lines:
        m = _LOG_LINE.match(line)
        if not m:
            if cur and cur["kind"] == "statement":
                cur["text"] += "\n" + line
            continue
        ts, pid, _level, rest = m.groups()
        t = _ts_to_ms(ts)

        if rest.startswith("statement: "):
            if cur:
                events.append(cur)
            cur = {"kind": "statement", "t": t, "pid": pid, "text": rest[len("statement: "):]}
        elif rest.startswith("duration: "):
            dm = _DURATION_RE.search(rest)
            ms = float(dm.group(1)) if dm else 0.0
            if cur and cur["kind"] == "statement" and cur["pid"] == pid:
                cur["duration_ms"] = ms
                events.append(cur)
                cur = None
        else:
            if cur:
                events.append(cur)
            cur = None
    if cur:
        events.append(cur)
    return [e for e in events if e["kind"] == "statement"]


def reconstruct(log_path, event_paths) -> list[dict]:
    """Rebuild spans from the warehouse log, then join the agent-side event
    files. `event_paths` may list several files — one per agent session."""
    spans = []
    for s in parse_log(log_path):
        ctx = parse_context(s["text"])
        if not ctx:
            continue  # untagged traffic (seeding, admin) — ignored
        sql = re.sub(r"/\*agenttrace:[^*]*\*/\s*", "", s["text"])
        span = dict(ctx)
        span.update({
            "sql": sql,
            "start_ms": s["t"],
            "exec_ms": s.get("duration_ms", 0),
            "exact": exact_hash(sql),
            "ast": ast_hash(sql),
            "shape": extract_shape(sql),
        })
        spans.append(span)

    by_id = {}
    paths = event_paths if isinstance(event_paths, (list, tuple)) else [event_paths]
    for p in [p for p in paths if p]:
        try:
            raw = Path(p).read_text()
        except OSError:
            continue
        for line in raw.split("\n"):
            if not line:
                continue
            e = json.loads(line)
            by_id[e["span_id"]] = e

    for sp in spans:
        e = by_id.get(sp["span_id"])
        sp["used_downstream"] = (e or {}).get("used_downstream", False)
        sp["grounded"] = (e or {}).get("grounded")
        sp["result_hash"] = (e or {}).get("result_hash")
        sp["rows"] = (e or {}).get("rows")
        sp["label"] = (e or {}).get("label")
        sp["question"] = (e or {}).get("question")

    spans.sort(key=lambda s: s["start_ms"])
    return spans


def bill(spans: list[dict], dilation: float = 1) -> Optional[dict]:
    """Apply warehouse billing to a set of spans. `dilation` scales elapsed
    wall-clock only — used by the simulated agent, whose think-time is
    compressed. Real traces pass 1."""
    if not spans:
        return None
    t0 = spans[0]["start_ms"]
    intervals = []
    for s in spans:
        start = ((s["start_ms"] - t0) / 1000) * dilation
        intervals.append({"start": start, "end": start + s["exec_ms"] / 1000, "span": s})

    # Group into resume windows separated by more than AUTO_SUSPEND_SEC of idle.
    windows = []
    w = None
    for iv in intervals:
        if w is None or iv["start"] > w["lastEnd"] + BILLING["AUTO_SUSPEND_SEC"]:
            w = {"start": iv["start"], "lastEnd": iv["end"], "items": [iv]}
            windows.append(w)
        else:
            w["lastEnd"] = max(w["lastEnd"], iv["end"])
            w["items"].append(iv)

    billed_sec = 0.0
    for win in windows:
        win["billed"] = max(
            BILLING["MIN_BILLING_SEC"],
            win["lastEnd"] - win["start"] + BILLING["AUTO_SUSPEND_SEC"],
        )
        billed_sec += win["billed"]

    productive_sec = sum(iv["end"] - iv["start"] for iv in intervals)
    overhead = billed_sec - productive_sec
    for iv in intervals:
        prod = iv["end"] - iv["start"]
        if productive_sec > 0:
            share = prod + (prod / productive_sec) * overhead
        else:
            share = prod + overhead / len(intervals)
        iv["span"]["billed_sec"] = share
        iv["span"]["cost"] = sec2dollars(share)

    batched_sec = max(BILLING["MIN_BILLING_SEC"], productive_sec + BILLING["AUTO_SUSPEND_SEC"])

    return {
        "windows": windows,
        "billedSec": billed_sec,
        "productiveSec": productive_sec,
        "overhead": overhead,
        "batchedSec": batched_sec,
        "elapsedSec": intervals[-1]["end"],
    }
