"""Successful native-agent answers must not become automatic resend instructions.

Exercise the real conversation loop and response phases; the only fake is the wire client.
Its prompt_deliveries counter has the same contract as CursorACPClient. Non-Cursor rows
prove the ordinary retry paths remain live. No actual Cursor or network is used.
"""
from types import SimpleNamespace

import pytest

SHIM = "Connect timeout, please try again later."
SCRATCHPAD = "<REASONING_SCRATCHPAD>unfinished reasoning"


class _AnswerClient:
    def __init__(self, answer):
        self.answer = answer
        self.prompt_deliveries = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.prompt_deliveries += 1
        text = self.answer if self.prompt_deliveries == 1 else "Recovered answer."
        # Incomplete scratchpad has two automatic retry slots; keep returning it to
        # prove the pre-existing save-as-partial limit in the non-Cursor control.
        if self.answer == SCRATCHPAD:
            text = self.answer
        if not kwargs.get("stream"):
            return SimpleNamespace(id="fake-answer", model="default[]", usage=None,
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                    content=text, tool_calls=None, reasoning_content=None))])

        def chunks():
            for content, finish in ((text, None), (None, "stop")):
                yield SimpleNamespace(model="default[]", usage=None,
                    choices=[SimpleNamespace(index=0, finish_reason=finish, delta=SimpleNamespace(
                        content=content, tool_calls=None, reasoning_content=None, reasoning=None))])
        return iter(chunks())

    def close(self):
        pass


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("answer", [SCRATCHPAD, SHIM], ids=["scratchpad", "shim-text"])
@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "other-provider"])
def test_delivered_answer_replay_policy(tmp_path, monkeypatch, provider, answer, stream):
    from pathlib import Path
    from run_agent import AIAgent
    import agent.turn_recovery as recovery

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Timing only: leave the actual invalid-response retry decision untouched.
    monkeypatch.setattr(recovery, "interruptible_backoff_sleep", lambda *a, **kw: None)
    client = _AnswerClient(answer)
    shown, fallbacks = [], []
    agent = AIAgent(provider=provider, api_key="test-key",
        base_url="acp://cursor" if provider == "cursor-acp" else "https://example.invalid/v1",
        model="default[]", quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, enabled_toolsets=[], max_iterations=4,
        stream_delta_callback=shown.append if stream else None)
    agent._disable_streaming = not stream
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kw: client)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)

    def no_available_fallback(*a, **kw):
        fallbacks.append(1)
        return False

    monkeypatch.setattr(agent, "_try_activate_fallback", no_available_fallback)
    try:
        result = agent.run_conversation("ping")
    finally:
        agent.close()

    if provider == "cursor-acp":
        assert client.prompt_deliveries == 1, "a successful delivered Cursor answer was resent"
        assert fallbacks == [], "a successful delivered Cursor answer reached fallback"
        if answer == SCRATCHPAD:
            assert result["partial"] and not result.get("completed")
            assert "after 2 retries" not in result.get("error", "")
        else:
            assert result["final_response"] == SHIM
            assert not result.get("failed")
    elif answer == SCRATCHPAD:
        assert client.prompt_deliveries == 3, "other providers lost their two scratchpad retries"
        assert fallbacks == []
        assert result["partial"]
    else:
        assert client.prompt_deliveries == 2, "other providers lost the router-shim retry"
        assert fallbacks == [1], "the ordinary invalid-response fallback path was bypassed"
        assert result["final_response"] == "Recovered answer."
