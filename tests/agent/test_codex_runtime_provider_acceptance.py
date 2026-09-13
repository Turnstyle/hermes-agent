"""Codex app-server provider-acceptance result contract.

The transport owns the exact boundary: ``turn_start_accepted`` becomes true
only after the ``turn/start`` request returns a concrete response. The runtime
must preserve that verdict in its public turn-result mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import codex_runtime
from agent.transports.codex_app_server_session import TurnResult


class _Session:
    def __init__(self, result: TurnResult):
        self._result = result

    def run_turn(self, *, user_input):
        assert user_input == "deploy now"
        return self._result

    def close(self):
        return None


def _agent(session):
    return SimpleNamespace(
        compression_checkpoint_required=False,
        _codex_session=session,
        _pending_input_recovery_claim_id=None,
        _pending_input_recovery_resend_id=None,
        _interrupt_requested=False,
        _interrupt_message=None,
        clear_interrupt=lambda: None,
    )


def _run(agent):
    return codex_runtime.run_codex_app_server_turn(
        agent,
        user_message="deploy now",
        original_user_message="deploy now",
        messages=[{"role": "user", "content": "deploy now"}],
        effective_task_id="task",
    )


@pytest.fixture(autouse=True)
def _isolate_post_turn_work(monkeypatch):
    monkeypatch.setattr(codex_runtime, "_persist_projected_messages", lambda *_a, **_k: None)
    monkeypatch.setattr(codex_runtime, "_finish_codex_turn", lambda *_a, **_k: {})


def test_startup_exception_reports_not_accepted(monkeypatch):
    agent = _agent(None)
    monkeypatch.setattr(
        codex_runtime,
        "_ensure_codex_session",
        lambda _agent: (_ for _ in ()).throw(RuntimeError("startup failed")),
    )

    result = _run(agent)

    assert result["turn_start_accepted"] is False
    assert result["api_calls"] == 0
    assert result["completed"] is False


def test_turn_start_failure_reports_not_accepted(monkeypatch):
    turn = TurnResult(
        error="turn/start failed",
        turn_start_attempted=True,
        turn_start_accepted=False,
    )
    agent = _agent(_Session(turn))
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)

    result = _run(agent)

    assert result["turn_start_accepted"] is False
    assert result["api_calls"] == 1
    assert result["completed"] is False


def test_successful_turn_start_reports_accepted(monkeypatch):
    turn = TurnResult(
        final_text="done",
        turn_id="turn-1",
        thread_id="thread-1",
        turn_start_attempted=True,
        turn_start_accepted=True,
    )
    agent = _agent(_Session(turn))
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)

    result = _run(agent)

    assert result["turn_start_accepted"] is True
    assert result["api_calls"] == 1
    assert result["completed"] is True


def test_synthetic_truthy_recovery_attributes_do_not_activate_recovery(monkeypatch):
    turn = TurnResult(
        final_text="done",
        turn_id="turn-1",
        thread_id="thread-1",
        turn_start_attempted=True,
        turn_start_accepted=True,
    )
    agent = MagicMock()
    agent.compression_checkpoint_required = False
    agent._codex_session = _Session(turn)
    agent._interrupt_requested = False
    agent._interrupt_message = None
    # Leave both recovery attributes as MagicMock's synthetic truthy values.
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent: None)

    result = _run(agent)

    assert result["turn_start_accepted"] is True
    assert result["completed"] is True
