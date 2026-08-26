"""Drives a REAL LLM agent (Claude Code, headless) against traced Databricks
GENIE. Unlike the other adapters there is NO trace injection: Genie authors
and runs the SQL itself. Everything recorded is captured on our side from
what Genie returns. See docs/DATABRICKS.md.

The agent's only tool is `ask_genie`, served by databricks_genie_mcp_server.py.

    adobs-databricks-agent "Why did revenue drop in July 2026?"
    adobs-databricks-agent "..." --model claude-haiku-4-5 --tag haiku

Exported as run_agent() (async) so cross_session.py can fan out over many
questions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import env  # noqa: F401  (loads .env; shell env still wins)
from .verify_citations import parse_cited, verify

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

SYSTEM_APPEND = """
You are a data analyst with access to one tool: ask_genie, which answers plain
English questions about a Databricks Genie space. You do NOT write SQL — Genie
writes and runs it, and returns both the SQL it used and the result rows. You
have no filesystem and no other tools.

Work the question until you can answer it with evidence.

Two requirements on how you use ask_genie:
  - Always pass a short "intent" describing what you are trying to learn.
  - When a query was prompted by an earlier result, pass that query's id as
    "follows_from" (e.g. follows_from: "q3"). This records your reasoning chain.

End your final answer with a line in exactly this format, listing the query ids
whose results actually support your conclusion:

CITED: q1, q4, q9
""".strip()

# Only appended in the coordinator condition. Deliberately pushes toward
# parallel delegation — if independent investigators re-derive each other's
# aggregates, this is where it shows up.
DELEGATE_APPEND = """
This is a broad investigation. Use the Task tool to delegate independent lines
of inquiry to subagents, running several in parallel rather than working
sequentially yourself. Each subagent has the same run_sql tool against the same
warehouse. Give each one a distinct angle to investigate and have it report
back what it found.
""".strip()


async def run_agent(question: str, model: Optional[str] = None, tag: str = "agent",
                     wide: bool = False, subagents: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    events_path = OUT / f"dbx-{tag}-events.jsonl"
    answer_path = OUT / f"dbx-{tag}-answer.txt"
    config_path = OUT / f"dbx-{tag}-mcp.json"

    server_env = {
        "TRACE_EVENTS_PATH": str(events_path),
        "TRACE_QUESTION": question,
        "AGENT_MODEL": model or "claude-opus-5",
        "AGENT_ID": f"claude-code-{tag}",
        # Databricks credentials are read from this process's environment and
        # forwarded to the server subprocess via this env block, which lands
        # in the mcp-config JSON written to `out/` below. That file is
        # gitignored, but it is still a plaintext credential on disk while it
        # exists, so it gets owner-only permissions and is deleted as soon as
        # the agent run finishes (see the try/finally below) rather than left
        # behind indefinitely, as the original JS version did.
        **{k: v for k, v in os.environ.items() if k.startswith("DATABRICKS_")},
    }

    config_path.write_text(json.dumps({
        "mcpServers": {
            "traced-genie": {
                "command": sys.executable,
                "args": ["-m", "agent_data_observability.databricks_genie_mcp_server"],
                "env": server_env,
            },
        },
    }))
    os.chmod(config_path, 0o600)
    events_path.write_text("")

    tools = ["mcp__traced-genie__ask_genie"]
    # The coordinator condition: allow the agent to spawn parallel subagents,
    # each of which independently reaches the same warehouse through the same
    # traced tool. This is the shape the redundancy thesis was written for.
    # The subagent-spawning tool is named `Agent` in Claude Code, not `Task`.
    # Getting this wrong does not error — the model simply never delegates, and
    # the run looks like a valid single-agent trace. Verified against a probe
    # run that observed two `Agent` tool calls.
    if subagents:
        tools.append("Agent")

    args = [
        "claude",
        "-p", question,
        "--mcp-config", str(config_path),
        "--allowedTools", ",".join(tools),
        "--append-system-prompt", f"{SYSTEM_APPEND}\n\n{DELEGATE_APPEND}" if subagents else SYSTEM_APPEND,
        "--output-format", "json",
    ]
    if model:
        args += ["--model", model]

    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out_b, err_b = await proc.communicate()
    finally:
        # The credentials in this file are only needed for the subprocess
        # spawn above; do not leave them sitting on disk once it has started.
        config_path.unlink(missing_ok=True)
    out = out_b.decode()
    err = err_b.decode()

    answer = out
    cost = None
    turns = None
    try:
        parsed = json.loads(out)
        answer = parsed.get("result", out)
        cost = parsed.get("total_cost_usd")
        turns = parsed.get("num_turns")
    except json.JSONDecodeError:
        pass  # not JSON — treat stdout as the answer

    if proc.returncode != 0:
        print(f"  [{tag}] claude exited {proc.returncode}: {err[:300]}", file=sys.stderr)
        return {"tag": tag, "question": question, "model": model, "ok": False, "queries": 0}

    answer_path.write_text(answer)
    stats = score_run(events_path, answer)
    return {
        "tag": tag, "question": question, "model": model, "ok": True,
        "cost": cost, "turns": turns, "answerPath": str(answer_path), "eventsPath": str(events_path),
        **stats,
    }


def score_run(events_path: Path, answer: str) -> dict:
    """Records both the agent's claim and the verified grounding for each query."""
    if not events_path.exists():
        return {"queries": 0}
    events = [json.loads(l) for l in events_path.read_text().split("\n") if l]
    if not events:
        return {"queries": 0}

    claimed = parse_cited(answer) or set()
    for e in events:
        e["used_downstream"] = e.get("label") in claimed

    verified = verify(events, answer)
    events_path.write_text("\n".join(json.dumps(e) for e in verified))

    return {
        "queries": len(verified),
        "claimed": sum(1 for e in verified if e["used_downstream"]),
        "grounded": sum(1 for e in verified if e["grounded"]),
        "unique": sum(1 for e in verified if e["uniquely_grounded"]),
    }


async def _cli() -> None:
    args = sys.argv[1:]
    question = args[0] if args and not args[0].startswith("--") else "Why did revenue drop in July 2026?"
    model = args[args.index("--model") + 1] if "--model" in args else None
    tag = args[args.index("--tag") + 1] if "--tag" in args else "agent"
    wide = "--wide" in args
    subagents = "--subagents" in args

    print(f"==> {tag}: \"{question}\"" + (f" [{model}]" if model else "") +
          (" [wide schema]" if wide else "") + (" [subagents]" if subagents else ""))
    r = await run_agent(question, model=model, tag=tag, wide=wide, subagents=subagents)
    if not r["ok"]:
        sys.exit(1)
    print(f"\n{Path(r['answerPath']).read_text().strip()}\n")
    print(f"==> {r['queries']} queries · claimed {r['claimed']} · grounded {r['grounded']} · uniquely grounded {r['unique']}")
    if r.get("cost") is not None:
        print(f"==> LLM cost ${r['cost']:.4f}, {r['turns']} turns")


def main() -> None:
    asyncio.run(_cli())


if __name__ == "__main__":
    main()
