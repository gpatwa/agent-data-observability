"""Reconstructs the agent plan tree from the warehouse's own query log, joins
it to the agent-side event log, fingerprints the workload, and applies a
Snowflake-style billing model to attribute cost.

Nothing here sits in the data path. The only input from the DB side is a log
file the warehouse already writes.

Built on top of trace.py rather than re-implementing log parsing and billing —
the original JS `assemble.mjs` duplicated that logic before `trace.mjs` was
extracted from it; this port collapses back to one parser, one billing model.
"""

from __future__ import annotations

import re
import sys
import time

import psycopg

from .config import PG
from .shape import covering_set
from .trace import bill, reconstruct, sec2dollars

USD = lambda n: f"${n:.3f}" if n < 1 else f"${n:.2f}"
PCT = lambda n: f"{n * 100:.1f}%"
BAR = lambda n: "█" * max(1, round(n * 30))

_LABEL_NORM = [
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<date>"),
    (re.compile(r"\b(AMER|EMEA|APAC)\b"), "<region>"),
    (re.compile(r"\b(paid_search|organic|partner|email)\b"), "<channel>"),
]


def collapse_label(kids: list[dict]) -> dict:
    """Collapse runs of sibling spans whose intent differs only by a literal."""
    def norm(s: str) -> str:
        for pat, repl in _LABEL_NORM:
            s = pat.sub(repl, s)
        return s

    groups: dict = {}
    for k in kids:
        key = norm(k["span_intent"])
        groups.setdefault(key, []).append(k)
    return groups


def verify_anchors(anchors: list[dict]) -> list[dict]:
    conn = psycopg.connect(**PG, autocommit=True)
    out = []
    for a in anchors:
        sql = a["anchor"].sql
        row = dict(a)
        try:
            t0 = time.perf_counter()
            cur = conn.execute(sql)
            rows = cur.fetchall()
            ms = (time.perf_counter() - t0) * 1000
            row.update({"ms": ms, "rows": len(rows), "ok": True})
        except Exception as e:
            row.update({"ok": False, "err": str(e).split("\n")[0]})
        out.append(row)
    conn.close()
    return out


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: adobs-assemble <logPath> <eventsPath> [dilation]", file=sys.stderr)
        sys.exit(1)
    log_path, events_path = args[0], args[1]
    # Time dilation applies ONLY to the simulated agent, which compresses its
    # think-time by this factor. A real agent's log timestamps are already
    # real — passing a dilation there would multiply its idle gaps into
    # fictional ones. Default 1 (no scaling); the demo script passes 100.
    dilation = float(args[2]) if len(args) > 2 else 1

    spans = reconstruct(log_path, [events_path])
    if not spans:
        print("no tagged spans found in log", file=sys.stderr)
        sys.exit(1)
    b = bill(spans, dilation)

    total = len(spans)
    distinct_exact = len({s["exact"] for s in spans})
    distinct_ast = len({s["ast"] for s in spans})
    shapes = [s["shape"] for s in spans]
    agg_idx = [i for i, s in enumerate(shapes) if s is not None]
    cover = covering_set(shapes)
    total_cost = sec2dollars(b["billedSec"])

    print("=" * 78)
    print(f"TRACE {spans[0]['trace_id']}   agent={spans[0]['agent_id']}   model={spans[0]['model_id']}")
    print('TASK  "Why did revenue drop in July 2026?"')
    print(f"SOURCE  {total} tagged statements recovered from the Postgres log (nothing in the data path)")
    print(f"TIME    elapsed scaled {dilation:g}× "
          + ("(real timestamps)" if dilation == 1 else "(simulated think-time, decompressed)"))
    print("=" * 78)

    # --- plan tree ---
    print("\n── PLAN TREE (reconstructed from the warehouse log alone) ────────────────")
    children: dict = {}
    for s in spans:
        p = s["parent_span_id"] or "ROOT"
        children.setdefault(p, []).append(s)
    known = {s["span_id"] for s in spans}
    roots = [s for s in spans if not s["parent_span_id"] or s["parent_span_id"] not in known]

    def print_node(s: dict, depth: int) -> None:
        kids = children.get(s["span_id"], [])
        mark = "✓" if s.get("used_downstream") else " "
        pad = "  " * depth
        intent = s["span_intent"].ljust(max(0, 38 - depth * 2))
        print(f"{pad}{mark} [{s['speculation_class'].ljust(6)}] {intent} {USD(s['cost']).rjust(7)}")
        for label, group in collapse_label(kids).items():
            if len(group) > 3:
                c = sum(k["cost"] for k in group)
                print(f"{pad}    └─ {str(len(group)).rjust(2)}× {label.ljust(31)} {USD(c).rjust(7)}")
            else:
                for k in group:
                    print_node(k, depth + 1)

    for r in roots:
        print_node(r, 0)

    # --- redundancy ---
    print("\n── REDUNDANCY ────────────────────────────────────────────────")
    print(f"  queries issued                  {total}")
    print(f"  distinct, exact SQL             {distinct_exact}  → {PCT(1 - distinct_exact / total)} caught by literal match")
    print(f"  distinct, AST-normalized        {distinct_ast}  → {PCT(1 - distinct_ast / total)} caught by normalization")
    print(f"  aggregate queries               {len(agg_idx)}")
    print(f"  minimal covering set            {len(cover.anchors)} rollups answer {cover.covered_count}/{cover.total}")
    print(f"  excluded as unmodellable        {cover.unmodelled}  (schema lookups, joins, CTEs — not servable by a rollup)")
    # Measured against queries the anchors could actually serve. Dividing by
    # every query counts unmodellable ones as deduplicated, which they are not.
    distinct_plans = len(cover.anchors) / cover.covered_count if cover.covered_count else 1
    print(f"\n  DISTINCT SUB-PLANS              {PCT(distinct_plans)}   {BAR(distinct_plans)}   (of servable queries)")
    print(f"  REDUNDANCY                      {PCT(1 - distinct_plans)}   {BAR(1 - distinct_plans)}")
    print("  (BAIR post reports 10–20% distinct sub-plans across agent attempts)")

    print("\n  Synthesized anchors — note these are queries the agent NEVER RAN:")
    verified = verify_anchors(cover.anchors[:5])
    anchor_ms = 0.0
    for a in verified:
        tag = "SYNTH" if a["anchor"].synthetic else "obs. "
        status = f"{a['rows']} rows, {a['ms']:.0f}ms" if a["ok"] else f"FAILED: {a['err']}"
        print(f"    [{tag}] covers {str(len(a['covers'])).rjust(3)}  {status}")
        print(f"             {(a['anchor'].sql or '')[:92]}")
        if a["ok"]:
            anchor_ms += a["ms"]

    # --- waste ---
    print("\n── SPECULATION WASTE (requires the agent-side half of the trace) ─────────")
    cited = [s for s in spans if s.get("used_downstream")]
    uncited = [s for s in spans if not s.get("used_downstream")]
    waste_cost = sum(s["cost"] for s in uncited)
    print(f"  results that reached the answer  {len(cited)}/{total}")
    print(f"  cost of results that did not     {USD(waste_cost)} of {USD(total_cost)}   ({PCT(waste_cost / total_cost)})")
    dead_end = [s for s in uncited if re.search(r"refund", s["span_intent"], re.IGNORECASE)]
    print(f"  largest dead-end branch          \"refunds spiked\" — {len(dead_end)} queries, {USD(sum(s['cost'] for s in dead_end))}")

    # --- cost ---
    print("\n── COST ATTRIBUTION (Snowflake XS, $3/credit, 60s min, 60s auto-suspend) ──")
    print(f"  wall-clock span of the task      {b['elapsedSec']:.0f}s")
    print(f"  warehouse resumes                {len(b['windows'])}")
    print(f"  billed warehouse-seconds         {b['billedSec']:.0f}s   {USD(sec2dollars(b['billedSec']))}")
    print(f"  productive (query exec) seconds  {b['productiveSec']:.1f}s   {USD(sec2dollars(b['productiveSec']))}")
    print(f"  idle + cold-start tax            {b['overhead']:.0f}s   {USD(sec2dollars(b['overhead']))}   ({PCT(b['overhead'] / b['billedSec'])} of bill)")
    print(f"\n  → COST PER RESOLVED TASK         {USD(total_cost)}")
    print(f"    at 5k agent tasks/day           {USD(total_cost * 5000)}/day   {USD(total_cost * 5000 * 30)}/mo")

    # --- recommendations ---
    print("\n── PHASE-1 RECOMMENDATIONS (advice only — no query rewriting, no interception) ─")
    batched_cost = sec2dollars(b["batchedSec"])
    print("  1. Batch probes into one warehouse window")
    print("     think-time between probes is what pays the idle tax")
    print(f"     {USD(total_cost)} → {USD(batched_cost)} per task   ({total_cost / batched_cost:.1f}×)")
    print(f"  2. Materialize {len(cover.anchors)} rollups to serve {cover.covered_count} of {cover.total} aggregate queries")
    print(f"     measured anchor exec: {anchor_ms:.0f}ms total vs {b['productiveSec'] * 1000:.0f}ms of probe execution")
    saved = 1 - anchor_ms / (b["productiveSec"] * 1000) if b["productiveSec"] > 0 else 0
    print(f"     → {PCT(saved) if saved > 0 else '0%'} less compute for the same answers")
    print(f"  3. {len(uncited)} of {total} queries never informed the answer ({USD(waste_cost)}/task).")
    print(f'     Of those, the "refunds spiked" hypothesis is a fully dead branch ({len(dead_end)} queries)')
    print("     — the rest are probes whose findings the rollups would have surfaced in one shot.")
    print("")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)
