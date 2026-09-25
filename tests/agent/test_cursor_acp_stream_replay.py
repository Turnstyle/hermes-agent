"""Native-effect requests must not become retries or synthetic continuations."""
import json
import os
import signal
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent.cursor_acp_client import CursorACPClient

PEER = Path(__file__).parent / "fixtures" / "cursor_acp_lifecycle_peer.py"
pytestmark = pytest.mark.live_system_guard_bypass


@pytest.mark.parametrize("error_type", [ConnectionError, BrokenPipeError, RuntimeError, InterruptedError])
@pytest.mark.parametrize("visible", [False, True])
def test_generic_no_replay_error_skips_inner_retry_and_continuation(monkeypatch, error_type, visible):
    from agent.chat_completion_helpers import _StreamingCall

    error = error_type("request may have executed")
    error.no_auto_replay = True
    call = object.__new__(_StreamingCall)
    call.agent = Mock(_interrupt_requested=False, _stream_options_unsupported=False,
                      provider="custom", model="fixture", api_mode="chat_completions")
    call.agent._is_provider_stream_parse_error.return_value = False
    call._request_cancelled = {"value": False}
    call.result = {"error": None}
    call.deltas_were_sent = {"yes": visible}
    call.provider_tool_in_flight = {"yes": False}
    call._visible_text_delivered = lambda: visible
    call._retry_after_drop = Mock()
    call._maybe_disable_streaming = Mock()
    call._unmask_server_error_with_nonstreaming = Mock(return_value=False)
    assert call._handle_stream_error(error, 0, 2) is False
    assert call.result["error"] is error
    call._retry_after_drop.assert_not_called()
    call._unmask_server_error_with_nonstreaming.assert_not_called()
    call._partial_stream_stub = Mock()
    call._resolve_stale_timeout = lambda: None
    call._run_call = lambda: None
    call._monitor_loop = lambda: None
    monkeypatch.setattr("agent.chat_completion_helpers.should_use_direct_api_call", lambda agent: True)
    with pytest.raises(RuntimeError if error_type is InterruptedError else error_type) as caught:
        call.run()
    if error_type is InterruptedError:
        assert caught.value.__cause__ is error and caught.value.no_auto_replay
    else:
        assert caught.value is error
    call._partial_stream_stub.assert_not_called()


@pytest.mark.parametrize("scenario", [
    "chunk-exit", "chunk-rpc-error", "chunk-timeout", "chunk-watchdog", "chunk-sigint", "perm-closestdin",
])
def test_real_stream_consumer_never_resends_delivered_prompt(tmp_path, monkeypatch, record_property, scenario):
    from run_agent import AIAgent
    import agent.cursor_acp_client as client_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "4" if scenario == "chunk-watchdog" else "60")
    monkeypatch.setattr(client_module, "_effective_timeout", lambda value: 15 if scenario == "chunk-watchdog" else 2)
    journal = tmp_path / "calls.jsonl"
    shown, calls, errors, sessions, signals = [], [], [], [], []
    released = {}
    first_text = threading.Event()
    finished = threading.Event()
    create, open_session = CursorACPClient._create, CursorACPClient._open
    release = CursorACPClient._release

    def counted_create(self, **kwargs):
        calls.append(1)
        return create(self, **kwargs)

    def retain_session(self, timeout):
        session = open_session(self, timeout)
        sessions.append(session)
        released[session] = threading.Event()
        return session

    def retain_release(self, session, primary_error=None):
        try:
            return release(self, session, primary_error=primary_error)
        finally:
            released[session].set()

    def consume(text):
        if text:
            shown.append(text)
            first_text.set()

    monkeypatch.setattr(CursorACPClient, "_create", counted_create)
    monkeypatch.setattr(CursorACPClient, "_open", retain_session)
    monkeypatch.setattr(CursorACPClient, "_release", retain_release)
    agent = AIAgent(provider="cursor-acp", api_key="cursor-acp", base_url="acp://cursor",
                    acp_command=sys.executable, acp_args=[str(PEER), scenario, str(journal)],
                    model="default[]", quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=[], max_iterations=4,
                    stream_delta_callback=consume,
                    request_overrides={"extra_body": {"cursor": {"allow_native_tools": True}}})
    def forbidden_fallback(*args, **kwargs):
        pytest.fail("Delivered prompt reached fallback")
    monkeypatch.setattr(agent, "_try_activate_fallback", forbidden_fallback)
    original_hook = agent._invoke_api_request_error_hook
    def error_hook(**kwargs):
        errors.append(kwargs["error_type"])
        return original_hook(**kwargs)
    monkeypatch.setattr(agent, "_invoke_api_request_error_hook", error_hook)
    sender = None
    old_handler = None
    if scenario == "chunk-sigint":
        # Exercise a real SIGINT after delivery, routed as the CLI's stop action.
        def interrupt(signum, frame):
            signals.append(signum)
            agent.interrupt()
        old_handler = signal.signal(signal.SIGINT, interrupt)
        def send_sigint():
            if first_text.wait(10) and not finished.is_set():
                os.kill(os.getpid(), signal.SIGINT)
        sender = threading.Thread(target=send_sigint)
        sender.start()
    try:
        result = agent.run_conversation("ping")
        receipts = [json.loads(line) for line in journal.read_text().splitlines()]
        prompt_count = sum(row.get("method") == "session/prompt" for row in receipts)
        record_property("prompts_received", prompt_count)
        record_property("create_calls", len(calls))
        record_property("interrupted", bool(result.get("interrupted")))
        record_property("failed", bool(result.get("failed")))
        assert prompt_count == 1
        assert len(calls) == 1
        if scenario == "chunk-sigint":
            assert signals == [signal.SIGINT]
            assert result["interrupted"]
        else:
            assert result["failed"] and result["failure_retryable"] is False
            final = result["final_response"]
            assert "not resent" in final and "/retry" in final and "repeat" in final
            assert "rejected" not in final and "retrying won't help" not in final
            if scenario == "perm-closestdin":
                assert errors == ["RuntimeError"]  # No transport exception escapes after delivery.
            else:
                assert "partial answer" in "".join(shown)
                assert final.startswith("partial answer") and result["partial"]
    finally:
        finished.set()
        if sender:
            sender.join(timeout=12)
            signal.signal(signal.SIGINT, old_handler)
        agent.close()
    for session in sessions:
        assert released[session].wait(12), "Owned request cleanup did not finish"
        assert not session._group_exists()
    record_property("owned_groups_gone", True)
