"""
Tests for query_log.py: status() must reflect whether Lakebase is actually
reachable, not just whether the LAKEBASE_* env vars are set. Before this,
fetch_recent() swallowed every failure and returned [], so a broken Lakebase
looked identical to "no queries yet" on the dashboard. record() already
disabled itself on failure; fetch_recent() must do the same, and both must
end up at the same "error" state so the dashboard can tell the two apart
from a genuinely empty, working query log.
"""

import logging

import pytest

import lakebase
import query_log


@pytest.fixture(autouse=True)
def reset_query_log_state(monkeypatch):
    """
    query_log tracks failure across calls via module-level globals, which
    would otherwise leak between tests. Reset them, and clear the Lakebase
    env vars so every test starts from "not configured" and opts in
    explicitly when it wants the "configured" branch.
    """
    monkeypatch.setattr(query_log, "_disabled", False)
    monkeypatch.setattr(query_log, "_warned", False)
    monkeypatch.setattr(query_log, "_last_error", None)
    monkeypatch.delenv("LAKEBASE_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_SCOPE", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_KEY", raising=False)


def _record_with(**overrides):
    kwargs = dict(
        tool_name="get_current_weather",
        location_query="Chicago",
        resolved=None,
        status="ok",
        verdict=None,
        summary="summary",
        duration_ms=10,
        requested_by=None,
    )
    kwargs.update(overrides)
    return kwargs


def test_status_is_off_when_unconfigured():
    result = query_log.status()
    assert result == {
        "state": "off",
        "message": (
            "The query log is optional and is not turned on for this deployment. "
            "Set LAKEBASE_URL, or LAKEBASE_SECRET_SCOPE and LAKEBASE_SECRET_KEY, to turn it on."
        ),
    }
    assert query_log.is_enabled() is False


def test_status_names_the_env_vars_to_turn_it_on():
    message = query_log.status()["message"]
    assert "LAKEBASE_URL" in message
    assert "LAKEBASE_SECRET_SCOPE" in message
    assert "LAKEBASE_SECRET_KEY" in message


def test_status_is_ok_when_configured_and_no_failure_seen(monkeypatch):
    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@host/db")
    assert query_log.status() == {
        "state": "ok",
        "message": "The query log is configured and working.",
    }
    assert query_log.is_enabled() is True


def test_fetch_recent_failure_flips_status_to_error(monkeypatch):
    """
    This is the bug from F14: before the fix, fetch_recent() caught every
    exception and returned [], so a database that does not exist looked
    identical to a working, empty query log.
    """
    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@unreachable-host/db")
    monkeypatch.setattr(lakebase, "run_query", _raise("connection refused"))

    rows = query_log.fetch_recent(limit=10)

    assert rows == []
    assert query_log.status()["state"] == "error"
    assert query_log.is_enabled() is False


def test_record_failure_also_flips_status_to_error(monkeypatch):
    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setattr(lakebase, "run_write", _raise("boom"))

    query_log.record(**_record_with())

    assert query_log.status()["state"] == "error"


def test_fetch_recent_short_circuits_once_disabled(monkeypatch):
    """
    After any failure, later calls must not retry a connection that is not
    going to work: fetch_recent() should return [] without ever calling
    lakebase.run_query again.
    """
    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setattr(lakebase, "run_write", _raise("boom"))
    query_log.record(**_record_with())
    assert query_log._disabled is True

    calls = []
    monkeypatch.setattr(lakebase, "run_query", lambda *a, **k: calls.append(1) or [])

    rows = query_log.fetch_recent()

    assert rows == []
    assert calls == []


def test_error_message_does_not_leak_connection_details(monkeypatch):
    secret_url = "postgresql://user:secret-password@internal-db-host:5432/weatherdb"
    monkeypatch.setenv("LAKEBASE_URL", secret_url)
    monkeypatch.setattr(
        lakebase,
        "run_query",
        _raise(f"could not connect to server: {secret_url}"),
    )

    query_log.fetch_recent()
    result = query_log.status()

    for leaked in ("secret-password", "internal-db-host", "Traceback", "postgresql://"):
        assert leaked not in result["message"]
    assert query_log._last_error is not None
    for leaked in ("secret-password", "internal-db-host", "postgresql://"):
        assert leaked not in query_log._last_error


def test_mark_failed_logs_warning_only_once(caplog):
    caplog.set_level(logging.WARNING, logger="weather-mcp.query_log")

    query_log._mark_failed("write")
    query_log._mark_failed("read")

    warnings = [r for r in caplog.records if r.name == "weather-mcp.query_log"]
    assert len(warnings) == 1


def test_record_and_fetch_recent_never_raise(monkeypatch):
    """The module's core contract: nothing here may ever raise into a caller."""
    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setattr(lakebase, "run_write", _raise("boom"))
    monkeypatch.setattr(lakebase, "run_query", _raise("boom"))

    query_log.record(**_record_with())
    assert query_log.fetch_recent() == []


def _raise(message):
    def _boom(*args, **kwargs):
        raise RuntimeError(message)

    return _boom
