"""Gateway-draining session chat spools must replay as turns, not transcript rows."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter
from gateway.shutdown_flush import (
    GATEWAY_DRAINING_REASON,
    recover_gateway_draining_session_chats,
    recover_pending_to_db,
)


def _write_draining_spool(flush_dir, *, session_id: str, text: str) -> None:
    payload = {
        "session_key": f"agent:api:{session_id}",
        "reason": GATEWAY_DRAINING_REASON,
        "ts": int(time.time()),
        "data": {"text": text, "session_id": session_id},
    }
    path = flush_dir / "drain-spool.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_gateway_draining_spool_replays_via_session_chat_dispatch(tmp_path, monkeypatch):
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)

    session_id = "bot-chat"
    text = "deliver me after restart"
    _write_draining_spool(flush_dir, session_id=session_id, text=text)

    mock_db = MagicMock()
    assert recover_pending_to_db(mock_db) == 0
    mock_db.append_message.assert_not_called()
    assert flush_dir.joinpath("drain-spool.json").exists()

    async def _exercise_replay() -> None:
        runner = SimpleNamespace(adapters={})
        assert await recover_gateway_draining_session_chats(runner) == 0
        assert flush_dir.joinpath("drain-spool.json").exists()

        api = APIServerAdapter.__new__(APIServerAdapter)
        dispatch = AsyncMock(return_value=False)
        api.dispatch_session_chat_turn = dispatch
        runner.adapters = {Platform.API_SERVER: api}

        assert await recover_gateway_draining_session_chats(runner) == 0
        dispatch.assert_awaited_once_with(session_id=session_id, message=text)
        assert flush_dir.joinpath("drain-spool.json").exists()

        dispatch.reset_mock()
        dispatch.return_value = True
        assert await recover_gateway_draining_session_chats(runner) == 1
        dispatch.assert_awaited_once_with(session_id=session_id, message=text)
        assert not flush_dir.joinpath("drain-spool.json").exists()

    asyncio.run(_exercise_replay())


def test_failed_replay_turn_keeps_the_spool_for_a_later_replay(tmp_path, monkeypatch):
    """A replayed turn that fails (after its retry) is not a success: the sole spool survives."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    _write_draining_spool(flush_dir, session_id="bot-chat", text="deliver me after restart")

    api = APIServerAdapter.__new__(APIServerAdapter)
    api._concurrency_limited_response = lambda: None
    api._build_session_chat_ctx = AsyncMock(return_value=({"session_id": "bot-chat", "run_kwargs": {}}, None))
    api._answer_through_live_bot_chat = AsyncMock(return_value=None)
    api._conversation_history_for_session = AsyncMock(return_value=[])
    failed = {"failed": True, "error": "Error code: 503 - upstream overloaded"}
    api._run_agent = AsyncMock(return_value=(failed, None))
    runner = SimpleNamespace(adapters={Platform.API_SERVER: api})

    async def _exercise() -> None:
        assert await recover_gateway_draining_session_chats(runner) == 0
        assert flush_dir.joinpath("drain-spool.json").exists()

        api._run_agent = AsyncMock(return_value=({"final_response": "done"}, None))
        assert await recover_gateway_draining_session_chats(runner) == 1
        assert not flush_dir.joinpath("drain-spool.json").exists()

    asyncio.run(_exercise())
