"""Busy drain locks produce retryable API receipts instead of generic failures."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import fleet_message_drain as drain


def _app(adapter):
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    return app


def _held_drain_lock(home: Path, session_id: str):
    key = (str(home.resolve()), session_id)
    lock = threading.Lock()
    lock.acquire()
    with drain._api_turn_locks_guard:
        drain._api_turn_locks[key] = lock
    return lock, key


@pytest.mark.asyncio
@pytest.mark.parametrize("enqueue", ["queued", "declined", "failed"])
async def test_session_chat_busy_drain_returns_queued_or_retryable_409(tmp_path, monkeypatch, enqueue):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    db = MagicMock(db_path=str(tmp_path / "state.db"))
    agent = MagicMock(session_id="s1", _session_db=db, _memory_manager=None)
    ctx = {"session_id": "s1", "gateway_session_key": None, "user_message": "hi",
           "body": {}, "run_kwargs": {"user_message": "hi", "session_id": "s1", "turn_author": {}}}
    lock, key = _held_drain_lock(tmp_path, "s1")
    monkeypatch.setattr(drain, "_api_drain_enabled", lambda _: True)
    try:
        with (patch.object(adapter, "_prepare_session_chat", new=AsyncMock(return_value=(ctx, None))),
              patch.object(adapter, "_answer_through_live_bot_chat", new=AsyncMock(return_value=None)),
              patch.object(adapter, "_conversation_history_for_session", new=AsyncMock(return_value=[])),
              patch.object(adapter, "_ensure_session_db_async", new=AsyncMock(return_value=db)),
              patch.object(adapter, "_create_agent", return_value=agent),
              patch("tools.fleet_message_drain.recipient_drain_enabled", return_value=enqueue != "declined"),
              patch("tools.bot_mode_dm._busy_sender", return_value="sender"),
              patch("tools.bot_mode_dm.enqueue_busy_peer_dm", return_value="msg_1",
                    side_effect=RuntimeError("fake enqueue failure") if enqueue == "failed" else None) as queued):
            async with TestClient(TestServer(_app(adapter))) as cli:
                resp = await cli.post("/api/sessions/s1/chat", json={"message": "hi"})
                body = await resp.json()
        if enqueue == "queued":
            assert resp.status == 202 and body["reason"] == "target_busy"
            assert body["message_id"] == "msg_1"
            queued.assert_called_once()
        else:
            assert resp.status == 409 and resp.headers["Retry-After"] == "30"
            assert body["error"]["code"] == "target_busy"
            if enqueue == "declined":
                queued.assert_not_called()
            else:
                queued.assert_called_once()
    finally:
        lock.release()
        with drain._api_turn_locks_guard:
            drain._api_turn_locks.pop(key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,payload,stream", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, False),
    ("/v1/responses", {"input": "hi"}, False),
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True}, True),
    ("/v1/responses", {"input": "hi", "stream": True}, True),
], ids=["chat-completions", "responses", "chat-completions-sse", "responses-sse"])
async def test_openai_routes_report_busy_drain(tmp_path, monkeypatch, route, payload, stream):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    db = MagicMock(db_path=str(tmp_path / "state.db"))
    agent = MagicMock(_session_db=db, _memory_manager=None)
    lock, key = _held_drain_lock(tmp_path, "unused")
    monkeypatch.setattr(drain, "_api_drain_enabled", lambda _: True)
    # The route chooses its own session id. Hold that session exactly as the drain worker does.
    async def run_busy(**kwargs):
        sid = kwargs["session_id"]
        with drain._api_turn_locks_guard:
            drain._api_turn_locks.pop(key, None)
            drain._api_turn_locks[(str(tmp_path.resolve()), sid)] = lock
        return await original_run(**kwargs)

    original_run = adapter._run_agent
    try:
        with (patch.object(adapter, "_create_agent", return_value=agent),
              patch.object(adapter, "_run_agent", side_effect=run_busy)):
            async with TestClient(TestServer(_app(adapter))) as cli:
                resp = await cli.post(route, json=payload)
                if stream:
                    text = await resp.text()
                    assert resp.status == 200 and "target_busy" in text
                    assert "retry_after" in text
                    assert "event: error" in text
                    if route.endswith("responses"):
                        assert "event: response.failed" in text
                else:
                    body = await resp.json()
                    assert resp.status == 409 and resp.headers["Retry-After"] == "30"
                    assert body["error"]["code"] == "target_busy"
    finally:
        lock.release()
        with drain._api_turn_locks_guard:
            drain._api_turn_locks.pop((str(tmp_path.resolve()), "unused"), None)
            for k, value in list(drain._api_turn_locks.items()):
                if value is lock:
                    drain._api_turn_locks.pop(k)


@pytest.mark.asyncio
async def test_session_chat_sse_reports_busy_drain(tmp_path, monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    db = MagicMock(db_path=str(tmp_path / "state.db"))
    agent = MagicMock(session_id="s1", _session_db=db, _memory_manager=None)
    ctx = {"session_id": "s1", "gateway_session_key": None, "user_message": "hi",
           "runtime_request": {"requested": {}}, "lock_active": False,
           "body": {}, "run_kwargs": {"user_message": "hi", "session_id": "s1"}}
    lock, key = _held_drain_lock(tmp_path, "s1")
    monkeypatch.setattr(drain, "_api_drain_enabled", lambda _: True)
    try:
        with (patch.object(adapter, "_prepare_session_chat", new=AsyncMock(return_value=(ctx, None))),
              patch.object(adapter, "_stream_through_live_bot_chat", new=AsyncMock(return_value=None)),
              patch.object(adapter, "_conversation_history_for_session", new=AsyncMock(return_value=[])),
              patch.object(adapter, "_create_agent", return_value=agent)):
            async with TestClient(TestServer(_app(adapter))) as cli:
                resp = await cli.post("/api/sessions/s1/chat/stream", json={"message": "hi"})
                text = await resp.text()
        assert resp.status == 200 and "event: error" in text
        assert '"reason": "target_busy"' in text and '"retry_after": 30' in text
    finally:
        lock.release()
        with drain._api_turn_locks_guard:
            drain._api_turn_locks.pop(key, None)


@pytest.mark.asyncio
async def test_accepted_run_status_and_event_keep_busy_retry_hint(tmp_path, monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    db = MagicMock(db_path=str(tmp_path / "state.db"))
    agent = MagicMock(session_id="s1", _session_db=db, _memory_manager=None)
    lock, key = _held_drain_lock(tmp_path, "s1")
    monkeypatch.setattr(drain, "_api_drain_enabled", lambda _: True)
    try:
        with patch.object(adapter, "_create_agent", return_value=agent):
            async with TestClient(TestServer(_app(adapter))) as cli:
                admitted = await cli.post("/v1/runs", json={"input": "hi", "session_id": "s1"})
                assert admitted.status == 202
                run_id = (await admitted.json())["run_id"]
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "failed":
                        break
                assert status_resp.status == 200 and status["status"] == "failed"
                assert status["reason"] == "target_busy" and status["retry_after"] == 30
                events = await cli.get(f"/v1/runs/{run_id}/events")
                event_text = await events.text()
                assert "target_busy" in event_text and "retry_after" in event_text
    finally:
        lock.release()
        with drain._api_turn_locks_guard:
            drain._api_turn_locks.pop(key, None)
