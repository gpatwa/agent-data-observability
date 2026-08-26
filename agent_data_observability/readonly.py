"""Read-only enforcement for agent-issued SQL.

Kept in its own side-effect-free module so it is testable without starting an
MCP server or opening a database connection.

HISTORY: the original guard was a regex matching only the START of the string
(`^\\s*(select|with)`). `select 1; drop table x` passed it, and the Postgres
driver executes multi-statement strings via the simple query protocol, so the
DROP ran. Verified against a canary table: it was dropped. That hole is
reachable by any prompt injection that reaches the agent.

This is the second line of defence, not the first. The first is a database
role with SELECT-only grants. An application-layer allowlist in front of a
read-write connection is not a security boundary.
"""

from __future__ import annotations

from typing import Optional

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


def read_only_refusal(sql: Optional[str]) -> Optional[str]:
    """Returns a refusal message, or None if the SQL is permitted."""
    if not isinstance(sql, str) or not sql.strip():
        return "Empty query."
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except Exception:
        return "Could not parse that SQL. Only a single read-only SELECT is permitted."
    if len(statements) != 1:
        return "Only one statement per call is permitted."
    if not isinstance(statements[0], exp.Select):
        return "Only SELECT queries are permitted."
    return None
