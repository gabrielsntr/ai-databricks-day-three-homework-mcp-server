"""
Best-effort Lakebase logging of MCP tool calls, for the dashboard's
"recent agent queries" view.

record() is called from every tool in weather_mcp_server.py and must never
raise: a misconfigured or unreachable Lakebase should degrade the server to
"no query history", not take it down. The first failure logs one WARNING
and flips a module-level _disabled flag so every later call is a no-op
instead of retrying a connection that is not going to work.
"""

import logging
import threading

import lakebase

logger = logging.getLogger("weather-mcp.query_log")

_warned = False
_disabled = False
_lock = threading.Lock()

_INSERT_SQL = """
INSERT INTO weather_queries
    (tool_name, location_query, resolved_label, latitude, longitude,
     status, verdict, summary, duration_ms, requested_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_RECENT_SQL = """
SELECT tool_name, location_query, resolved_label, latitude, longitude,
       status, verdict, summary, duration_ms, requested_by, created_at
FROM weather_queries
ORDER BY created_at DESC
LIMIT %s
"""


def is_enabled() -> bool:
    """Whether Lakebase logging is currently active for this process."""
    return not _disabled and lakebase.is_configured()


def record(
    tool_name: str,
    *,
    location_query: str | None,
    resolved: dict | None,
    status: str,
    verdict: str | None,
    summary: str | None,
    duration_ms: int | None,
    requested_by: str | None,
) -> None:
    """
    Insert one row into weather_queries. Never raises: any failure is
    caught, logged once at WARNING, and disables further attempts for the
    rest of this process.

    Args:
        tool_name: Name of the MCP tool that was called.
        location_query: The raw location text/coordinates the caller passed in, if any.
        resolved: The resolved location dict (from weather_client.resolve_location), if any.
        status: The tool's result status, e.g. "ok", "not_found", "invalid_request", "error".
        verdict: A short verdict string when the tool produced one (e.g. umbrella verdict), else None.
        summary: A one-line human-readable summary of the result, for the dashboard table.
        duration_ms: How long the tool call took, in milliseconds.
        requested_by: The end user's identity (from x-forwarded-email/x-forwarded-user), if known.
    """
    global _warned, _disabled
    if _disabled or not lakebase.is_configured():
        return

    resolved = resolved or {}
    try:
        lakebase.run_write(
            _INSERT_SQL,
            (
                tool_name,
                location_query,
                resolved.get("label"),
                resolved.get("latitude"),
                resolved.get("longitude"),
                status,
                verdict,
                summary,
                duration_ms,
                requested_by,
            ),
        )
    except Exception:
        with _lock:
            if not _warned:
                logger.warning(
                    "Lakebase query logging is unavailable, disabling it for the rest of this process.",
                    exc_info=True,
                )
                _warned = True
            _disabled = True


def fetch_recent(limit: int = 50) -> list[dict]:
    """
    Fetch the most recent logged queries, most recent first, for the dashboard.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        A list of row dicts, or [] if logging is disabled, unconfigured, or the query fails.
    """
    if _disabled or not lakebase.is_configured():
        return []
    try:
        return lakebase.run_query(_RECENT_SQL, (limit,))
    except Exception:
        logger.warning("Failed to fetch recent queries from Lakebase.", exc_info=True)
        return []
