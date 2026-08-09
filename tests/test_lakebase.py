"""
Tests for lakebase.py: is_configured() and _lakebase_url() read their env
vars at call time, not at import time (F13). No test here ever opens a
real connection; conftest's block_network fixture would catch it if one did.
"""

import lakebase


def test_is_configured_reads_lakebase_url_set_after_import(monkeypatch):
    monkeypatch.delenv("LAKEBASE_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_SCOPE", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_KEY", raising=False)
    assert lakebase.is_configured() is False

    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@host/db")
    assert lakebase.is_configured() is True


def test_is_configured_reads_secret_scope_and_key_set_after_import(monkeypatch):
    """
    Before F13, _SCOPE/_KEY were snapshotted at import time, so a test's
    monkeypatch.setenv (or a load_dotenv() ordered after the import) here
    would change is_configured()'s answer for LAKEBASE_URL but not for the
    secret-scope path, since that path read the stale, import-time values.
    """
    monkeypatch.delenv("LAKEBASE_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_SCOPE", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_KEY", raising=False)
    assert lakebase.is_configured() is False

    monkeypatch.setenv("LAKEBASE_SECRET_SCOPE", "database")
    monkeypatch.setenv("LAKEBASE_SECRET_KEY", "lakebase-url")
    assert lakebase.is_configured() is True


def test_lakebase_url_reads_the_url_env_var_set_after_import(monkeypatch):
    monkeypatch.delenv("LAKEBASE_SECRET_SCOPE", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("LAKEBASE_URL", "postgresql://user:pass@host/db")
    assert lakebase._lakebase_url() == "postgresql://user:pass@host/db"


def test_lakebase_url_raises_a_plain_runtime_error_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LAKEBASE_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_SCOPE", raising=False)
    monkeypatch.delenv("LAKEBASE_SECRET_KEY", raising=False)
    import pytest

    with pytest.raises(RuntimeError):
        lakebase._lakebase_url()
