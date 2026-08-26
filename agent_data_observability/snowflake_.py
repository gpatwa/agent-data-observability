"""Snowflake connection + trace tagging.

WHY SNOWFLAKE IS DIFFERENT, AND SIMPLER:
On Postgres this repo injects trace context as a SQL comment and reconstructs
the plan tree by parsing the server log. Snowflake has a native slot for
exactly this — QUERY_TAG — which lands in ACCOUNT_USAGE.QUERY_HISTORY without
any log access at all. And QUERY_ATTRIBUTION_HISTORY reports actual credits
per query, so cost stops being MODELLED and becomes MEASURED.

Every dollar figure published by this project so far is Snowflake billing
rules applied to Postgres execution times. This module is how that claim gets
checked.

Credentials come from the environment only — never a file in the repo, never
an argument. Set them in your shell:

    export SNOWFLAKE_ACCOUNT='ORGNAME-ACCOUNTNAME'   # e.g. ABCDEFG-HI12345
    export SNOWFLAKE_USER='your_user'
    # then ONE of:
    export SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_key.p8   # recommended
    export SNOWFLAKE_PRIVATE_KEY_PASSPHRASE='...'              # if encrypted
    export SNOWFLAKE_PAT='...'                                 # programmatic access token
    export SNOWFLAKE_PASSWORD='...'                            # often blocked by MFA policy
    # optional:
    export SNOWFLAKE_WAREHOUSE=COMPUTE_WH SNOWFLAKE_ROLE=ACCOUNTADMIN
    export SNOWFLAKE_DATABASE=SNOWFLAKE_SAMPLE_DATA SNOWFLAKE_SCHEMA=TPCH_SF1
"""

from __future__ import annotations

import json
import os
import re

from . import env  # noqa: F401  (loads .env; shell env still wins)

_INTENT_STRIP_RE = re.compile(r"['\\\x00-\x1f]")
_WS_RE = re.compile(r"\s+")


def env_config() -> dict:
    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    username = os.environ.get("SNOWFLAKE_USER")
    if not account or not username:
        raise RuntimeError("SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER must be set. See docs/SNOWFLAKE.md.")

    base = {
        "account": account,
        "user": username,
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        "role": os.environ.get("SNOWFLAKE_ROLE") or None,
        "database": os.environ.get("SNOWFLAKE_DATABASE", "SNOWFLAKE_SAMPLE_DATA"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "TPCH_SF1"),
        "client_session_keep_alive": True,
    }

    if os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"):
        # Key-pair is the path Snowflake pushes for programmatic access; password
        # auth is commonly blocked outright by MFA policy on newer accounts.
        from cryptography.hazmat.primitives import serialization

        with open(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"], "rb") as f:
            pem = f.read()
        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
        key = serialization.load_pem_private_key(
            pem, password=passphrase.encode() if passphrase else None
        )
        der = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return {**base, "private_key": der}
    if os.environ.get("SNOWFLAKE_PAT"):
        return {**base, "authenticator": "PROGRAMMATIC_ACCESS_TOKEN", "token": os.environ["SNOWFLAKE_PAT"]}
    if os.environ.get("SNOWFLAKE_PASSWORD"):
        return {**base, "password": os.environ["SNOWFLAKE_PASSWORD"]}
    raise RuntimeError(
        "No credential found. Set SNOWFLAKE_PRIVATE_KEY_PATH (recommended), "
        "SNOWFLAKE_PAT, or SNOWFLAKE_PASSWORD. See docs/SNOWFLAKE.md."
    )


def connect(cfg: dict | None = None):
    import snowflake.connector

    return snowflake.connector.connect(**(cfg if cfg is not None else env_config()))


def execute(conn, sql_text: str, binds: list | None = None) -> dict:
    """Rows come back as dicts keyed by Snowflake's column names (uppercase
    for unquoted identifiers, e.g. row["C"]) — DictCursor, to match the shape
    callers already expect from the Node SDK's default row objects."""
    import snowflake.connector

    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute(sql_text, binds or None)
    rows = cur.fetchall()
    return {"rows": rows, "queryId": cur.sfqid}


def trace_tag(span: dict) -> str:
    """The trace context, as a QUERY_TAG rather than a SQL comment. Snowflake
    caps QUERY_TAG at 2000 characters, so intent is truncated rather than
    risking a rejected ALTER SESSION mid-run.

    The intent is model-authored free text and gets inlined into a SQL string
    literal by set_tag(), so strip the characters that could terminate or
    escape it. Structure is ours; only this field is untrusted.
    """
    safe_intent = _WS_RE.sub(" ", _INTENT_STRIP_RE.sub(" ", str(span.get("span_intent") or ""))).strip()[:300]
    tag = {
        "t": span.get("trace_id"),
        "s": span.get("span_id"),
        "p": span.get("parent_span_id"),
        "a": span.get("agent_id"),
        "c": span.get("speculation_class"),
        "i": safe_intent,
    }
    return json.dumps(tag, separators=(",", ":"))[:2000]


def set_tag(conn, span: dict) -> None:
    """ALTER SESSION DOES NOT ACCEPT BIND PARAMETERS. Passing a bind
    silently sets the tag to the literal placeholder string — no error, and
    every downstream trace lookup then finds nothing. This cost a whole agent
    run. The value must be inlined; trace_tag() has already removed quotes
    and backslashes, and the doubling here is belt and braces."""
    tag = trace_tag(span).replace("'", "''")
    execute(conn, f"ALTER SESSION SET QUERY_TAG = '{tag}'")


def parse_tag(raw: str):
    try:
        t = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return t if t and t.get("t") else None
