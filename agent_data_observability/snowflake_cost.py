"""Deferred cost join: turn tagged agent queries into MEASURED credits.

Two tiers, because Snowflake's views have very different latency:

    INFORMATION_SCHEMA.QUERY_HISTORY   near-real-time, no credit attribution.
                                        Used to confirm tagging worked at all.
    ACCOUNT_USAGE.QUERY_HISTORY        ~45 min latency, has QUERY_TAG.
    ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                                        hours of latency, has CREDITS_ATTRIBUTED_COMPUTE
                                        — the actual per-query cost.

So this cannot run in the same breath as the agent. Run the agent, wait, then
run this. It reports clearly which tier had data rather than silently
returning zeros.

    adobs-snowflake-cost [--hours 6] [--credit-price 3.00]
"""

from __future__ import annotations

import sys

from .snowflake_ import connect, execute, parse_tag


def _arg(name: str, default):
    args = sys.argv[1:]
    flag = f"--{name}"
    return args[args.index(flag) + 1] if flag in args else default


# 72h by default: ACCOUNT_USAGE lags hours, and in practice the read happens
# well after the run. A 6h default silently reported "no tagged queries" for a
# run whose data was present the whole time.
HOURS = float(_arg("hours", 72))
CREDIT_PRICE = float(_arg("credit-price", 3.0))  # Standard edition list


def usd(n: float) -> str:
    return f"${n:.4f}"


def main() -> None:
    conn = connect()

    # --- tier 1: did tagging work at all? -----------------------------------
    live = execute(conn, f"""
        select QUERY_ID, QUERY_TAG, TOTAL_ELAPSED_TIME, BYTES_SCANNED, WAREHOUSE_NAME
        from table(information_schema.query_history(
          end_time_range_start => dateadd('hour', -{HOURS}, current_timestamp()),
          -- RESULT_LIMIT defaults to 100 and the WHERE below filters AFTER the
          -- function returns, so tagged queries beyond the 100 most recent were
          -- invisible. This reported "0 tagged queries" for runs that had tagged
          -- correctly.
          result_limit => 10000))
        where QUERY_TAG like '%"t":%'
          and QUERY_TAG not like '%preflight%'
        order by START_TIME desc
        limit 500""")

    live_tagged = [r for r in live["rows"] if parse_tag(r["QUERY_TAG"])]
    print("── TAGGING (INFORMATION_SCHEMA, near real-time) ───────────────")
    print(f"  tagged agent queries in last {HOURS:g}h   {len(live_tagged)}")
    if not live_tagged:
        print("\n  No tagged queries found. Either the agent has not run yet, or")
        print('  QUERY_TAG was not set. Run: adobs-snowflake-agent "<question>"')
        conn.close()
        return
    traces = {parse_tag(r["QUERY_TAG"])["t"] for r in live_tagged}
    print(f"  distinct traces                        {len(traces)}")

    # --- tier 2: measured credits -------------------------------------------
    cost = execute(conn, f"""
        select q.QUERY_ID, q.QUERY_TAG, q.TOTAL_ELAPSED_TIME, q.BYTES_SCANNED,
               a.CREDITS_ATTRIBUTED_COMPUTE
        from snowflake.account_usage.query_history q
        left join snowflake.account_usage.query_attribution_history a
               on a.QUERY_ID = q.QUERY_ID
        where q.START_TIME >= dateadd('hour', -{HOURS}, current_timestamp())
          and q.QUERY_TAG like '%"t":%'
          and q.QUERY_TAG not like '%preflight%'
        order by q.START_TIME desc
        limit 1000""")

    print("\n── MEASURED COST (ACCOUNT_USAGE, lagging) ─────────────────────")
    if not cost["rows"]:
        print("  ACCOUNT_USAGE has no tagged rows yet.")
        print("  QUERY_HISTORY lags ~45 min and QUERY_ATTRIBUTION_HISTORY longer.")
        print("  Re-run this later — the agent run is already recorded.")
        conn.close()
        return

    with_credits = [r for r in cost["rows"] if r.get("CREDITS_ATTRIBUTED_COMPUTE") is not None]
    print(f"  tagged queries in ACCOUNT_USAGE         {len(cost['rows'])}")
    print(f"  with credit attribution                {len(with_credits)}")
    if not with_credits:
        print("\n  Credits not attributed yet — that view lags the furthest.")
        print("  Everything else below would be zero, so stopping here.")
        conn.close()
        return

    # --- per trace -----------------------------------------------------------
    by_trace: dict = {}
    for r in with_credits:
        t = parse_tag(r["QUERY_TAG"])
        if not t:
            continue
        e = by_trace.setdefault(t["t"], {"queries": 0, "credits": 0.0, "ms": 0.0, "bytes": 0.0})
        e["queries"] += 1
        e["credits"] += float(r.get("CREDITS_ATTRIBUTED_COMPUTE") or 0)
        e["ms"] += float(r.get("TOTAL_ELAPSED_TIME") or 0)
        e["bytes"] += float(r.get("BYTES_SCANNED") or 0)

    print("\n  trace             queries   credits      measured cost")
    tot_c = 0.0
    tot_q = 0
    for trace_id, e in by_trace.items():
        tot_c += e["credits"]
        tot_q += e["queries"]
        credits_str = f"{e['credits']:.6f}".rjust(9)
        print(f"  {trace_id[:16].ljust(17)} {str(e['queries']).rjust(7)}   "
              f"{credits_str}   {usd(e['credits'] * CREDIT_PRICE).rjust(12)}")

    print("\n── MEASURED vs MODELLED ───────────────────────────────────────")
    measured_per_task = (tot_c * CREDIT_PRICE) / max(len(by_trace), 1)
    print(f"  traces                                 {len(by_trace)}")
    print(f"  total queries                          {tot_q}")
    print(f"  total credits                          {tot_c:.6f}")

    # Refuse to publish a comparison built on nothing. Attribution arrives
    # gradually, so a partially-populated view yields a near-zero cost that
    # looks like a real measurement and would invite "correcting" a published
    # figure to zero. Silence is the correct output until the data is there.
    if tot_c <= 0:
        print("\n  Total attributed credits are zero, so there is no measurement yet.")
        print("  NOT printing a measured-vs-modelled ratio — it would read as 0.00x")
        print("  and invite correcting a real published figure to zero.")
        print("  Re-run once QUERY_ATTRIBUTION_HISTORY has caught up.")
        conn.close()
        return

    print(f"  MEASURED cost per resolved task        {usd(measured_per_task)}")
    print("  modelled equivalent (Postgres runs)    $0.073   <- published figure")
    print(f"\n  Ratio measured/modelled: {measured_per_task / 0.073:.2f}x")
    print("  A ratio far from 1.0 means the modelled numbers in the README and")
    print("  on the landing page need correcting — that is the point of this run.")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
