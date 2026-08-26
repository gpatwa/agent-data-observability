"""Preflight for the Databricks Genie pilot.

Deliberately exercises the SAME code path the agent uses (config -> start
conversation -> poll -> extract -> fetch result), because the last time a
preflight wrote its own shortcut it passed while production was broken.
"""

from __future__ import annotations

import json
import re
import sys

from .databricks import (
    config, extract_attachments, get_query_result, rows_from_result,
    start_conversation, wait_for_message,
)


def ok(s: str) -> str:
    return f"  ✓ {s}"


def bad(s: str) -> str:
    return f"  ✗ {s}"


def main() -> None:
    try:
        cfg = config()
    except Exception as e:
        print(bad(str(e)), file=sys.stderr)
        sys.exit(1)

    print("── DATABRICKS GENIE PREFLIGHT ─────────────────────────────────")
    print(f"  host      {cfg['host']}")
    print(f"  space     {cfg['spaceId']}")
    token = cfg["token"]
    print(f"  token     {token[:4]}…{token[-2:]} ({len(token)} chars)")

    args = sys.argv[1:]
    question = args[0] if args else "How many rows are in the largest table in this space?"
    print(f'\n  asking Genie: "{question}"')

    try:
        started = start_conversation(cfg, question)
        print(ok("start-conversation accepted"))
    except Exception as e:
        msg = str(e)
        print(bad(f"start-conversation failed: {msg}"), file=sys.stderr)
        if re.search(r"401|403", msg):
            print("    Check DATABRICKS_TOKEN is valid and has access to this Genie space.", file=sys.stderr)
        if re.search(r"404", msg):
            print("    Check DATABRICKS_GENIE_SPACE_ID — it is the id in the Genie space URL.", file=sys.stderr)
        sys.exit(1)

    conversation_id = started.get("conversation_id") or (started.get("conversation") or {}).get("id")
    message_id = started.get("message_id") or started.get("id") or (started.get("message") or {}).get("id")
    if not conversation_id or not message_id:
        print(bad(f"unexpected response shape: {json.dumps(started)[:240]}"), file=sys.stderr)
        print("    The adapter expects conversation_id and message_id. If the API has", file=sys.stderr)
        print("    changed shape, agent_data_observability/databricks.py is where to fix it.", file=sys.stderr)
        sys.exit(1)
    print(ok(f"conversation {conversation_id} · message {message_id}"))

    try:
        done = wait_for_message(cfg, conversation_id, message_id, timeout_s=180)
    except Exception as e:
        print(bad(str(e)), file=sys.stderr)
        sys.exit(1)
    status = done.get("status") or done.get("state")
    print(ok(f"status {status}") if status == "COMPLETED" else bad(f"status {status}"))

    att = extract_attachments(done)
    print(ok(f"Genie returned its generated SQL ({len(att['sql'])} chars) — sub-expression analysis is possible")
          if att["sql"]
          else bad("no SQL in attachments — only the narrative is available, so shape analysis will not work"))
    if att["text"]:
        narrative = re.sub(r"\s+", " ", att["text"])[:80]
        print(ok(f"narrative: {narrative}…"))

    if not att["attachmentId"]:
        print(bad("no query attachment — cannot fetch result rows, so value-grounding will not work"))
        sys.exit(1)
    try:
        rows = rows_from_result(get_query_result(cfg, conversation_id, message_id, att["attachmentId"]))
        print(ok(f"result rows readable ({len(rows)}) — value-grounding will work"))
        if rows:
            print(f"     first row: {json.dumps(rows[0])[:90]}")
    except Exception as e:
        print(bad(f"query-result fetch failed: {e}"))
        sys.exit(1)

    print('\n  Next: adobs-databricks-agent "<question>"')
    print("  Note: Genie authors the SQL, so there is no trace context to inject")
    print("  and no per-query credit attribution — see docs/DATABRICKS.md.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
