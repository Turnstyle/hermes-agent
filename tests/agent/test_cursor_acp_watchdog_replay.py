"""A prompt Cursor already received is never resent after a Hermes watchdog cancels it.

Real ``AIAgent`` turns against a fake ACP peer (no network, no real Cursor). The peer answers
``session/cancel`` the way the ACP spec says real Cursor does (``stopReason: cancelled``), keeps
streaming text after the cancel, or ignores the cancel and finishes ``end_turn`` late. Every delivered
watchdog row must end as ONE prompt, ZERO fallback activations and a terminal, honest error; controls
prove that explicit stops, pre-delivery setup failures and other providers keep their behaviour.
"""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cursor_acp_client import CursorACPClient

PEER = Path(__file__).parent / "fixtures" / "cursor_acp_lifecycle_peer.py"
pytestmark = pytest.mark.live_system_guard_bypass
STALE_SECONDS = 3
NATIVE_ACK = {"extra_body": {"cursor": {"allow_native_tools": True}}}


def _run_turn(tmp_path, monkeypatch, scenario, *, consumer=True, stale=60, nonstream=False,
              interrupt_after_delivery=False, fast_backoff=False):
    """One real ``run_conversation`` turn; returns what the peer, the client and the loop observed."""
    from run_agent import AIAgent
    import agent.cursor_acp_client as client_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", str(stale))
    monkeypatch.setattr(client_module, "_effective_timeout", lambda value: 30)
    if fast_backoff:  # timing only; retry decisions stay untouched
        import agent.turn_api_error as turn_api_error
        real_sleep = turn_api_error.interruptible_backoff_sleep
        monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep",
                            lambda agent, wait_time, *a, **kw: real_sleep(agent, min(wait_time, .2), *a, **kw))
    journal = tmp_path / "calls.jsonl"
    shown, creates, sessions, fallback_calls = [], [], [], []
    released = {}
    first_text = threading.Event()
    create, open_session, release = CursorACPClient._create, CursorACPClient._open, CursorACPClient._release

    def counted_create(self, **kwargs):
        creates.append(bool(kwargs.get("stream")))
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
                    stream_delta_callback=consume if consumer else None, request_overrides=NATIVE_ACK)
    if nonstream:  # the session already fell back to non-streaming; only the timing is shimmed
        agent._disable_streaming = True
        monkeypatch.setattr(agent, "_compute_non_stream_stale_timeout", lambda api_kwargs: float(STALE_SECONDS))
    ladder = agent._try_activate_fallback

    def counted_fallback(*args, **kwargs):
        fallback_calls.append(1)
        return ladder(*args, **kwargs)

    monkeypatch.setattr(agent, "_try_activate_fallback", counted_fallback)
    if interrupt_after_delivery:  # the /stop path, triggered by events rather than wall-clock timing
        def stop_after_delivery():
            for _ in range(200):
                if journal.exists() and "session/prompt" in journal.read_text():
                    break
                threading.Event().wait(.05)
            if not consumer or first_text.wait(10):
                agent.interrupt()
        threading.Thread(target=stop_after_delivery, daemon=True).start()
    try:
        result = agent.run_conversation("ping")
    finally:
        agent.close()
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    for session in sessions:
        assert released[session].wait(15), "Owned request cleanup did not finish"
        assert not session._group_exists()
    return SimpleNamespace(
        result=result, shown="".join(shown), creates=creates, fallback_calls=fallback_calls,
        prompts=sum(row.get("method") == "session/prompt" for row in rows),
        spawns=sum("scenario" in row for row in rows))


def _assert_terminal_single_prompt(run, *, partial):
    result, final = run.result, run.result["final_response"]
    assert run.prompts == 1, "a delivered prompt was resent"
    assert len(run.creates) == 1
    assert run.fallback_calls == [], "a delivered prompt reached the fallback ladder"
    assert result["failed"] is True and not result.get("completed") and not result.get("interrupted")
    assert result["failure_retryable"] is False
    assert "not resent" in final and "/retry" in final and "repeat" in final
    assert "rejected" not in final and "retrying won't help" not in final
    if partial:
        assert "partial answer" in run.shown
        assert final.startswith("partial answer") and result["partial"]


@pytest.mark.parametrize("consumer", [True, False], ids=["consumer", "no-consumer"])
@pytest.mark.parametrize("scenario", ["chunk-ackcancel", "chunk-latetext", "chunk-lateend"])
def test_stale_watchdog_after_delivery_is_terminal_with_one_prompt(tmp_path, monkeypatch, scenario, consumer):
    """Cancel ack after text / more text after the cancel / late ``end_turn`` after the cancel."""
    run = _run_turn(tmp_path, monkeypatch, scenario, consumer=consumer, stale=STALE_SECONDS)
    # Without a display consumer nothing was ever shown, so there is no delivered partial text to retain.
    _assert_terminal_single_prompt(run, partial=consumer)
    assert "late-text" not in run.result["final_response"]


@pytest.mark.parametrize("scenario", ["chunk-ackcancel", "chunk-stall"])
def test_nonstream_watchdog_after_delivery_is_terminal_with_one_prompt(tmp_path, monkeypatch, scenario):
    """Forced non-streaming mode: a cancel-acked partial is not a complete answer; a timeout is not a replay."""
    run = _run_turn(tmp_path, monkeypatch, scenario, nonstream=True, consumer=False)
    _assert_terminal_single_prompt(run, partial=False)
    assert run.creates == [False]
    assert run.result["final_response"].strip() != "partial answer"


@pytest.mark.parametrize("consumer", [True, False], ids=["consumer", "no-consumer"])
def test_explicit_stop_after_delivery_stays_interrupted(tmp_path, monkeypatch, consumer):
    """A user stop is not a watchdog failure: interrupted, one prompt, no fallback, no replay copy."""
    run = _run_turn(tmp_path, monkeypatch, "chunk-ackcancel", consumer=consumer, interrupt_after_delivery=True)
    assert run.result["interrupted"] is True and not run.result.get("failed")
    assert run.prompts == 1 and len(run.creates) == 1 and run.fallback_calls == []
    assert "not resent" not in run.result["final_response"]
    if consumer:
        assert "partial answer" in run.result["final_response"]


def test_setup_failure_before_delivery_stays_retryable(tmp_path, monkeypatch):
    """The peer dies before any prompt can be sent: retry and the fallback ladder remain available."""
    run = _run_turn(tmp_path, monkeypatch, "setup-exit", consumer=True, fast_backoff=True)
    assert run.prompts == 0
    assert run.spawns >= 2, "setup failure was not retried"
    assert run.fallback_calls, "setup failure never reached the fallback ladder"
    assert "not resent" not in run.result["final_response"]


# ── Unit level: the delivery marker itself ─────────────────────────────────────────────────────────

class _Delivering:
    """Stand-in request client exposing the generic ``prompt_deliveries`` counter."""
    def __init__(self, deliveries=0):
        self.prompt_deliveries = deliveries


def test_registry_reports_only_deliveries_made_while_the_client_was_checked_out():
    from agent.chat_completion_helpers import _RequestClientRegistry

    registry = _RequestClientRegistry(SimpleNamespace(
        _close_request_openai_client=lambda client, reason: None,
        _abort_request_openai_client=lambda client, reason: None))
    reused = _Delivering(deliveries=3)  # a cached client that served earlier turns
    registry.set_client(reused)
    assert registry.prompt_delivered() is False
    reused.prompt_deliveries += 1
    assert registry.prompt_delivered() is True
    registry.close_once("worker_exit")  # the owner pops the client; the verdict must survive
    assert registry.client is None and registry.prompt_delivered() is True


def test_registry_ignores_clients_without_a_delivery_counter():
    from agent.chat_completion_helpers import _RequestClientRegistry

    registry = _RequestClientRegistry(SimpleNamespace())
    registry.set_client(SimpleNamespace(prompt_deliveries="not-a-count"))
    assert registry.prompt_delivered() is False


@pytest.mark.parametrize("delivered", [True, False])
def test_nonstream_timeout_after_kill_is_tagged_only_when_the_prompt_was_delivered(delivered):
    from agent.chat_completion_nonstream import _NonStreamRequest

    request = object.__new__(_NonStreamRequest)
    request.thread = SimpleNamespace(join=lambda timeout: None)
    request.result = {"response": None, "error": None}
    request.clients = SimpleNamespace(prompt_delivered=lambda: delivered)
    request._await_worker_after_kill("watchdog fired")
    error = request.result["error"]
    assert isinstance(error, TimeoutError)
    assert getattr(error, "no_auto_replay", False) is delivered


def _cursor_client(tmp_path, scenario):
    return CursorACPClient(acp_command=sys.executable, acp_args=[str(PEER), scenario, str(tmp_path / "calls.jsonl")],
                           acp_cwd=str(tmp_path), timeout=30)


def test_stream_ends_normally_on_cancel_ack_and_still_counts_the_delivery(tmp_path):
    """Existing stream contract: a cancel ack ends the stream. Hermes' stream sites decide what that means,
    so the client must keep counting the delivered prompt after its session was released."""
    client = _cursor_client(tmp_path, "chunk-ackcancel")
    messages = [{"role": "user", "content": "ping"}]
    stream = client._create(model="default[]", messages=messages, stream=True, extra_body=NATIVE_ACK["extra_body"])
    first = next(stream)
    assert first.choices[0].delta.content == "partial answer "
    assert client.prompt_deliveries == 1
    client.cancel()  # what Hermes' stale watchdog does through the request slot
    rest = list(stream)
    assert rest[-1].cursor_stop_reason == "cancelled"
    stream.close()
    assert client.prompt_deliveries == 1 and not client._sessions
    client.close()


def test_cancelled_nonstream_completion_raises_instead_of_returning_the_partial(tmp_path):
    client = _cursor_client(tmp_path, "chunk-ackcancel")
    threading.Timer(1.5, client.cancel).start()
    with pytest.raises(RuntimeError) as caught:
        client._create(model="default[]", messages=[{"role": "user", "content": "ping"}],
                       extra_body=NATIVE_ACK["extra_body"])
    assert caught.value.no_auto_replay
    assert client.prompt_deliveries == 1
    client.close()


def test_client_counts_a_delivery_only_after_the_prompt_frame_was_written(tmp_path):
    client = _cursor_client(tmp_path, "setup-exit")
    with pytest.raises(RuntimeError):
        client._create(model="default[]", messages=[{"role": "user", "content": "ping"}],
                       extra_body=NATIVE_ACK["extra_body"])
    assert client.prompt_deliveries == 0
    client.close()


# ── Other providers: the changed sites keep their retry and continuation behaviour ─────────────────

class _CancellableStream:
    """Request client for a non-Cursor provider whose stream hangs after text until the watchdog aborts it."""
    def __init__(self):
        self.abort = threading.Event()
        self.requests = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def _chunk(content=None, finish=None):
        return SimpleNamespace(model="stub", usage=None, choices=[SimpleNamespace(index=0, finish_reason=finish, delta=SimpleNamespace(
            content=content, tool_calls=None, reasoning_content=None, reasoning=None))])

    def _create(self, **kwargs):
        self.requests += 1
        request = self.requests

        def chunks():
            if request == 1:
                yield self._chunk("PART-ONE ")
                self.abort.wait(20)  # silence, then the abort lets the iterator end normally (like a cancel ack)
                return
            yield self._chunk("PART-TWO.")
            yield self._chunk(finish="stop")
        return _Iterable(chunks)

    def cancel(self):
        self.abort.set()

    def close(self):
        pass


class _Iterable:
    response = SimpleNamespace(headers={})

    def __init__(self, factory):
        self._factory = factory

    def __iter__(self):
        return self._factory()


@pytest.mark.parametrize("consumer", [True, False], ids=["consumer", "no-consumer"])
def test_other_provider_stale_watchdog_keeps_retry_and_continuation(monkeypatch, consumer):
    """The superseded-attempt error stays untagged for providers with no delivery counter."""
    from unittest.mock import patch
    from run_agent import AIAgent
    from hermes_constants import PARTIAL_STREAM_STUB_ID

    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", str(STALE_SECONDS))
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
    stub = _CancellableStream()
    shown = []
    agent = AIAgent(api_key="test-key", base_url="https://example.com/v1", model="test/model", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=2,
                    stream_delta_callback=(lambda text: shown.append(text)) if consumer else None)
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    with patch("run_agent.AIAgent._create_request_openai_client", return_value=stub), \
            patch("run_agent.AIAgent._close_request_openai_client"), \
            patch("agent.chat_completion_helpers._wait_stream_retry_backoff"):
        response = agent._interruptible_streaming_api_call(
            {"model": "test/model", "messages": [{"role": "user", "content": "hi"}]})
    if consumer:
        # Text reached a display: the loop continues from the partial (one request so far).
        assert response.id == PARTIAL_STREAM_STUB_ID and "PART-ONE" in "".join(shown)
        assert stub.requests == 1
    else:
        # Nothing was shown: the streamer re-opens the stream itself and finishes the answer.
        assert stub.requests == 2
        assert response.choices[0].message.content == "PART-TWO."
