"""Phase 0b — cross-session redundancy.

The single-agent run killed the INTRA-session redundancy thesis: one agent,
one question, seven well-targeted queries, nothing to deduplicate. This tests
the version that survives it:

    Many analysts' agents hit the same warehouse asking overlapping questions.
    How much of the corpus is answerable from a shared set of rollups?

That framing needs no agent to be wasteful — only for different people to ask
related questions about the same tables, which is what a company is.

    adobs-cross-session [--concurrency 4]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .real_agent import run_agent
from .shape import covering_set
from .trace import bill, reconstruct, sec2dollars

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / ".pgdata" / "pglog" / "queries.log"
OUT = ROOT / "out"

# Eight questions a real analytics team might ask of the same warehouse in a
# week. Deliberately overlapping — same tables, same date range, same
# dimensions — without being duplicates of each other.
QUESTIONS = [
    ("drop", "Why did revenue drop in July 2026 compared to June 2026?"),
    ("growth", "Which region had the strongest revenue growth in July 2026?"),
    ("aov", "What is the average order value by channel for July 2026, and how does it compare to June?"),
    ("anomaly", "Is there any day in July 2026 where order volume or revenue behaved unusually?"),
    ("emea", "Which channel contributes the most revenue in EMEA?"),
    ("refunds", "How much did refunds cost us in June and July 2026, and is the rate rising?"),
    ("mix", "Break down July 2026 revenue by region and channel and flag anything that looks anomalous."),
    ("daily", "What was total revenue per day in July 2026?"),
]


async def _pool(items, n, fn):
    results = [None] * len(items)
    idx = [0]
    lock = asyncio.Lock()

    async def worker():
        while True:
            async with lock:
                if idx[0] >= len(items):
                    return
                i = idx[0]
                idx[0] += 1
            results[i] = await fn(items[i])

    await asyncio.gather(*(worker() for _ in range(min(n, len(items)))))
    return results


def pct(n: float) -> str:
    return f"{n * 100:.1f}%"


def usd(n: float) -> str:
    return f"${n:.3f}" if n < 1 else f"${n:.2f}"


async def main() -> None:
    args = sys.argv[1:]
    concurrency = int(args[args.index("--concurrency") + 1]) if "--concurrency" in args else 4
    skip_run = "--analyze-only" in args

    if not skip_run:
        LOG.write_text("")
        print(f"==> launching {len(QUESTIONS)} agents (concurrency {concurrency})\n")

        async def launch(item):
            tag, question = item
            r = await run_agent(question, tag=tag)
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {tag.ljust(9)} {str(r['queries']).rjust(2)} queries · "
                  f"grounded {r.get('grounded', 0)}/{r['queries']} · "
                  f"{usd(r['cost']) if r.get('cost') is not None else 'n/a'}")
            return r

        runs = await _pool(QUESTIONS, concurrency, launch)
        total_cost = sum(r.get("cost") or 0 for r in runs)
        print(f"\n==> {sum(1 for r in runs if r['ok'])}/{len(runs)} succeeded, LLM cost {usd(total_cost)}\n")

    # ---- analysis -----------------------------------------------------------
    event_paths = [p for p in (OUT / f"{tag}-events.jsonl" for tag, _ in QUESTIONS) if p.exists()]
    spans = reconstruct(str(LOG), [str(p) for p in event_paths])
    if not spans:
        print("no tagged spans found — did the agents run?", file=sys.stderr)
        sys.exit(1)

    by_trace: dict = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)

    print("=" * 78)
    print(f"CROSS-SESSION ANALYSIS — {len(by_trace)} agent sessions, {len(spans)} queries")
    print("=" * 78)

    print("\n── PER-SESSION ───────────────────────────────────────────────")
    print(f"  {'session'.ljust(10)} {'queries'.rjust(7)} {'anchors'.rjust(7)} {'grounded'.rjust(8)}")
    per_trace_anchors = 0
    for group in by_trace.values():
        cover = covering_set([s["shape"] for s in group])
        per_trace_anchors += len(cover.anchors)
        agent_id = next((s.get("agent_id") for s in group if s.get("agent_id")), None)
        tag = (agent_id or "?").replace("claude-code-", "")
        grounded = sum(1 for s in group if s.get("grounded"))
        print(f"  {tag.ljust(10)} {str(len(group)).rjust(7)} {str(len(cover.anchors)).rjust(7)} {str(grounded).rjust(8)}")

    # The whole point: one covering set computed over every session at once.
    global_cover = covering_set([s["shape"] for s in spans])
    b = bill(spans, 1)

    print("\n── CROSS-SESSION REDUNDANCY ──────────────────────────────────")
    print(f"  queries across all sessions      {len(spans)}")
    print(f"  anchors needed per-session (sum) {per_trace_anchors}")
    print(f"  anchors needed GLOBALLY          {len(global_cover.anchors)}  covering {global_cover.covered_count}/{global_cover.total}")
    print(f"  excluded as unmodellable         {global_cover.unmodelled}  (parser declined rather than guess)")
    print(f"  modelled but uncovered           {len(global_cover.uncovered)}")
    cross_redundancy = 1 - len(global_cover.anchors) / per_trace_anchors if per_trace_anchors > 0 else 0
    print(f"\n  CROSS-SESSION REDUNDANCY         {pct(cross_redundancy)}")
    print("  (share of per-session rollups made unnecessary by sharing across sessions)")
    # Dedup must be measured against the queries the anchors could actually
    # serve. Dividing by every raw query counts the unmodellable ones as
    # "deduplicated", which they are not.
    print(f"  dedup vs COVERED queries         {pct(1 - len(global_cover.anchors) / max(global_cover.covered_count, 1))}"
          f"   ({len(global_cover.anchors)} anchors serve {global_cover.covered_count})")
    print(f"  dedup vs MODELLED queries        {pct(1 - len(global_cover.anchors) / max(global_cover.total, 1))}")

    print("\n  Shared anchors — each serves queries from multiple sessions:")
    for a in global_cover.anchors[:6]:
        covers = a["covers"]
        anchor = a["anchor"]
        sessions = len({spans[i]["trace_id"] for i in covers})
        synth = "SYNTH" if anchor.synthetic else "obs. "
        print(f"    covers {str(len(covers)).rjust(3)} queries across {sessions} session(s)  [{synth}]")
        print(f"      {(anchor.sql or '')[:92]}")

    print("\n── COST ──────────────────────────────────────────────────────")
    total_cost = sec2dollars(b["billedSec"])
    print(f"  billed warehouse-seconds         {b['billedSec']:.0f}s   {usd(total_cost)}")
    print(f"  productive (query exec) seconds  {b['productiveSec']:.1f}s   {usd(sec2dollars(b['productiveSec']))}")
    print(f"  idle + cold-start tax            {b['overhead']:.0f}s   {usd(sec2dollars(b['overhead']))}   ({pct(b['overhead'] / b['billedSec'])})")
    print(f"  cost per session                 {usd(total_cost / len(by_trace))}")

    print("\n── CITATION VERIFICATION (all sessions) ──────────────────────")
    claimed = sum(1 for s in spans if s.get("used_downstream"))
    grounded = sum(1 for s in spans if s.get("grounded"))
    print(f"  self-reported as cited           {claimed}/{len(spans)}")
    print(f"  verified grounded in the answer  {grounded}/{len(spans)}")
    print("")

    (OUT / "cross-session-summary.json").write_text(json.dumps({
        "sessions": len(by_trace),
        "queries": len(spans),
        "perTraceAnchors": per_trace_anchors,
        "globalAnchors": len(global_cover.anchors),
        "crossRedundancy": cross_redundancy,
        "claimed": claimed, "grounded": grounded,
        "billedSec": b["billedSec"], "productiveSec": b["productiveSec"],
    }, indent=2))


def _main() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _main()
