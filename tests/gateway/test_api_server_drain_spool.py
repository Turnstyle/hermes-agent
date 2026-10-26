"""G10: restart-spooled API session chats are admitted once through the canonical door.

A session chat accepted while the gateway drains for a queue-keeping restart is spooled (202); after
the next start it is admitted after boot resume turns, through the session authority, under a
spool-derived id: never as a raw transcript row, never twice, invalid payloads retained.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms import api_server_drain_spool as drain
from gateway.platforms.api_server import APIServerAdapter
from gateway.shutdown_flush import GATEWAY_DRAINING_REASON, recover_pending_spool
from tests.waiting import async_wait_until

PRIMARY = "primary-listener-key-0001"
SHELDON = "sheldon-caller-key-00000001"


def _payload(session_id="s1", text="hello", message_id="a" * 32, ts=1_700_000_000):
    return {"session_key": f"agent:api:{session_id}", "reason": GATEWAY_DRAINING_REASON,
            "message_id": message_id, "ts": ts, "data": {"text": text, "session_id": session_id}}


def _write(spool, name, payload):
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("API_SERVER_CALLER_KEYS", raising=False)
    (tmp_path / ".env").write_text(f"API_SERVER_CALLER_KEYS={json.dumps({'sheldon': SHELDON})}\n")
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("keeps_queue, path, status", [
    (True, "/api/sessions/s1/chat", 202),
    (True, "/api/sessions/s1/chat/stream", 202),
    (False, "/api/sessions/s1/chat", 503),
    (True, "/v1/runs", 503),
], ids=["chat-spooled", "stream-spooled", "interrupt-restart-refused", "runs-refused"])
async def test_draining_session_chat_spools_the_carried_payload(home, monkeypatch, keeps_queue, path, status):
    runner = SimpleNamespace(_queue_during_drain_enabled=lambda: keeps_queue)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    monkeypatch.setattr(APIServerAdapter, "_gateway_is_draining", staticmethod(lambda: True))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PRIMARY}))
    app = web.Application()
    for method, route, handler in adapter._http_route_table():
        app.router.add_route(method, route, handler)
    try:
        async with TestClient(TestServer(app)) as cli:
            body = {"message": "hi", "input": "hi"}
            resp = await cli.post(path, json=body, headers={"Authorization": f"Bearer {SHELDON}"})
            assert resp.status == status, await resp.text()
            answer = await resp.json()
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
    spooled = sorted((home / "pending_messages").glob("*.json")) if (home / "pending_messages").is_dir() else []
    if status != 202:
        assert spooled == [] and answer["error"]["code"] == "gateway_draining"
        return
    assert answer["object"] == "hermes.session.chat.queued" and answer["session_id"] == "s1"
    [payload] = [json.loads(p.read_text()) for p in spooled]
    assert payload["reason"] == GATEWAY_DRAINING_REASON and payload["message_id"] == answer["message_id"]
    assert payload["session_key"] == "agent:api:s1"
    # The verified caller is bound into the durable text; the payload shape is the carried one.
    assert payload["data"] == {"text": "[verified caller: sheldon] hi", "session_id": "s1"}


def test_transcript_recovery_never_appends_a_draining_spool(home):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("s1", "api_server")
        path = _write(home / "pending_messages", "pending-1.json", _payload())
        assert recover_pending_spool(session_db=db) == (0, set())
        assert path.exists()
        assert db.get_messages_as_conversation("s1") == []
    finally:
        db.close()


@pytest_asyncio.fixture
async def gateway(home, monkeypatch):
    """A runner whose real session authority admits API turns; nothing executes them."""
    from gateway import run
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from hermes_state import SessionDB
    monkeypatch.setattr(run, "_load_gateway_config",
                        lambda *_p: {"platform_toolsets": {"cli": []}, "model": {"default": "fixture"}})
    store = SessionStore(home / "sessions", GatewayConfig())
    store._db = SessionDB(db_path=home / "state.db")
    retained = []
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             config=GatewayConfig(), _evict_cached_agent=lambda route: None,
                             _cached_agent_for=lambda route: None, _startup_should_abort=lambda: False,
                             _retain_background_task=lambda task: retained.append(task) or task)
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="fixture")
    scheduled = []
    monkeypatch.setattr(authority, "_schedule", lambda ref: scheduled.append(ref.session_id))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PRIMARY}))
    adapter.gateway_runner = runner
    adapter._session_db = authority.db
    runner.adapters[Platform.API_SERVER] = adapter
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority.db.create_session("s1", "api_server")
    try:
        yield SimpleNamespace(runner=runner, authority=authority, adapter=adapter, spool=home / "pending_messages",
                              scheduled=scheduled, retained=retained)
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        store._db.close()


def _api_admissions(authority):
    return [(row["request_id"], json.loads(row["payload_json"])["text"]) for row in authority.db._read_all(
        "SELECT request_id, payload_json FROM session_admissions WHERE principal_id='api'")]


def _key(message_id):
    return hashlib.sha256(f"gateway-draining:{message_id}".encode()).hexdigest()


@pytest.mark.asyncio
async def test_replay_waits_for_resume_then_admits_once_and_retains_invalid(gateway, monkeypatch):
    monkeypatch.setattr("gateway.run._startup_restore_drain_timeout_secs", lambda: 0.05)
    valid = _write(gateway.spool, "pending-valid.json", _payload())
    invalid = _write(gateway.spool, "pending-invalid.json", _payload(text="   "))
    unreadable = _write(gateway.spool, "pending-garbage.json", "{not json")
    foreign = _write(gateway.spool, "pending-other.json", {"reason": "shutdown", "session_key": "k",
                                                            "data": {"text": "x"}})
    resume = asyncio.get_running_loop().create_future()
    drain.start_draining_session_chat_replay(gateway.runner, [resume])
    await asyncio.wait_for(gateway.retained[0], 5)  # the bounded wait lapsed: deferred, not run
    assert _api_admissions(gateway.authority) == [] and valid.exists()
    resume.set_result(None)  # the boot resume turn finished: its completion callback replays
    await async_wait_until(lambda: len(gateway.retained) == 2, timeout=5, what="the deferred replay")
    assert await asyncio.wait_for(gateway.retained[1], 10) == 1
    [(request_id, text)] = _api_admissions(gateway.authority)
    assert request_id == _key("a" * 32)
    assert text.startswith("Late delivery: this message was sent at ") and text.endswith("\n\nhello")
    assert gateway.scheduled == ["s1"]
    assert not valid.exists()
    assert invalid.exists() and unreadable.exists() and foreign.exists()
    assert gateway.authority.db.get_messages_as_conversation("s1") == [], "admitted, never a raw row"


@pytest.mark.asyncio
async def test_replay_after_a_crash_before_unlink_names_the_same_admission(gateway, tmp_path):
    path = _write(gateway.spool, "pending-1.json", _payload())
    saved = tmp_path / "saved.json"
    shutil.copy(path, saved)
    assert await drain.replay_draining_session_chats(gateway.runner) == 1
    shutil.copy(saved, path)  # the unlink never happened
    assert await drain.replay_draining_session_chats(gateway.runner) == 1
    assert len(_api_admissions(gateway.authority)) == 1 and not path.exists()


@pytest.mark.asyncio
async def test_refused_admission_keeps_spools_in_session_order(gateway, monkeypatch):
    first = _write(gateway.spool, "pending-1.json", _payload(session_id="missing", message_id="1" * 32, ts=10))
    later = _write(gateway.spool, "pending-2.json", _payload(session_id="missing", message_id="2" * 32, ts=20))
    calls = []
    real = drain._admit_spooled

    async def _counting(adapter, payload, path):
        calls.append(path.name)
        return await real(adapter, payload, path)

    monkeypatch.setattr(drain, "_admit_spooled", _counting)
    assert await drain._replay_home(gateway.adapter, None, gateway.spool) == (0, True)
    assert calls == ["pending-1.json"], "a refused spool holds back that session's later spools"
    assert first.exists() and later.exists() and _api_admissions(gateway.authority) == []


@pytest.mark.asyncio
async def test_each_served_home_replays_inside_its_own_profile_scope(tmp_path, monkeypatch):
    from gateway.platforms.api_server import _api_request_profile
    from hermes_constants import get_hermes_home
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    worker = tmp_path / "profiles" / "worker"
    _write(tmp_path / "pending_messages", "pending-a.json", _payload(session_id="root-s"))
    _write(worker / "pending_messages", "pending-b.json", _payload(session_id="worker-s"))
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda config: [("default", tmp_path), ("worker", worker)])
    seen = []

    class _Adapter:
        def _profile_scope(self, name):
            from contextlib import contextmanager
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override

            @contextmanager
            def _scope():
                token = set_hermes_home_override(str(worker if name == "worker" else tmp_path))
                try:
                    yield
                finally:
                    reset_hermes_home_override(token)
            return _scope()

    async def _admit(adapter, payload, path):
        seen.append((_api_request_profile.get(), str(get_hermes_home()), payload["data"]["session_id"]))
        return True

    monkeypatch.setattr(drain, "_admit_spooled", _admit)
    runner = SimpleNamespace(config=GatewayConfig(multiplex_profiles=True), adapters={Platform.API_SERVER: _Adapter()},
                             _startup_should_abort=lambda: False)
    assert await drain.replay_draining_session_chats(runner) == 2
    assert seen == [(None, str(tmp_path), "root-s"), ("worker", str(worker), "worker-s")]


@pytest.mark.asyncio
async def test_bot_chat_spool_replayed_twice_is_one_native_delivery(gateway, tmp_path):
    """The Bot Chat branch names the spool's stable delivery id: a repeat replay is the same delivery."""
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    owner = Principal('uid:1000', 'default', frozenset({'session:create', 'session:read', 'session:submit'}), 'native')
    chat = create_local_session(gateway.authority, owner, {'request_id': 'bot', 'source': 'gui', 'cwd': str(tmp_path),
                                                            'model': 'fixture', 'toolsets': []})
    title_new_session(gateway.authority, chat, 'Bot Chat')
    path = _write(gateway.spool, "pending-bot.json", _payload(session_id=chat.session_id, message_id="b" * 32))
    saved = tmp_path / "saved-bot.json"
    shutil.copy(path, saved)
    assert await drain.replay_draining_session_chats(gateway.runner) == 1
    shutil.copy(saved, path)  # a crash before unlink, or a second replay of the same entry
    assert await drain.replay_draining_session_chats(gateway.runner) == 1
    rows = gateway.authority.db._read_all(
        "SELECT request_id FROM session_admissions WHERE target_session_id=?", (chat.session_id,))
    assert len(rows) == 1 and not path.exists()
    delivery = gateway.authority.db.get_session(chat.session_id)  # still the Bot Chat; no API binding
    assert delivery is not None and _api_admissions(gateway.authority) == []
