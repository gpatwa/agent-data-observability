"""Preflight for the DuckDB pilot. No account, no credentials, no network —
this either works or something is wrong with the local Python environment.

Round-trips a tagged query through duckdb_logs() within one live connection,
the same code path duckdb_mcp_server.py uses, so a broken tag format fails
here rather than silently in the middle of an agent run.
"""

from __future__ import annotations

import sys

from .context import new_span, new_trace, parse_context, serialize_context
from .duckdb_ import connect, db_path, execute, read_agenttrace_log


def ok(s: str) -> str:
    return f"  ✓ {s}"


def bad(s: str) -> str:
    return f"  ✗ {s}"


def main() -> None:
    print("── DUCKDB PREFLIGHT ────────────────────────────────────────────")
    print(f"  database  {db_path()}")

    try:
        conn = connect()
        print(ok("connected (installed/loaded the tpch extension)"))
    except Exception as e:
        print(bad(f"connect failed: {e}"), file=sys.stderr)
        sys.exit(1)

    try:
        n = execute(conn, "select count(*) as c from lineitem")["rows"][0]["c"]
        print(ok(f"TPC-H SF1 present — lineitem has {n:,} rows"))
    except Exception as e:
        print(bad(f"TPC-H schema not usable: {e}"), file=sys.stderr)
        sys.exit(1)

    # Tag round-trip through duckdb_logs(), same as the MCP server does.
    trace = new_trace(agent_id="preflight", model="preflight", task_intent="preflight check")
    span = new_span(trace, "round-trip check with ' quote and \\ backslash", "probe")
    tagged = f"{serialize_context(span)} select 1"
    try:
        execute(conn, tagged)
        hits = [r for r in read_agenttrace_log(conn) if span["span_id"] in r["message"]]
        if not hits:
            print(bad("tagged query did not appear in duckdb_logs()"))
        else:
            recovered = parse_context(hits[0]["message"])
            if recovered and recovered["span_id"] == span["span_id"]:
                print(ok("trace context round-trips through duckdb_logs() within this connection"))
            else:
                print(bad("logged text present but context did not parse back correctly"))
    except Exception as e:
        print(bad(f"tag round-trip failed: {e}"))

    print("\n  Note: duckdb_logs() does not survive past this connection closing —")
    print("  there is no separate warehouse-log recovery step here, unlike the")
    print("  Postgres and Snowflake pilots. See docs/DUCKDB.md.")
    print('\n  Next: adobs-duckdb-agent "<question>"')
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
