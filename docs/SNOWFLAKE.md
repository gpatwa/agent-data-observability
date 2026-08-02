# Snowflake pilot

Why this exists: **every dollar figure this project has published is modelled**, not measured — Snowflake billing rules applied to Postgres execution times. Snowflake reports actual credits per query, so this is how that claim gets checked.

It is also a better *validity* test than anything run so far. Every previous condition used a dataset with an anomaly I planted and questions I wrote. The TPC-H sample schema has no planted answer, so the agent has to do real analysis.

On Snowflake the tool is **simpler**, not bigger: trace context rides in the native `QUERY_TAG` instead of a SQL comment, so there is no log file, no log parser and no span reconstruction.

---

## 1. Credentials

Credentials live in a **gitignored `.env`** at the repo root, written for you by
the setup script below. Nothing needs exporting by hand, and nothing goes in the
repo or a chat window.

Precedence: a variable already set in your shell always beats `.env`, so you can
override one value for a single run without editing the file.

```
SNOWFLAKE_ACCOUNT=ORGNAME-ACCOUNTNAME
SNOWFLAKE_USER=YOUR_USER
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=ACCOUNTADMIN
SNOWFLAKE_PRIVATE_KEY_PATH=/Users/you/.ssh/snowflake_key.p8
```

Then **one** credential. Key-pair is recommended — Snowflake blocks password auth for programmatic access on accounts with MFA policy, which includes most new trials.

### Option A — key-pair (recommended)

```bash
./scripts/snowflake-keypair.sh YOUR_USER ORGNAME-ACCOUNTNAME
```

Generates the key if absent and prints the exact `ALTER USER` statement to paste
into a Snowsight worksheet, plus the `export` block for your shell. The private
key never leaves the machine and is never printed — only the public body is.

Find your user with `SELECT CURRENT_USER();` in Snowsight.

### Option B — programmatic access token

Snowsight → your user → Settings → Authentication → Programmatic access tokens.

```bash
export SNOWFLAKE_PAT='...'
```

> ⚠️ **On a fresh trial this is usually blocked.** The PAT panel reports
> *"Missing network policy — programmatic access tokens require an active
> network policy on the account or user."* You can create one, but a
> misconfigured network policy can lock you out of your own account, which is a
> poor trade for avoiding a two-command key-pair setup. **Prefer Option A.**

### Option C — password

Works only if no MFA policy applies to the user (a brand-new trial with
*0 registered MFA methods* qualifies). Supported by the adapter via
`SNOWFLAKE_PASSWORD`, but it breaks the moment MFA is enabled and keeps a
long-lived secret in your shell environment. Use it to unblock yourself, not as
the resting state.

---

## 2. Check the connection

```bash
npm run snowflake:check
```

Confirms auth, prints account/region/warehouse/edition, verifies the TPC-H sample database is present, and checks whether `ACCOUNT_USAGE` is readable by your role. Runs one trivial query, so it costs a few seconds of warehouse time.

## 3. Run an agent against it

```bash
node src/snowflake-agent.mjs "Which nations generate the most revenue, and has the mix shifted across the order years in the data?"
```

Same harness as the Postgres conditions — a real Claude Code agent whose only tool is a traced `run_sql`.

## 4. Read the measured cost (later, not immediately)

```bash
npm run snowflake:cost
```

**This will show nothing useful straight after the run, and that is expected.** Snowflake's views lag:

| View | Latency | Has |
|---|---|---|
| `INFORMATION_SCHEMA.QUERY_HISTORY` | near real-time | tags, timings — no credits |
| `ACCOUNT_USAGE.QUERY_HISTORY` | ~45 min | `QUERY_TAG` |
| `ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY` | hours | `CREDITS_ATTRIBUTED_COMPUTE` |

The script reports which tier had data rather than returning silent zeros, so re-running it later is the normal path.

---

## Trial-account notes

- **A trial is ~$400 of credits over 30 days.** These runs are small — a handful of TPC-H queries on an XS warehouse — but an idle warehouse still bills. `ALTER WAREHOUSE COMPUTE_WH SET AUTO_SUSPEND = 60;` if it is not already.
- **`SNOWFLAKE_SAMPLE_DATA` is a shared read-only database.** Querying it costs compute but no storage.
- `ACCOUNT_USAGE` requires `ACCOUNTADMIN` or explicitly granted access. Trials default the first user to `ACCOUNTADMIN`.
- Prefer `TPCH_SF1` (~6M lineitem rows) over `TPCH_SF100`+ on a trial. Larger scale factors will burn credits quickly.

## What this does not do

- No Databricks adapter. It was considered and skipped: query tagging is less mature and cost attribution goes through `system.billing.usage` at coarser granularity, so it would add an integration without adding a second measurement.
- Result values are still written to `out/*.jsonl` in plaintext for the answer-grounding check. On a real warehouse that is a data-exfiltration surface — see the "Not production software" section of the README.
