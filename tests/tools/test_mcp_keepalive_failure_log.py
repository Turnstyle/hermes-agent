"""Keepalive failure logs name the failing phase, the numeric JSON-RPC code and the numeric HTTP status.

mcp >= 2.0 turns a non-2xx whose body is not a JSON-RPC error into the opaque
``-32603 Server returned an error response``. The log line must say which probe (ping / list_tools)
failed and with which numbers, and nothing else from the exchange: no body, header, URL, session id
or token. The ping fallback rules stay exactly as they were.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_errors import _keepalive_failure_fields

SECRET_URL = "https://api.example.test/v2/mcp/apps/voice-monitor/mcp?token=SECRET-URL-TOKEN"
SECRET_BODY = "SECRET-BODY-bearer-abc123"
SECRET_SESSION = "SECRET-SESSION-ID-9f8e7d"


class _RpcError(Exception):
    """Stand-in carrying the structural ``.error.code`` the real MCPError has."""

    def __init__(self, code, message="Server returned an error response"):
        super().__init__(code, message, None)
        self.error = SimpleNamespace(code=code, message=message, data={"session": SECRET_SESSION})


def _caps(tools=True):
    return SimpleNamespace(capabilities=SimpleNamespace(
        tools=SimpleNamespace() if tools else None, prompts=None, resources=None))


def _task_with(send_ping, list_tools=None):
    task = MCPServerTask("telnyx_voice")
    task._config = {"url": "https://api.example.test/v2/mcp/apps/voice-monitor/mcp"}  # remote: keepalive on
    task.initialize_result = _caps()
    task.session = SimpleNamespace(send_ping=send_ping, list_tools=list_tools or AsyncMock())
    return task


def _poisoned_rejection(status):
    """What the httpx response hook records: it holds the URL and body head, which must never be logged."""
    return {"status": status, "method": "POST", "url": SECRET_URL, "body": SECRET_BODY}


# ── _keepalive_failure_fields (pure) ─────────────────────────────────────────

class TestKeepaliveFailureFields:
    def test_phase_code_and_status(self):
        out = _keepalive_failure_fields(_RpcError(-32603), "ping", _poisoned_rejection(401))
        assert out == " [phase=ping jsonrpc_code=-32603 http_status=401]"

    def test_status_from_http_status_error_response(self):
        exc = RuntimeError("boom")
        exc.response = SimpleNamespace(status_code=503, text=SECRET_BODY, headers={"authorization": "x"})
        assert _keepalive_failure_fields(exc, "list_tools") == " [phase=list_tools http_status=503]"

    def test_no_numbers_available_omits_them(self):
        assert _keepalive_failure_fields(ConnectionError("x"), "ping") == " [phase=ping]"

    def test_unknown_or_unsafe_phase_is_a_fixed_word(self):
        assert _keepalive_failure_fields(ConnectionError("x"), None) == " [phase=unknown]"
        assert _keepalive_failure_fields(ConnectionError("x"), f"ping {SECRET_URL}") == " [phase=unknown]"

    def test_non_numeric_code_and_status_are_dropped(self):
        exc = SimpleNamespace(error=SimpleNamespace(code=SECRET_BODY),
                              response=SimpleNamespace(status_code=True))
        rejection = {"status": SECRET_BODY, "method": "POST", "url": SECRET_URL, "body": SECRET_BODY}
        assert _keepalive_failure_fields(exc, "ping", rejection) == " [phase=ping]"

    def test_get_rejection_is_not_attributed_to_a_post_probe(self):
        rejection = {"status": 405, "method": "GET", "url": SECRET_URL, "body": ""}
        assert _keepalive_failure_fields(_RpcError(-32603), "ping", rejection) == \
            " [phase=ping jsonrpc_code=-32603]"

    def test_never_contains_secret_material(self):
        out = _keepalive_failure_fields(_RpcError(-32603), "ping", _poisoned_rejection(401))
        for secret in (SECRET_URL, SECRET_BODY, SECRET_SESSION, "voice-monitor", "token="):
            assert secret not in out


# ── the real log lines ───────────────────────────────────────────────────────

async def _drive_one_failing_keepalive(task):
    """Run _wait_for_lifecycle_event through one keepalive timeout; the failing probe ends it."""
    import tools.mcp_tool as mcp_mod

    async def fake_wait(tasks, timeout=None, return_when=None):
        return set(), set(tasks)  # keepalive interval elapsed, nothing else fired

    orig = mcp_mod.asyncio.wait
    mcp_mod.asyncio.wait = fake_wait
    try:
        return await task._wait_for_lifecycle_event()
    finally:
        mcp_mod.asyncio.wait = orig


def _keepalive_messages(caplog):
    return [r.getMessage() for r in caplog.records if "keepalive failed, triggering reconnect" in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.no_isolate
async def test_opaque_ping_error_logs_phase_code_and_status(caplog):
    task = _task_with(AsyncMock())

    async def failing_ping():
        # The response hook runs inside the SDK call, after the probe started.
        task._http_rejection.update(_poisoned_rejection(401))
        raise _RpcError(-32603)

    task.session.send_ping = failing_ping

    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        reason = await _drive_one_failing_keepalive(task)

    assert reason == "reconnect"
    messages = _keepalive_messages(caplog)
    assert len(messages) == 1
    assert "[phase=ping jsonrpc_code=-32603 http_status=401]" in messages[0]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (SECRET_URL, SECRET_BODY, SECRET_SESSION, "voice-monitor"):
        assert secret not in logged


@pytest.mark.asyncio
async def test_generic_ping_error_is_not_hidden_or_fallen_back():
    """A non -32601 JSON-RPC error still propagates; list_tools is NOT used and the latch stays off."""
    task = _task_with(AsyncMock(side_effect=_RpcError(-32603)))

    with pytest.raises(_RpcError):
        await task._keepalive_probe()

    task.session.list_tools.assert_not_called()
    assert task._ping_unsupported is False
    assert task._keepalive_phase == "ping"


@pytest.mark.asyncio
async def test_method_not_found_fallback_unchanged_and_list_tools_failure_names_its_phase():
    task = _task_with(AsyncMock(side_effect=_RpcError(-32601, "Method not found")),
                      AsyncMock(side_effect=_RpcError(-32000, "denied")))

    with pytest.raises(_RpcError) as info:
        await task._keepalive_probe()

    assert task._ping_unsupported is True
    assert task._keepalive_phase == "list_tools"
    assert _keepalive_failure_fields(info.value, task._keepalive_phase, task._http_rejection) == \
        " [phase=list_tools jsonrpc_code=-32000]"


@pytest.mark.asyncio
async def test_ping_timeout_then_list_tools_failure_reports_the_ping_failure():
    task = _task_with(AsyncMock(side_effect=asyncio.TimeoutError()),
                      AsyncMock(side_effect=asyncio.TimeoutError()))

    with pytest.raises(asyncio.TimeoutError):
        await task._keepalive_probe()

    assert task._ping_unsupported is False
    assert task._keepalive_phase == "ping"


@pytest.mark.asyncio
async def test_each_probe_clears_the_previous_http_status():
    task = _task_with(AsyncMock())
    task._http_rejection.update(_poisoned_rejection(401))  # left over from an older request

    await task._keepalive_probe()

    assert task._http_rejection == {}


@pytest.mark.asyncio
@pytest.mark.no_isolate
async def test_health_check_failure_log_carries_the_same_safe_fields(caplog):
    task = _task_with(AsyncMock(side_effect=_RpcError(-32603)))
    task.mark_suspect("probe")

    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        assert await task.ensure_healthy() is False

    messages = [r.getMessage() for r in caplog.records if "failed health check" in r.getMessage()]
    assert messages and "[phase=ping jsonrpc_code=-32603]" in messages[0]


@pytest.mark.usefixtures("require_mcp_2_sdk")
def test_real_sdk_error_code_is_read():
    exceptions = pytest.importorskip("mcp.shared.exceptions")
    err = exceptions.MCPError(code=-32603, message="Server returned an error response")
    assert _keepalive_failure_fields(err, "ping", _poisoned_rejection(401)) == \
        " [phase=ping jsonrpc_code=-32603 http_status=401]"
