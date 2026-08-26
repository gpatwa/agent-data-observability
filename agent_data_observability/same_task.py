"""Replication of the actual published measurement.

WHAT I GOT WRONG: every earlier condition in this repo gave each agent a
DIFFERENT question, then reported that the redundancy thesis "did not
reproduce". The published claim is not about different questions. From the
EPIC Lab paper (arXiv 2509.00997):

    BIRD text-to-SQL benchmark, 50 independent attempts PER TASK with
    GPT-4o-mini; redundancy is "the proportion of distinct sub-expressions
    relative to total sub-expressions across multiple agent attempts", and
    "the number of distinct sub-plans of each size is often a small fraction
    of less than 10-20% of the total".

So the setup is N agents attempting the SAME task, and the unit is
SUB-EXPRESSIONS, not whole queries. This script measures both, at both
levels, so the comparison is finally like-for-like.

    adobs-same-task [--attempts 8] [--question "..."] [--analyze-only]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

from .real_agent import run_agent
from .shape import covering_set
from .trace import reconstruct

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / ".pgdata" / "pglog" / "queries.log"
OUT = ROOT / "out"


def _arg(name: str, default):
    args = sys.argv[1:]
    flag = f"--{name}"
    return args[args.index(flag) + 1] if flag in args else default


ATTEMPTS = int(_arg("attempts", 8))
QUESTION = _arg("question", "Why did revenue drop in July 2026 compared to June 2026?")


def pct(n: float) -> str:
    return f"{n * 100:.1f}%"


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


# --- sub-expression decomposition -----------------------------------------
# The paper counts sub-plans by size. This approximates that: each aggregate
# query is broken into the pieces a plan would share — the filtered scan, the
# grouping, the individual measures, and the whole aggregate — so overlap can
# be counted at each level rather than only for identical whole queries.
def sub_expressions(shape) -> list[dict]:
    if shape is None:
        return []
    out = []
    scan = f"scan({shape.table})"
    out.append({"size": 1, "kind": "scan", "key": scan})
    for f in shape.filters:
        out.append({"size": 1, "kind": "filter", "key": f"{scan}|{f}"})
    out.append({"size": 2, "kind": "filtered_scan", "key": f"{scan}|{'&'.join(shape.filters)}"})
    for m in shape.measures:
        out.append({"size": 2, "kind": "measure", "key": f"{scan}|{m}"})
    if shape.groupby:
        out.append({"size": 3, "kind": "grouping", "key": f"{scan}|by:{','.join(shape.groupby)}"})
    out.append({
        "size": 4, "kind": "full_agg",
        "key": f"{scan}|{'&'.join(shape.filters)}|by:{','.join(shape.groupby)}|{','.join(shape.measures)}",
    })
    return out


def ratio(items: list) -> dict:
    total = len(items)
    distinct = len(set(items))
    return {"total": total, "distinct": distinct, "distinctPct": (distinct / total) if total else 1}


async def main() -> None:
    args = sys.argv[1:]
    analyze_only = "--analyze-only" in args
    tags = [f"same{i + 1}" for i in range(ATTEMPTS)]

    if not analyze_only:
        LOG.write_text("")
        print(f"==> {ATTEMPTS} independent attempts at ONE task")
        print(f'    "{QUESTION}"\n')

        async def launch(tag):
            r = await run_agent(QUESTION, tag=tag)
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {tag.ljust(8)} {str(r['queries']).rjust(2)} queries · ${(r.get('cost') or 0):.3f}")
            return r

        runs = await _pool(tags, 4, launch)
        print(f"\n==> LLM cost ${sum(r.get('cost') or 0 for r in runs):.2f}\n")

    event_paths = [p for p in (OUT / f"{t}-events.jsonl" for t in tags) if p.exists()]
    spans = reconstruct(str(LOG), [str(p) for p in event_paths])
    if not spans:
        print("no spans found — run without --analyze-only first", file=sys.stderr)
        sys.exit(1)

    by_trace: dict = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)

    print("=" * 72)
    print(f"SAME-TASK REPLICATION — {len(by_trace)} attempts, {len(spans)} queries")
    print("=" * 72)
    print(f"\n  queries per attempt: {', '.join(str(len(g)) for g in by_trace.values())}")

    # --- whole-query level (what this repo measured before) -----------------
    print("\n── WHOLE-QUERY LEVEL (what this repo measured previously) ─────")
    ex = ratio([s["exact"] for s in spans])
    ast = ratio([s["ast"] for s in spans])
    print(f"  exact SQL          {ex['distinct']}/{ex['total']} distinct   {pct(ex['distinctPct'])}")
    print(f"  AST-normalized     {ast['distinct']}/{ast['total']} distinct   {pct(ast['distinctPct'])}")
    shapes = [s["shape"] for s in spans]
    cover = covering_set(shapes)
    print(f"  covering set       {len(cover.anchors)} anchors serve {cover.covered_count}/{cover.total} servable")
    print(f"  ({cover.unmodelled} queries unmodellable — joins/CTEs/schema lookups)")

    # --- sub-expression level (what the paper measured) ----------------------
    print("\n── SUB-EXPRESSION LEVEL (what the paper measured) ─────────────")
    all_sub = [e for s in spans for e in sub_expressions(s["shape"])]
    if not all_sub:
        print("  no modellable aggregate queries — cannot decompose")
    else:
        print(f"  {'size'.ljust(6)} {'kind'.ljust(15)} {'total'.rjust(6)} {'distinct'.rjust(9)} {'distinct %'.rjust(11)}")
        by_size: dict = {}
        for e in all_sub:
            k = f"{e['size']}|{e['kind']}"
            by_size.setdefault(k, []).append(e["key"])
        for k, keys in sorted(by_size.items()):
            size, kind = k.split("|")
            r = ratio(keys)
            print(f"  {size.ljust(6)} {kind.ljust(15)} {str(r['total']).rjust(6)} {str(r['distinct']).rjust(9)} {pct(r['distinctPct']).rjust(11)}")
        overall = ratio([e["key"] for e in all_sub])
        print(f"\n  ALL SUB-EXPRESSIONS  {overall['distinct']}/{overall['total']} distinct = {pct(overall['distinctPct'])}")
        print('  paper reports: "often a small fraction of less than 10-20% of the total"')
        if overall["distinctPct"] <= 0.20:
            verdict = "REPRODUCES the published range"
        elif overall["distinctPct"] <= 0.40:
            verdict = "partially — above the published range but substantial sharing"
        else:
            verdict = "does NOT reproduce at this scale"
        print(f"  → {verdict}")

    print("\n  Caveats: this is an approximation of plan sub-expressions from the")
    print("  query shape, not a real plan decomposition; the paper used BIRD with")
    print(f"  50 attempts on GPT-4o-mini, this is {len(by_trace)} attempts on a frontier model.")
    print("")


def _main() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _main()
