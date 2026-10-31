"""G05-G08 under session authority: the verified caller rides both admission branches.

The canonical Bot Chat branch (``admit_peer_turn`` -> ``session_bot.deliver``) and the general API
admission (``admit_api_turn_async``) both record the server-verified author and label, so a queued
turn executes with them; a body author is never trusted. Peer ping never admits anything.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.session_contract import Principal
from tests.gateway.test_api_server_peer_callers import (
    PRIMARY, SHELDON, SHELDON_AUTHOR, SPOOFED, _app, _bearer, _ping)
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def peer(tmp_path, monkeypatch):
    from gateway import run
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    from hermes_state import SessionDB

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('API_SERVER_CALLER_KEYS', raising=False)
    (tmp_path / '.env').write_text(f"API_SERVER_CALLER_KEYS={json.dumps({'sheldon': SHELDON})}\n")
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    monkeypatch.setattr('tools.bot_mode_dm._LIVE_WAIT_SECONDS', 2.0)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / 'state.db')
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='fixture')
    owner = Principal('uid:1000', 'default', frozenset({'session:create', 'session:read', 'session:submit'}), 'native')
    chat = create_local_session(authority, owner, {'request_id': 'bot', 'source': 'gui', 'cwd': str(tmp_path),
                                                    'model': 'fixture', 'toolsets': []})
    title_new_session(authority, chat, 'Bot Chat')
    authority.db.create_session('scratch', 'api_server')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': PRIMARY}))
    adapter.gateway_runner = runner
    adapter._session_db = authority.db
    runner.adapters[Platform.API_SERVER] = adapter
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    executed = []

    async def execute(authority, ref, row):
        executed.append((ref.session_id, row['payload']['text'],
                         row['payload'].get('local_automation_v1', {}).get('turn_author')))
        return 'pong'

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            yield SimpleNamespace(cli=cli, authority=authority, chat=chat.session_id, executed=executed,
                                  adapter=adapter, home=tmp_path)
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        store._db.close()


def _api_rows(authority):
    return [(row['target_session_id'], json.loads(row['payload_json'])) for row in authority.db._read_all(
        "SELECT target_session_id, payload_json FROM session_admissions WHERE principal_id='api'")]


@pytest.mark.asyncio
async def test_session_chat_into_bot_chat_executes_with_the_verified_caller(peer):
    resp = await peer.cli.post(f"/api/sessions/{peer.chat}/chat", headers=_bearer(SHELDON),
                               json={"message": "ping", "author": SPOOFED})
    assert resp.status == 200, await resp.text()
    assert peer.executed == [(peer.chat, "[verified caller: sheldon] ping", SHELDON_AUTHOR)]


@pytest.mark.asyncio
async def test_run_into_bot_chat_executes_with_the_verified_caller(peer):
    resp = await peer.cli.post("/v1/runs", headers=_bearer(SHELDON),
                               json={"input": "ping", "session_id": peer.chat, "author": SPOOFED})
    assert resp.status == 202, await resp.text()
    await async_wait_until(lambda: peer.executed, timeout=10, what="the Bot Chat FIFO ran the run")
    assert peer.executed == [(peer.chat, "[verified caller: sheldon] ping", SHELDON_AUTHOR)]


@pytest.mark.asyncio
async def test_run_into_api_session_is_admitted_with_the_verified_caller(peer, monkeypatch):
    monkeypatch.setattr(peer.authority, "_schedule", lambda ref: None)  # admission only; nothing runs
    resp = await peer.cli.post("/v1/runs", headers=_bearer(SHELDON), json={
        "input": "status?", "session_id": "scratch", "author": SPOOFED})
    assert resp.status == 202, await resp.text()
    [(target, payload)] = _api_rows(peer.authority)
    assert target == "scratch"
    assert payload["text"] == "[verified caller: sheldon] status?"
    assert payload["api_turn_v1"]["turn_author"] == SHELDON_AUTHOR


@pytest.mark.asyncio
async def test_peer_ping_admits_nothing_under_authority(peer):
    before = peer.authority.db._read_all("SELECT COUNT(*) AS n FROM session_admissions")[0]["n"]
    resp = await peer.cli.post("/v1/peer/ping", headers=_bearer(SHELDON), json=_ping("auth-1"))
    assert resp.status == 201, await resp.text()
    replay = await peer.cli.post("/v1/peer/ping", headers=_bearer(SHELDON), json=_ping("auth-1"))
    assert replay.status == 200
    conflict = await peer.cli.post("/v1/peer/ping", headers=_bearer(SHELDON), json=_ping(
        "auth-1", payload_sha256=hashlib.sha256(b"other").hexdigest()))
    assert conflict.status == 409
    after = peer.authority.db._read_all("SELECT COUNT(*) AS n FROM session_admissions")[0]["n"]
    assert after == before and peer.executed == []
