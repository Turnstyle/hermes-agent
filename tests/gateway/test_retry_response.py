"""Regression test: /retry must return the agent response, not None.

Before the fix in PR #441, _handle_retry_command() called
_handle_message(retry_event) but discarded its return value with `return None`,
so users never received the final response.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _EmptyPendingTurnDB:
    """Healthy durable-DB seam with no pending recovery generations."""

    @staticmethod
    def _execute_write(callback):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        return callback(connection)


@pytest.fixture
def gateway(tmp_path):
    config = MagicMock()
    config.sessions_dir = tmp_path
    config.max_context_messages = 20
    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = MagicMock()
    return gw


@pytest.mark.asyncio
async def test_retry_returns_response_not_none(gateway):
    """_handle_retry_command must return the inner handler response, not None."""
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C1",
        chat_type="dm",
        user_id="U1",
        scope_id="W1",
    )
    gateway.session_store.get_or_create_session.return_value = MagicMock(
        session_id="test-session", session_key=build_session_key(source)
    )
    gateway.session_store._db_for_key.return_value = _EmptyPendingTurnDB()
    gateway.session_store.load_transcript.return_value = [
        {"role": "user", "content": "Hello Hermes"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    gateway.session_store.rewrite_transcript = MagicMock()
    expected_response = "Hi there! (retried)"
    gateway._handle_message = AsyncMock(return_value=expected_response)
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=source,
    )
    result = await gateway._handle_retry_command(event)
    assert result is not None, "/retry must not return None"
    assert result == expected_response
