"""Cleanup must not replay native effects or replace a provider outcome."""
import json
import sys
from pathlib import Path

import pytest

from agent.cursor_acp_client import CursorACPClient
from agent.cursor_acp_transport import CursorSession
from agent.error_classifier import classify_api_error

PEER = Path(__file__).parent / "fixtures" / "cursor_acp_lifecycle_peer.py"
ACK = {"cursor": {"allow_native_tools": True}}
pytestmark = pytest.mark.live_system_guard_bypass


@pytest.mark.parametrize("outcome", ["success", "timeout", "invalid"])
def test_cleanup_failure_preserves_outcome_and_is_terminal(tmp_path, monkeypatch, outcome):
    journal = tmp_path / "peer.jsonl"
    client = CursorACPClient(command=sys.executable,
                             args=[str(PEER), "tree-exit" if outcome == "success" else "tree-stall", str(journal)],
                             acp_cwd=str(tmp_path))
    close = CursorSession.close

    def failing_close(session):
        close(session)
        raise OSError("fixture cleanup failure")

    monkeypatch.setattr(CursorSession, "close", failing_close)
    extra_session = client._open(30)
    extra_session.request("initialize", {})
    try:
        kwargs = dict(model="invalid" if outcome == "invalid" else "default[]", messages=[], timeout=2, extra_body=ACK)
        if outcome == "success":
            assert client.chat.completions.create(**kwargs).cursor_stop_reason == "end_turn"
        else:
            with pytest.raises(ValueError if outcome == "invalid" else TimeoutError) as caught:
                client.chat.completions.create(**kwargs)
            verdict = classify_api_error(caught.value, provider="cursor-acp")
            assert not verdict.retryable and not verdict.should_fallback and not verdict.should_compress
        assert client.cleanup_error.code == "cursor_cleanup_failed"
        with pytest.raises(RuntimeError) as caught:
            client.chat.completions.create(**kwargs)
        verdict = classify_api_error(caught.value, provider="cursor-acp")
        assert not verdict.retryable and not verdict.should_fallback and not verdict.should_compress
        # A failed session must not short-circuit cleanup of the other owned sessions.
        sessions = list(client._sessions)
        with pytest.raises(RuntimeError) as caught:
            client.close()
        assert caught.value.code == "cursor_cleanup_failed"
        assert all(s.process.poll() is not None and not s._reader.is_alive() for s in sessions)
    finally:
        monkeypatch.setattr(CursorSession, "close", close)
        try:
            client.close()
        except RuntimeError:
            pass  # Direct shutdown reports the already-recorded cleanup failure.


@pytest.mark.parametrize("inject_failure", [False, True])
def test_completed_hermes_turn_is_never_replayed(tmp_path, monkeypatch, inject_failure):
    from run_agent import AIAgent
    import agent.turn_api_error as recovery

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    journal = tmp_path / "turn.jsonl"
    # Keep RED bounded while still running the real Hermes retry decision and call path.
    monkeypatch.setattr(recovery, "interruptible_backoff_sleep", lambda *a, **k: None)
    create = CursorACPClient._create
    calls = []

    def counted_create(self, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            from agent.cursor_acp_client import _unsupported
            raise _unsupported("native_tool_safety_unavailable", "fixture-replay-tripwire")
        return create(self, **kwargs)

    monkeypatch.setattr(CursorACPClient, "_create", counted_create)
    if inject_failure:
        close = CursorSession.close

        def failing_close(session):
            close(session)
            raise OSError("fixture cleanup failure after completion")

        monkeypatch.setattr(CursorSession, "close", failing_close)
    agent = AIAgent(provider="cursor-acp", api_key="cursor-acp", base_url="acp://cursor",
                    acp_command=sys.executable, acp_args=[str(PEER), "success-ignore-eof", str(journal)],
                    model="default[]", quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=[], max_iterations=4,
                    request_overrides={"extra_body": ACK})
    try:
        result = agent.run_conversation("ping")
        assert len(calls) == 1, "Hermes replayed a completed call after cleanup failed"
        assert result["final_response"] == "pong"
        receipts = [json.loads(line) for line in journal.read_text().splitlines()]
        assert sum(r.get("method") == "session/prompt" for r in receipts) == 1
    finally:
        agent.close()
