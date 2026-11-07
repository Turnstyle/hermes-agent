"""R8: every returned-answer recovery must respect native prompt custody.

Real loop/transport, fake wire only; no Cursor process, credential or network call.
The table owns sibling recovery branches; non-Cursor rows prove those branches remain live.
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tests.agent.test_cursor_acp_response_replay import _AnswerClient


class _RecoveryClient(_AnswerClient):
    HERMES_SUPPORTS_STREAMING = True

    def __init__(self, scenario):
        super().__init__("")
        self.scenario = scenario

    def create(self, **kwargs):
        self.prompt_deliveries += 1
        first = self.prompt_deliveries == 1
        text, finish, tools = "Recovered answer.", "stop", None
        if first:
            text = {
                "think": "<think>working</think>", "empty": "", "empty-fallback": "",
                "prefill-exhausted": "<think>working</think>", "post-tool-empty": "",
                "invalid": "", "length": "Visible partial answer", "length-tool": "",
                "filter": "Cannot answer.", "filter-stall": "Visible partial answer",
                "stall": "Let me check the files now.", "ack": "I will do that.",
                "degenerate": "Done", "dropped": "Preparing the call.",
                "verify": "Delivered answer.", "preverify": "Delivered answer.",
                "kanban": "Delivered answer.", "json": "", "postcheck-error": "Delivered answer.",
                "valid-tool": "", "tool-outer-error": "", "invalid-tool-name": "",
            }[self.scenario]
            if self.scenario in {"length", "length-tool", "filter-stall"}:
                finish = "length"
            elif self.scenario == "filter":
                finish = "content_filter"
            elif self.scenario in {"dropped", "json", "valid-tool", "tool-outer-error", "invalid-tool-name"}:
                finish = "tool_calls"
            if self.scenario in {"length-tool", "json", "valid-tool", "tool-outer-error", "invalid-tool-name"}:
                tools = [NS(id="call-1", type="function", index=0, function=NS(
                    name="nonexistent_r8_tool" if self.scenario == "invalid-tool-name" else "terminal",
                    arguments='{"command": invalid}' if self.scenario == "json" else '{}'))]
        if first and self.scenario == "invalid":
            return NS(id="invalid", choices=[], usage=None)
        if not kwargs.get("stream"):
            result = NS(id="answer", model="default[]", usage=None,
                choices=[NS(finish_reason=finish, message=NS(
                    content=text, tool_calls=tools, reasoning_content=None))])
            if first and self.scenario == "filter-stall":
                result._content_filter_terminated = True
            return result

        def chunks():
            for content, calls, reason in ((text, tools, None), (None, None, finish)):
                yield NS(model="default[]", usage=None,
                    choices=[NS(index=0, finish_reason=reason, delta=NS(
                        content=content, tool_calls=calls, reasoning_content=None, reasoning=None))])
        return iter(chunks())


def _make_agent(tmp_path, monkeypatch, provider, stream, scenario):
    from run_agent import AIAgent
    import agent.turn_recovery as recovery
    import agent.turn_empty_response as empty

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(recovery, "interruptible_backoff_sleep", lambda *a, **kw: None)
    monkeypatch.setattr(empty, "interruptible_backoff_sleep", lambda *a, **kw: None)
    agent = AIAgent(provider=provider, api_key="test-key", model="default[]",
        base_url="acp://cursor" if provider == "cursor-acp" else "https://example.invalid/v1",
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, enabled_toolsets=[], max_iterations=6,
        stream_delta_callback=(lambda text: None) if stream else None)
    agent._disable_streaming = not stream
    client = _RecoveryClient(scenario)
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kw: client)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)
    fallbacks = []
    def fallback(*a, **kw):
        fallbacks.append(1)
        return False
    monkeypatch.setattr(agent, "_try_activate_fallback", fallback)
    return agent, client, fallbacks


SCENARIOS = ["think", "empty", "empty-fallback", "prefill-exhausted", "post-tool-empty",
    "invalid", "length", "length-tool", "filter", "filter-stall", "stall", "ack",
    "degenerate", "dropped", "verify", "preverify", "kanban", "json", "postcheck-error", "invalid-tool-name"]


@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "control"])
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_returned_answer_ends_without_automatic_resend(tmp_path, monkeypatch, provider, stream, scenario):
    import agent.turn_stop_gates as gates
    import agent.turn_response_check as checks
    import agent.agent_runtime_helpers as runtime
    from agent.message_metadata import append_message

    # An invalid non-stream envelope and content-filter stub have no stream analogue;
    # other rows exercise the real streaming assembler, including thinking-only text.
    if stream and scenario in {"invalid", "filter-stall"}:
        pytest.skip("raw nonstream envelope/stub; streaming counterpart covered by length rows")
    agent, client, fallbacks = _make_agent(tmp_path, monkeypatch, provider, stream, scenario)
    if scenario in {"stall", "ack", "degenerate", "json", "length-tool"}:
        agent.valid_tool_names = {"terminal"}
    if scenario == "stall":
        monkeypatch.setattr(runtime, "trailing_continue_intent", lambda text: client.prompt_deliveries == 1)
    if scenario == "ack":
        monkeypatch.setattr(runtime, "intent_ack_continuation_mode", lambda a: "always")
        monkeypatch.setattr(agent, "_looks_like_codex_intermediate_ack", lambda **kw: client.prompt_deliveries == 1)
    if scenario == "degenerate":
        monkeypatch.setattr(runtime, "intent_ack_continuation_mode", lambda a: "always")
        monkeypatch.setattr(runtime, "tool_results_this_turn", lambda messages: 1)
        monkeypatch.setattr(runtime, "looks_like_degenerate_final", lambda *a, **kw: client.prompt_deliveries == 1)
    if scenario in {"verify", "preverify", "kanban"}:
        target = {"verify": "_verify_on_stop_nudge", "preverify": "_pre_verify_nudge", "kanban": "_kanban_stop_nudge"}[scenario]
        monkeypatch.setattr(gates, target, lambda *a, **kw: "Check the result." if client.prompt_deliveries == 1 else None)
    if scenario == "postcheck-error":
        import agent.turn_recovery as recovery
        validate = recovery.validate_response_shape
        def fail_first(*args):
            if client.prompt_deliveries == 1:
                raise ValueError("post-call decoder failure")
            return validate(*args)
        monkeypatch.setattr(recovery, "validate_response_shape", fail_first)
    if scenario in {"prefill-exhausted", "empty-fallback", "post-tool-empty"}:
        # Set counters only AFTER turn initialization, at the returned-response boundary.
        original_check = checks.check_api_response
        import agent.conversation_loop as loop
        from functools import wraps
        @wraps(original_check)
        def prepared_check(*args, **kw):
            if scenario == "prefill-exhausted":
                agent._thinking_prefill_retries = 2
            elif scenario == "empty-fallback":
                agent._empty_content_retries = 99
                agent._fallback_chain = [{"provider": "custom", "model": "never-called"}]
            else:
                append_message(kw["messages"], {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "prior", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
                append_message(kw["messages"], {"role": "tool", "tool_call_id": "prior", "content": "New tool result"})
            return original_check(*args, **kw)
        monkeypatch.setattr(loop, "check_api_response", prepared_check)
    try:
        result = agent.run_conversation("Please work on this request.")
    finally:
        agent.close()
    if provider == "cursor-acp":
        assert client.prompt_deliveries == 1, f"{scenario}: delivered Cursor prompt was resent"
        assert fallbacks == [], f"{scenario}: delivered Cursor prompt activated fallback"
        assert result["final_response"]
        assert any(m.get("role") == "assistant" for m in result["messages"]), "terminal answer missing from transcript"
        if scenario in {"think", "empty", "empty-fallback", "prefill-exhausted", "post-tool-empty", "invalid", "json", "postcheck-error", "invalid-tool-name"}:
            assert not result.get("completed"), "unusable Cursor output was reported complete"
            assert "not resent automatically" in result.get("error", "")
        if scenario in {"length", "filter-stall"}:
            assert "Visible partial answer" in result["final_response"]
            assert result.get("partial")
    elif scenario in {"filter", "empty-fallback", "postcheck-error"}:
        assert client.prompt_deliveries == 1
        assert fallbacks == [1], "non-Cursor exhausted/refusal recovery lost fallback activation"
    else:
        assert client.prompt_deliveries >= 2, f"{scenario}: non-Cursor recovery no longer continues"


@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "control"])
def test_iteration_summary_does_not_resend_native_turn(tmp_path, monkeypatch, provider):
    from agent.chat_completion_helpers import handle_max_iterations
    agent, client, fallbacks = _make_agent(tmp_path, monkeypatch, provider, False, "empty")
    monkeypatch.setattr(agent, "_ensure_primary_openai_client", lambda **kw: client)
    messages = [{"role": "user", "content": "Work"}, {"role": "assistant", "content": "Partial work"}]
    try:
        result = handle_max_iterations(agent, messages, 1)
    finally:
        agent.close()
    if provider == "cursor-acp":
        assert client.prompt_deliveries == 0, "iteration-limit nudge resent the native turn"
        assert "not resent automatically" in result
        assert len(messages) == 2, "summary scaffold polluted transcript"
    else:
        assert client.prompt_deliveries == 2, "ordinary empty-summary retry was lost"
        assert result == "Recovered answer."
    assert not fallbacks


@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "control"])
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_complete_tool_result_authorizes_followup_and_finalization(tmp_path, monkeypatch, provider, stream):
    import agent.conversation_loop as loop
    from agent.message_metadata import append_message
    agent, client, fallbacks = _make_agent(tmp_path, monkeypatch, provider, stream, "valid-tool")
    agent.valid_tool_names = {"terminal"}
    executed, finalized = [], []
    def execute(message, messages, *args):
        executed.append(message.tool_calls[0].function.name)
        append_message(messages, {"role": "tool", "name": "terminal", "tool_call_id": "call-1", "content": "Actual new tool result"})
    monkeypatch.setattr(agent, "_execute_tool_calls", execute)
    original = loop.finalize_turn
    from functools import wraps
    @wraps(original)
    def finalize(*args, **kwargs):
        finalized.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(loop, "finalize_turn", finalize)
    try:
        result = agent.run_conversation("Work using a tool.")
    finally:
        agent.close()
    assert executed == ["terminal"]
    assert client.prompt_deliveries == 2, "new actual tool result must authorize the next prompt"
    assert not fallbacks
    assert result["final_response"] == "Recovered answer."
    assert result["completed"]
    assert finalized == [1], "post-delivery choke bypassed normal turn cleanup/hooks"
    assert agent.session_api_calls == 2, "usage-less completed attempts must each be counted once"


@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "control"])
def test_ordinary_native_final_keeps_finalizer_and_projection(tmp_path, monkeypatch, provider):
    import agent.conversation_loop as loop
    import agent.provider_projection as projection
    agent, client, fallbacks = _make_agent(tmp_path, monkeypatch, provider, False, "verify")
    finalized, projected = [], []
    original_finalize, original_project = loop.finalize_turn, projection.splice_provider_projection
    from functools import wraps
    @wraps(original_finalize)
    def finalize(*args, **kwargs):
        finalized.append(1)
        return original_finalize(*args, **kwargs)
    def project(*args):
        projected.append(1)
        return original_project(*args)
    monkeypatch.setattr(loop, "finalize_turn", finalize)
    monkeypatch.setattr(projection, "splice_provider_projection", project)
    # The old intake imports the projection function eagerly.
    import agent.turn_response_intake as intake
    monkeypatch.setattr(intake, "splice_provider_projection", project)
    try:
        result = agent.run_conversation("Give an ordinary answer.")
    finally:
        agent.close()
    assert finalized == [1]
    assert projected == [1]
    assert agent.session_api_calls == 1
    assert client.prompt_deliveries == 1 and not fallbacks
    assert result["final_response"] == "Delivered answer."
    assert result["completed"]


@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "control"])
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_local_tool_processing_error_before_result_never_resends_native_prompt(tmp_path, monkeypatch, provider, stream):
    agent, client, fallbacks = _make_agent(tmp_path, monkeypatch, provider, stream, "tool-outer-error")
    agent.valid_tool_names = {"terminal"}
    def fail_before_tool(*args):
        raise ConnectionError("local tool processing failed before execution")
    monkeypatch.setattr(agent, "_deduplicate_tool_calls", fail_before_tool)
    try:
        result = agent.run_conversation("Work on this.")
    finally:
        agent.close()
    assert not fallbacks
    if provider == "cursor-acp":
        assert client.prompt_deliveries == 1, "outer-loop recovery resent a delivered prompt with no new tool result"
        assert not result.get("completed")
        assert "not resent automatically" in result.get("error", "")
    else:
        assert client.prompt_deliveries == 2
        assert result["final_response"] == "Recovered answer."


@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("nonstream", [False, True], ids=["stream", "nonstream"])
def test_real_cursor_client_thinking_only_rpc_is_delivered_exactly_once(tmp_path, monkeypatch, nonstream):
    # Exercise the actual native client and subprocess JSON-RPC, not only an
    # OpenAI-shaped response stub. The peer is local Python, never real Cursor.
    import tests.agent.test_cursor_acp_watchdog_replay as watchdog
    monkeypatch.setattr(watchdog, "PEER", Path(__file__).parent / "fixtures" / "cursor_acp_thinking_peer.py")
    run = watchdog._run_turn(tmp_path, monkeypatch, "thinking-only", consumer=True, nonstream=nonstream)
    assert run.prompts == 1, "session/prompt was automatically resent to Cursor"
    assert len(run.creates) == 1
    assert run.fallback_calls == []
    assert not run.result.get("completed")
    assert run.result["failure_retryable"] is False
    assert "not resent automatically" in run.result.get("error", "")
    assert "<think>" not in run.result["final_response"]


@pytest.mark.parametrize("provider", ["cursor-acp", "custom"], ids=["cursor", "control"])
@pytest.mark.parametrize("scenario", ["think", "length", "length-tool"], ids=["thinking", "text", "tool"])
def test_unmarked_local_stream_error_after_delivery_does_not_replay(tmp_path, monkeypatch, provider, scenario):
    agent, client, fallbacks = _make_agent(tmp_path, monkeypatch, provider, True, scenario)
    agent.valid_tool_names = {"terminal"}
    create = client.create
    def break_after_delta(**kwargs):
        stream = create(**kwargs)
        if client.prompt_deliveries != 1:
            return stream
        def broken():
            for chunk in stream:
                yield chunk
                # A local adapter/emitter error need not carry native exception
                # attributes; delivery evidence is still held by request custody.
                raise ConnectionError("unmarked local stream adapter failure")
        return iter(broken())
    client.chat.completions.create = break_after_delta
    try:
        result = agent.run_conversation("Work on this request.")
    finally:
        agent.close()
    assert not fallbacks
    if provider == "cursor-acp":
        assert client.prompt_deliveries == 1, "inner stream recovery resent a delivered native prompt"
        assert not result.get("completed")
    else:
        assert client.prompt_deliveries >= 2, "other providers lost partial-stream recovery"
        assert "Recovered answer." in result["final_response"]
