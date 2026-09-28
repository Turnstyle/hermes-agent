"""Opted-in peer DMs acknowledge a busy Bot Chat without waiting for its current turn."""

import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox
from tools import bot_mode_dm, fleet_message_drain


def _app(adapter):
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_shape", ["lease_held", "live_owner_busy"])
async def test_opted_in_peer_dm_fast_acks_one_durable_busy_delivery(tmp_path, monkeypatch, busy_shape):
    home = tmp_path.resolve()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_mode_dm, "_LIVE_WAIT_SECONDS", 4.5)
    db = SessionDB(home / "state.db")
    db.create_session("bot-chat", "desktop")
    db.set_session_title("bot-chat", "Bot Chat")
    holder = f"pid={os.getpid()}:turn=other:platform=cli"
    owner_lease = None
    if busy_shape == "lease_held":
        assert db.try_acquire_session_turn_lease("bot-chat", holder, ttl_seconds=30)
    else:
        from hermes_cli.active_sessions import try_acquire_active_session
        owner_lease, refusal = try_acquire_active_session(
            session_id="bot-chat", surface="desktop", config={}, registry_home=home,
            track_liveness=True, metadata={"live_session_id": "live-1", "bot_live_delivery_consumer": True})
        assert owner_lease is not None and refusal is None

    enqueued = []
    admitted = []
    original_deliver = mailbox.deliver_to_live_owner

    def _deliver(*args, **kwargs):
        record = original_deliver(*args, **kwargs)
        admitted.append(record["delivery_id"])
        return record

    monkeypatch.setattr(mailbox, "deliver_to_live_owner", _deliver)
    monkeypatch.setattr(fleet_message_drain, "recipient_drain_enabled", lambda *a, **k: True)
    monkeypatch.setattr(bot_mode_dm, "enqueue_busy_peer_dm",
                        lambda home, sender, message: (enqueued.append((home, sender, message)) or "message-1"),
                        raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    try:
        with patch.object(adapter, "_run_agent", AsyncMock(return_value=({"final_response": "unexpected"}, {}))) as run, \
             patch.object(adapter, "_create_agent") as create:
            async with TestClient(TestServer(_app(adapter))) as cli:
                started = time.monotonic()
                resp = await cli.post("/api/sessions/bot-chat/chat", json={
                    "message": "ping", "on_busy": "queue",
                    "author": {"id": "bot:cto", "name": "cto", "is_bot": True}})
                elapsed = time.monotonic() - started
                body = await resp.json()
        assert resp.status == 202, body
        assert elapsed < 4, elapsed
        assert body["object"] == "hermes.session.chat.queued" and body["status"] == "queued"
        assert not run.called and not create.called
        if busy_shape == "lease_held":
            assert body["message_id"] == body["delivery_id"] == "message-1"
            assert body["reason"] == "target_busy"
            assert enqueued == [(home, "cto", "ping")]
            assert not admitted
        else:
            assert len(admitted) == 1 and body["delivery_id"] == admitted[0]
            assert not enqueued
            record_path = home / "runtime" / "bot_live_delivery" / f"{admitted[0]}.json"
            record = json.loads(record_path.read_text())
            assert record["status"] == "queued" and record["message"] == "ping"
    finally:
        if busy_shape == "lease_held":
            db.release_session_turn_lease("bot-chat", holder)
        if owner_lease is not None:
            owner_lease.release()
        db.close()
