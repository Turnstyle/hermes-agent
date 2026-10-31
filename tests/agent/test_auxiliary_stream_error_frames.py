"""An adapter-delivered SSE ``error`` frame on an auxiliary stream fails the call with the
structured body instead of aggregating into an empty "successful" completion.

Fake chunks only: no network, no provider SDK request.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import (
    _FALLBACK_REASONS,
    _aggregate_chat_stream,
    _aggregate_chat_stream_async,
    _create_with_progress,
    _is_statusless_structured_provider_error,
    call_llm,
)
from agent.auxiliary_stream_error import AuxiliaryStreamError

_PROVIDER_BODY = {"type": "server_error", "code": "overloaded", "message": "secret-ish provider detail"}


def _text_chunk(text, finish_reason=None):
    delta = SimpleNamespace(content=text, tool_calls=None, reasoning=None, reasoning_content=None)
    return SimpleNamespace(id="r1", model="m", usage=None,
                           choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


@pytest.mark.parametrize("frame", [
    SimpleNamespace(id="r1", model="m", usage=None, choices=[], error=_PROVIDER_BODY),
    {"error": _PROVIDER_BODY},
    SimpleNamespace(id="r1", model="m", usage=None, choices=[], model_extra={"error": _PROVIDER_BODY}),
], ids=["attribute", "dict", "model_extra"])
def test_error_only_frame_raises_with_structured_body(frame):
    with pytest.raises(AuxiliaryStreamError) as caught:
        _aggregate_chat_stream(iter([frame]), model="m")
    assert caught.value.body == _PROVIDER_BODY
    # The provider payload stays on .body; the message (what reaches logs) never echoes it.
    assert "secret-ish" not in str(caught.value)


def test_error_frame_after_partial_content_is_not_a_truncated_success():
    with pytest.raises(AuxiliaryStreamError):
        _aggregate_chat_stream(iter([_text_chunk("partial "), {"error": "upstream reset"}]), model="m")


def test_ordinary_frames_without_error_still_aggregate():
    frames = [_text_chunk("hello "), _text_chunk("world", finish_reason="stop")]
    for frame in frames:
        frame.error = None  # an explicit null error is an ordinary frame
    result = _aggregate_chat_stream(iter(frames), model="m")
    assert result.choices[0].message.content == "hello world"
    assert result.choices[0].finish_reason == "stop"


def test_async_aggregation_raises_on_error_frame():
    async def _frames():
        yield SimpleNamespace(id="r1", model="m", usage=None, choices=[], error=_PROVIDER_BODY)

    with pytest.raises(AuxiliaryStreamError) as caught:
        asyncio.run(_aggregate_chat_stream_async(_frames(), model="m"))
    assert caught.value.body == _PROVIDER_BODY


def test_streamed_create_surfaces_error_frame_and_recovery_classifies_it():
    client = MagicMock()
    client.base_url = "https://relay.example/v1"
    client.chat.completions.create.return_value = iter([{"error": _PROVIDER_BODY}])

    with pytest.raises(AuxiliaryStreamError) as caught:
        _create_with_progress(client, {"model": "m", "messages": []}, task="compression", force_stream=True)

    assert client.chat.completions.create.call_args.kwargs["stream"] is True
    assert _is_statusless_structured_provider_error(caught.value)
    reason = next(label for predicate, label in _FALLBACK_REASONS if predicate(caught.value))
    assert reason == "structured provider error"


def test_error_frame_on_primary_reaches_configured_fallback():
    primary_client = MagicMock()
    primary_client.base_url = "https://relay.example/v1"
    primary_client.chat.completions.create.side_effect = AuxiliaryStreamError(_PROVIDER_BODY)
    fallback_client = MagicMock()
    fallback_client.chat.completions.create.return_value = MagicMock(choices=[
        MagicMock(message=MagicMock(content="fallback response"))])

    with patch("agent.auxiliary_client._get_cached_client", return_value=(primary_client, "virtual-model")), \
         patch("agent.auxiliary_client._resolve_task_provider_model",
               return_value=("custom", "virtual-model", "https://relay.example/v1", "test-key", None)), \
         patch("agent.auxiliary_client._try_configured_fallback_chain",
               return_value=(fallback_client, "fallback-model", "fallback_chain[0](openrouter)")) as chain, \
         patch("agent.auxiliary_client._try_main_agent_model_fallback", return_value=(None, None, "")):
        call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])

    assert fallback_client.chat.completions.create.called
    assert chain.call_args.kwargs["reason"] == "structured provider error"
