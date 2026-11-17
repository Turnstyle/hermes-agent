"""Closed, credential-safe diagnostics from cached MCP state; no live probes."""
from __future__ import annotations

import errno
from types import SimpleNamespace

import httpx
import pytest

from tools import mcp_tool as core, mcp_tool_discovery as discovery


SECRET = (
    "Authorization: Bearer HEADER_CANARY "
    "https://USER_CANARY:PASS_CANARY@example.invalid/mcp?token=QUERY_CANARY "
    'body={"secret":"BODY_CANARY"}'
)
REASONS = {"auth", "timeout", "invalid_endpoint", "connection", "missing_executable", "parked", "unknown"}
CONFIG = {"svc": {"url": "https://example.invalid/mcp"}}


@pytest.fixture(autouse=True)
def isolated_cached_state(monkeypatch):
    # Replace rather than clear process-wide ledgers, including any new reason cache.
    for name, value in list(vars(core).items()):
        if name.startswith("_server_") and isinstance(value, (dict, set)):
            monkeypatch.setattr(core, name, type(value)())
    monkeypatch.setattr(core, "_servers", {})
    monkeypatch.setattr(core, "_lazy_server_configs", {})
    monkeypatch.setattr(core, "_lazy_server_tool_names", {})
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: False)

    def forbidden(*args, **kwargs):
        pytest.fail("cached status must not connect, probe, or start an MCP loop")

    monkeypatch.setattr(discovery, "_connect_server", forbidden)
    monkeypatch.setattr(discovery, "probe_mcp_server_tools", forbidden)
    monkeypatch.setattr(discovery._loop, "_ensure_mcp_loop", forbidden)


def _http_error(status):
    request = httpx.Request("GET", "https://example.invalid/mcp", headers={"Authorization": "Bearer HEADER_CANARY"})
    response = httpx.Response(status, request=request, content=b"BODY_CANARY")
    return httpx.HTTPStatusError(SECRET, request=request, response=response)


def _nested_timeout():
    leaf = httpx.ReadTimeout(SECRET)
    wrapper = RuntimeError("transport wrapper")
    wrapper.__cause__ = leaf
    return ExceptionGroup("task group", [ExceptionGroup("nested", [wrapper])])


@pytest.mark.parametrize("factory, expected", [
    (lambda: _http_error(401), "auth"),
    (lambda: _http_error(403), "auth"),
    (_nested_timeout, "timeout"),
    (lambda: TimeoutError(SECRET), "timeout"),
    (lambda: httpx.InvalidURL(SECRET), "invalid_endpoint"),
    (lambda: FileNotFoundError(errno.ENOENT, SECRET, "EXECUTABLE_CANARY"), "missing_executable"),
    (lambda: ConnectionRefusedError(errno.ECONNREFUSED, SECRET), "connection"),
    (lambda: httpx.ConnectError(SECRET), "connection"),
    (lambda: RuntimeError(SECRET), "unknown"),
    (lambda: RuntimeError("401 Unauthorized timeout missing executable " + SECRET), "unknown"),
], ids=["401", "403", "nested-timeout", "timeout", "invalid-url", "missing-command", "refused", "connect", "unknown", "untrusted-text"])
def test_recorded_failure_exposes_only_a_closed_reason(factory, expected):
    discovery._note_connect_failure("svc", factory())
    row = discovery.get_mcp_status(CONFIG)[0]
    assert row["status"] == "failed"
    assert row["failure_reason"] == expected
    assert row["failure_reason"] in REASONS
    # Internal callers retain their existing error field; the reason itself is never raw text.
    assert "CANARY" not in row["failure_reason"]


def test_oauth_noninteractive_failure_is_auth():
    from tools.mcp_oauth import OAuthNonInteractiveError

    discovery._note_connect_failure("svc", OAuthNonInteractiveError(SECRET))
    assert discovery.get_mcp_status(CONFIG)[0]["failure_reason"] == "auth"


@pytest.mark.parametrize("auth_park, expected", [(None, "parked"), ((object(), "stamp"), "auth")])
def test_parked_state_without_connect_error_retains_its_diagnostic(auth_park, expected):
    core._servers["svc"] = SimpleNamespace(session=None, _park_reason=SECRET, _auth_park=auth_park)
    row = discovery.get_mcp_status(CONFIG)[0]
    assert row["status"] == "failed"
    assert row["failure_reason"] == expected
    assert row["needs_auth"] is (auth_park is not None)
    assert "CANARY" not in str({key: value for key, value in row.items() if key != "error"})


def test_success_clears_previous_failure_reason():
    discovery._note_connect_failure("svc", TimeoutError(SECRET))
    discovery._note_connect_success("svc")
    row = discovery.get_mcp_status(CONFIG)[0]
    assert row["status"] == "configured"
    assert "failure_reason" not in row


def test_runtime_exclusion_hides_cached_failure_reason():
    discovery._note_connect_failure("svc", TimeoutError(SECRET))
    row = discovery.get_mcp_status(CONFIG, include_runtime=False)[0]
    assert row["status"] == "configured"
    assert "failure_reason" not in row
    assert "error" not in row


def test_replaced_failure_does_not_reuse_an_older_category():
    discovery._note_connect_failure("svc", TimeoutError(SECRET))
    discovery._note_connect_failure("svc", RuntimeError(SECRET))
    assert discovery.get_mcp_status(CONFIG)[0]["failure_reason"] == "unknown"


def test_nested_exception_cycle_is_bounded():
    outer = RuntimeError("outer")
    inner = TimeoutError(SECRET)
    outer.__cause__ = inner
    inner.__context__ = outer
    discovery._note_connect_failure("svc", outer)
    assert discovery.get_mcp_status(CONFIG)[0]["failure_reason"] == "timeout"


def test_transport_retry_exhaustion_carries_reason_through_actual_park(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from tools.mcp_tool_server_run import _RetryBudget

    server = core.MCPServerTask("svc")
    server._ever_connected = True
    monkeypatch.setattr(core, "_MAX_RECONNECT_RETRIES", 0)
    monkeypatch.setattr(server, "_deregister_tools", lambda: None)
    monkeypatch.setattr(server, "_wait_for_reconnect_or_shutdown", AsyncMock(return_value="shutdown"))
    assert asyncio.run(server._on_transport_error(TimeoutError(SECRET), _RetryBudget())) is False
    core._servers["svc"] = server
    assert discovery.get_mcp_status(CONFIG)[0]["failure_reason"] == "timeout"

    server.session = object()
    server._mark_session_proven()
    row = discovery.get_mcp_status(CONFIG)[0]
    assert row["status"] == "connected"
    assert "failure_reason" not in row


def test_same_named_profiles_do_not_share_failure_categories(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    for home, error in ((tmp_path / "a", TimeoutError(SECRET)),
                        (tmp_path / "b", ConnectionRefusedError(SECRET))):
        home.mkdir()
        token = set_hermes_home_override(home)
        try:
            discovery._note_connect_failure("svc", error)
        finally:
            reset_hermes_home_override(token)
    for home, expected in ((tmp_path / "a", "timeout"), (tmp_path / "b", "connection")):
        token = set_hermes_home_override(home)
        try:
            assert discovery.get_mcp_status(CONFIG)[0]["failure_reason"] == expected
        finally:
            reset_hermes_home_override(token)
