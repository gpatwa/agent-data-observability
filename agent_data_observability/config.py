"""Time compression for the simulation. The agent sleeps think_ms/DILATION of
real time; the assemble CLI scales elapsed wall-clock back up by DILATION
before applying warehouse billing. Query execution time is real and never
scaled."""

import os

DILATION = 100

PG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "55432")),
    "user": os.environ.get("PGUSER", "postgres"),
    "dbname": os.environ.get("PGDATABASE", "postgres"),
}
