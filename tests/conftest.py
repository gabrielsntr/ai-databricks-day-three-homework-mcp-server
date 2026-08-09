"""
Shared pytest fixtures for the weather MCP server test suite.

Puts mcp_server/ on sys.path so tests import the canonical modules
(weather_client, recommendations, weather_mcp_server, query_log) directly -
the same files the deployed app runs - rather than a separate copy. This
does not depend on dashboard/ existing at all, even though dashboard/
carries its own copy of the same modules.
"""

import socket
import sys
from pathlib import Path

import pytest

_MCP_SERVER_DIR = Path(__file__).resolve().parent.parent / "mcp_server"
if str(_MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER_DIR))


def _blocked_socket(*args, **kwargs):
    raise AssertionError(
        "A test tried to open a real network socket. Mock the HTTP boundary "
        "(weather_client._session.request) instead of letting a call reach the wire."
    )


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """
    Block real socket creation for every test in this suite.

    This is the suite's proof of no network: nothing here ever needs a live
    socket, so replacing socket.socket with something that raises turns any
    test that accidentally reaches the wire into an immediate, loud failure
    instead of a hang. Every test that talks to weather_client mocks the
    session's request method, which never gets far enough to open a socket.
    """
    monkeypatch.setattr(socket, "socket", _blocked_socket)
