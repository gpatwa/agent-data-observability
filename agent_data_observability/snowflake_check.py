"""Preflight for the Snowflake pilot. Run this before anything else — it fails
with a specific, actionable message rather than letting an agent run die
halfway through on a permissions problem.
"""

from __future__ import annotations

import re
import sys

from .snowflake_ import connect, env_config, execute, set_tag


def ok(s: str) -> str:
    return f"  ✓ {s}"


def bad(s: str) -> str:
    return f"  ✗ {s}"


def main() -> None:
    try:
        cfg = env_config()
    except Exception as e:
        print(bad(str(e)), file=sys.stderr)
        sys.exit(1)

    auth_mode = ("PAT" if cfg.get("authenticator") == "PROGRAMMATIC_ACCESS_TOKEN"
                 else "key-pair" if "private_key" in cfg else "password")
    print("── SNOWFLAKE PREFLIGHT ────────────────────────────────────────")
    print(f"  account   {cfg['account']}")
    print(f"  user      {cfg['user']}")
    print(f"  auth      {auth_mode}")
    print(f"  warehouse {cfg['warehouse']}   role {cfg.get('role') or '(default)'}")

    try:
        conn = connect(cfg)
        print(ok("connected"))
    except Exception as e:
        msg = str(e)
        print(bad(f"connect failed: {msg}"), file=sys.stderr)
        if re.search(r"jwt|token is invalid", msg, re.IGNORECASE):
            print("    Key-pair auth: confirm ALTER USER ... SET RSA_PUBLIC_KEY was run,", file=sys.stderr)
            print("    and that SNOWFLAKE_USER matches the user it was set on.", file=sys.stderr)
        if re.search(r"password|mfa", msg, re.IGNORECASE):
            print("    Password auth is often blocked by MFA policy. Use key-pair or a PAT.", file=sys.stderr)
        sys.exit(1)

    def q(label, sql, on_rows):
        try:
            rows = execute(conn, sql)["rows"]
            on_rows(rows)
        except Exception as e:
            print(bad(f"{label}: {str(e).splitlines()[0]}"))

    q("context", "select current_account() a, current_region() r, current_version() v",
      lambda rows: print(ok(f"account {rows[0]['A']} · region {rows[0]['R']} · version {rows[0]['V']}")))

    q("sample data",
      "select count(*) c from snowflake_sample_data.information_schema.schemata where schema_name like 'TPCH%'",
      lambda rows: print(ok(f"SNOWFLAKE_SAMPLE_DATA present ({rows[0]['C']} TPCH schemas)") if rows[0]["C"] > 0
                          else bad("SNOWFLAKE_SAMPLE_DATA not visible to this role")))

    q("tpch scale", "select count(*) c from snowflake_sample_data.tpch_sf1.orders",
      lambda rows: print(ok(f"TPCH_SF1.orders readable — {int(rows[0]['C']):,} rows")))

    # QUERY_TAG round trip via the REAL set_tag() path. An earlier version
    # wrote its own literal here, so it passed while production silently
    # tagged every query "?" — the preflight must exercise the same code the
    # agent uses.
    probe = {
        "trace_id": "preflight-probe", "span_id": "p0", "parent_span_id": None,
        "agent_id": "preflight", "speculation_class": "probe",
        "span_intent": "round-trip check with ' quote and \\ backslash",
    }
    try:
        set_tag(conn, probe)
        rows = execute(conn,
            "select query_tag from table(information_schema.query_history(result_limit=>50)) "
            "where query_tag like '%preflight-probe%' limit 1")["rows"]
        if not rows:
            print(bad("QUERY_TAG not visible yet (history lags a few seconds — retry)"))
        elif rows[0]["QUERY_TAG"] == "?" or "preflight-probe" not in rows[0]["QUERY_TAG"]:
            print(bad(f"QUERY_TAG did not carry the trace: {rows[0]['QUERY_TAG']!r}"))
        else:
            print(ok("QUERY_TAG carries trace context into query history"))
    except Exception as e:
        print(bad(f"setTag failed: {str(e).splitlines()[0]}"))
    q("clear tag", "alter session unset query_tag", lambda rows: None)

    # ACCOUNT_USAGE is the one that actually carries credits.
    q("account_usage",
      "select count(*) c from snowflake.account_usage.query_history "
      "where start_time >= dateadd('hour',-24,current_timestamp())",
      lambda rows: print(ok(f"ACCOUNT_USAGE.QUERY_HISTORY readable ({rows[0]['C']} rows in 24h)")))

    q("attribution",
      "select count(*) c from snowflake.account_usage.query_attribution_history "
      "where start_time >= dateadd('day',-7,current_timestamp())",
      lambda rows: print(ok(f"QUERY_ATTRIBUTION_HISTORY readable ({rows[0]['C']} rows in 7d) — measured cost available")
                          if rows[0]["C"] > 0
                          else bad("QUERY_ATTRIBUTION_HISTORY empty — new account, or not enough activity yet")))

    print('\n  Next: adobs-snowflake-agent "<question>"')
    print("  Then (after ~1h): adobs-snowflake-cost")
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
