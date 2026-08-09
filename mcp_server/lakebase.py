"""
Lakebase (Databricks-managed Postgres) connection helper for the weather
MCP server.

Adapted from the reference repo's lakebase.py with one change: nothing
here runs at import time. The reference version builds a WorkspaceClient()
at module load, which fails outside a Databricks context. This version
resolves the connection string lazily, from LAKEBASE_URL (a plain Postgres
URL, handy for local dev) if set, otherwise from a Databricks secret named
by LAKEBASE_SECRET_SCOPE/LAKEBASE_SECRET_KEY. That keeps this module (and
anything that imports it, like query_log.py) importable with no Lakebase
or Databricks credentials present at all, which is what lets query_log
fail open instead of crashing the server at startup.
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


def is_configured() -> bool:
    """Whether enough configuration is present to attempt a Lakebase connection."""
    scope = os.environ.get("LAKEBASE_SECRET_SCOPE")
    key = os.environ.get("LAKEBASE_SECRET_KEY")
    return bool(os.environ.get("LAKEBASE_URL")) or bool(scope and key)


def _lakebase_url() -> str:
    """Resolve the Lakebase connection URL from LAKEBASE_URL or a Databricks secret."""
    url = os.environ.get("LAKEBASE_URL")
    if url:
        return url
    scope = os.environ.get("LAKEBASE_SECRET_SCOPE")
    key = os.environ.get("LAKEBASE_SECRET_KEY")
    if not (scope and key):
        raise RuntimeError("Lakebase is not configured: set LAKEBASE_URL or LAKEBASE_SECRET_SCOPE/LAKEBASE_SECRET_KEY.")
    from databricks.sdk import WorkspaceClient

    secret = WorkspaceClient().secrets.get_secret(scope=scope, key=key)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
