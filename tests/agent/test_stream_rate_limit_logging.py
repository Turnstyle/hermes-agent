"""Pre-delivery Anthropic throttling remains visible without a redundant traceback."""

import logging
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch


class _ProviderError(Exception):
    def __init__(self, error_type):
        super().__init__("HTTP 429: retry later")
        self.status_code = 429
        self.body = {"type": "error", "error": {"type": error_type, "message": "retry later"}}


@pytest.mark.parametrize("error_type,level,traceback_expected", [
    ("rate_limit_error", logging.WARNING, False),
    ("unexpected_error", logging.ERROR, True),
])
def test_anthropic_429_stream_logging_preserves_error_for_outer_retry(
    error_type, level, traceback_expected, caplog, monkeypatch
):
    call = cch._StreamingCall.__new__(cch._StreamingCall)
    call.agent = SimpleNamespace(
        provider="anthropic", api_mode="anthropic_messages",
        _stream_options_unsupported=False, _interrupt_requested=False,
        _is_provider_stream_parse_error=lambda error: False,
    )
    call._request_cancelled = {"value": False}
    call.deltas_were_sent = {"yes": False}
    call.provider_tool_in_flight = {"yes": False}
    call.result = {"error": None}
    monkeypatch.setattr(call, "_maybe_disable_streaming", lambda error: None)
    error = _ProviderError(error_type)

    with caplog.at_level(logging.WARNING, logger=cch.__name__):
        try:
            raise error
        except _ProviderError as caught:
            assert call._handle_stream_error(caught, attempt=0, max_retries=0) is False

    assert call.result["error"] is error
    records = [record for record in caplog.records if record.name == cch.__name__]
    assert len(records) == 1
    assert records[0].levelno == level
    assert bool(records[0].exc_info) is traceback_expected
    assert "HTTP 429" in records[0].getMessage()
