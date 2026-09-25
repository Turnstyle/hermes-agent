"""Delivered native-effect prompts are never automatically replayed by Hermes."""
import json
import sys
from pathlib import Path

import pytest

from agent.cursor_acp_client import CursorACPClient
from agent.error_classifier import classify_api_error

PEER = Path(__file__).parent / "fixtures" / "cursor_acp_lifecycle_peer.py"
ACK = {"cursor": {"allow_native_tools": True}}
pytestmark = pytest.mark.live_system_guard_bypass


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scenario", ["stall-prompt", "exit-mid", "rpc-error", "empty", "native-empty", "malformed-update", "stall-setup"])
def test_delivery_marks_failures_but_not_setup(tmp_path, scenario, stream):
    journal = tmp_path / "calls.jsonl"
    client = CursorACPClient(command=sys.executable, args=[str(PEER), scenario, str(journal)], acp_cwd=str(tmp_path))
    try:
        with pytest.raises(Exception) as caught:
            result = client.chat.completions.create(model="default[]", messages=[], timeout=2, stream=stream, extra_body=ACK)
            if stream:
                list(result)
        delivered = scenario != "stall-setup"
        assert bool(getattr(caught.value, "cursor_prompt_delivered", False)) == delivered
        assert bool(getattr(caught.value, "no_auto_replay", False)) == delivered
        verdict = classify_api_error(caught.value, provider="cursor-acp", approx_tokens=150000, num_messages=100)
        assert verdict.retryable is not delivered
        assert not verdict.should_fallback and not verdict.should_compress and not verdict.should_rotate_credential
        receipts = [json.loads(line) for line in journal.read_text().splitlines()]
        assert sum(row.get("method") == "session/prompt" for row in receipts) == int(delivered)
    finally:
        client.close()


@pytest.mark.parametrize("scenario", ["stall-prompt", "exit-mid", "rpc-error", "empty", "native-empty", "malformed-update"])
def test_hermes_delivers_only_once_and_allows_next_explicit_turn(tmp_path, monkeypatch, scenario):
    from run_agent import AIAgent
    import agent.turn_api_error as recovery
    import agent.cursor_acp_client as client_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_module, "_effective_timeout", lambda value: 2)
    monkeypatch.setattr(recovery, "interruptible_backoff_sleep", lambda *a, **k: None)
    journal = tmp_path / "turn.jsonl"
    calls = []
    create = CursorACPClient._create
    def counted_create(self, **kwargs):
        calls.append(1)
        if len(calls) > 1:  # Bound RED without replacing the first real failure.
            from agent.cursor_acp_client import _unsupported
            raise _unsupported("native_tool_safety_unavailable", "fixture-replay-tripwire")
        return create(self, **kwargs)
    monkeypatch.setattr(CursorACPClient, "_create", counted_create)
    agent = AIAgent(provider="cursor-acp", api_key="cursor-acp", base_url="acp://cursor",
                    acp_command=sys.executable, acp_args=[str(PEER), scenario, str(journal)],
                    model="default[]", quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=[], max_iterations=4,
                    request_overrides={"extra_body": ACK})
    def forbidden_fallback(*args, **kwargs):
        pytest.fail("A delivered prompt reached fallback activation")
    monkeypatch.setattr(agent, "_try_activate_fallback", forbidden_fallback)
    try:
        for turn in (1, 2):
            calls.clear()
            result = agent.run_conversation("ping")
            assert len(calls) == 1, "Hermes attempted automatic replay"
            assert result["failed"] and result["failure_retryable"] is False
            assert "not resent" in result["final_response"] and "/retry" in result["final_response"]
            assert "retrying won't help" not in result["final_response"]
            receipts = [json.loads(line) for line in journal.read_text().splitlines()]
            assert sum(row.get("method") == "session/prompt" for row in receipts) == turn
    finally:
        agent.close()
