"""The generic tool deadline (420s default) must never fire BELOW a tool's own deadline.

Foreground ``terminal`` owns its timeout (explicit arg, else ``terminal.timeout``, capped by
``TERMINAL_MAX_FOREGROUND_TIMEOUT``) and ``process(action="wait")`` owns its wait window. Before
this fix a foreground ``claude -p`` / ``codex exec`` / ``agy`` run asked for 600s — or run under a
profile with ``terminal.timeout: 1800`` — was abandoned and its process tree killed at 420s.
"""
import threading
import time

import pytest

from agent import tool_executor as te


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("agent.deadline._timeouts_section", lambda: {})
    monkeypatch.delenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    monkeypatch.setattr(te, "_terminal_foreground_cap_s", lambda: 600.0, raising=False)


GRACE = 60.0


def test_foreground_terminal_explicit_timeout_extends_generic_deadline():
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "claude -p x", "timeout": 600}) == 600 + GRACE


def test_foreground_terminal_uses_profile_default_timeout(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "1800")
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "codex exec x"}) == 1800 + GRACE


def test_short_terminal_keeps_generic_deadline():
    # Never lowered: a 30s command still gets the generic 420s guard.
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "ls", "timeout": 30}) == 420.0


def test_over_cap_foreground_is_promoted_so_not_extended():
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "x", "timeout": 5400}) == 420.0


def test_background_terminal_not_extended():
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "x", "background": True, "timeout": 600}) == 420.0


def test_process_wait_clamped_to_terminal_timeout(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "1800")
    assert te._effective_tool_deadline(420.0, "process", {"action": "wait", "timeout": 900}) == 900 + GRACE
    assert te._effective_tool_deadline(420.0, "process", {"action": "wait", "timeout": 9000}) == 1800 + GRACE
    assert te._effective_tool_deadline(420.0, "process", {"action": "poll"}) == 420.0


def test_disabled_generic_deadline_stays_disabled():
    assert te._effective_tool_deadline(None, "terminal", {"command": "x", "timeout": 600}) is None


def test_unrelated_tool_and_bad_args_untouched():
    assert te._effective_tool_deadline(420.0, "web_search", {"query": "x"}) == 420.0
    assert te._effective_tool_deadline(420.0, "terminal", "not-a-dict") == 420.0
    # Unparseable timeout falls back to the terminal default (180s): below 420, so unchanged.
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "x", "timeout": "nan"}) == 420.0


# Round 2 (Codex checker finding on bb339893d7): non-finite / oversized values must never disarm the guard.

@pytest.mark.parametrize("raw", [float("inf"), float("-inf"), float("nan"), "inf", "nan", 10 ** 400, True])
def test_non_finite_or_oversized_timeout_never_extends(raw):
    assert te._positive_seconds(raw) is None
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "x", "timeout": raw}) == 420.0
    assert te._effective_tool_deadline(420.0, "process", {"action": "wait", "timeout": raw}) == 420.0


def test_infinite_terminal_timeout_env_keeps_generic_deadline(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "inf")
    # process wait mirrors process_registry.wait: unparseable int -> 180s ceiling.
    assert te._effective_tool_deadline(420.0, "process", {"action": "wait"}) == 420.0
    assert te._effective_tool_deadline(420.0, "terminal", {"command": "x"}) == 420.0


def test_concurrent_batch_deadline_stays_finite_with_infinite_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "inf")
    deadline = 420.0
    for name, args in (("process", {"action": "wait"}), ("web_search", {"query": "x"})):
        deadline = te._effective_tool_deadline(deadline, name, args)
    assert deadline == 420.0


def test_terminal_default_is_scope_aware(monkeypatch):
    """Under gateway multiplexing the profile's terminal.timeout arrives via the terminal scope,
    not os.environ; the self-bound must read the same source the terminal tool reads."""
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    token = set_terminal_scope({"TERMINAL_TIMEOUT": "1800"})
    try:
        assert te._effective_tool_deadline(420.0, "terminal", {"command": "codex exec x"}) == 1800 + GRACE
    finally:
        reset_terminal_scope(token)


class _Agent:
    def __init__(self):
        self._tool_worker_threads = set()
        self._tool_worker_threads_lock = threading.Lock()
        self._interrupt_requested = False

    def _touch_activity(self, _label):
        pass


@pytest.fixture()
def _quiet_emit(monkeypatch):
    monkeypatch.setattr(te, "_SEQUENTIAL_INTERRUPT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda agent, **kw: None)


def test_sequential_runner_does_not_abandon_terminal_before_its_own_timeout(monkeypatch, _quiet_emit):
    """End to end through the sequential runner: generic deadline 1s, terminal asks for 3s,
    the tool takes 2s -> must complete, not time out."""
    monkeypatch.setattr("agent.deadline._timeouts_section", lambda: {"tools": {"sequential_call": 1}})
    monkeypatch.setattr(te, "_SELF_BOUNDED_TOOL_GRACE_S", 1.0, raising=False)
    captured = {}

    def fake_middleware(agent, *, authorization_gate=None, **kwargs):
        time.sleep(2.0)
        captured["ran"] = True
        return "ok"

    monkeypatch.setattr(te, "_run_agent_tool_execution_middleware", fake_middleware)
    result = te._run_sequential_tool_execution_middleware(
        _Agent(), function_name="terminal", function_args={"command": "sleep 2", "timeout": 3},
        effective_task_id="t", tool_call_id="c1", execute=lambda *a, **k: None,
    )
    assert result == "ok" and captured.get("ran")


def test_sequential_runner_still_times_out_unbounded_tool(monkeypatch, _quiet_emit):
    monkeypatch.setattr("agent.deadline._timeouts_section", lambda: {"tools": {"sequential_call": 1}})

    def fake_middleware(agent, *, authorization_gate=None, **kwargs):
        time.sleep(3.0)
        return "late"

    monkeypatch.setattr(te, "_run_agent_tool_execution_middleware", fake_middleware)
    result = te._run_sequential_tool_execution_middleware(
        _Agent(), function_name="web_search", function_args={"query": "x"},
        effective_task_id="t", tool_call_id="c2", execute=lambda *a, **k: None,
    )
    assert isinstance(result.result, te._ToolTimeoutResult)
    assert "timed out after 1.0s" in str(result.result)
