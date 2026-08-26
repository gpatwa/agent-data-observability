"""Loads .env from the repo root so credentials live in one gitignored file
instead of being exported by hand in every shell.

PRECEDENCE: a variable already set in the real environment always wins. That
matters — otherwise a stale .env would silently override the value you just
exported to test something, which is a miserable thing to debug.

No dependency: .env is a handful of KEY=value lines and this reads it as text.
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_PATH) -> dict:
    if not path.exists():
        return {"loaded": False, "keys": []}
    keys = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if not key:
            continue
        if key not in os.environ:  # shell wins
            os.environ[key] = val
            keys.append(key)
    return {"loaded": True, "keys": keys}


load_env()
