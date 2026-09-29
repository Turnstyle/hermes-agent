"""The peer ping receipt must never enter a bot/session turn."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms import api_server_peer_ping as ping


def _body(key="ticket-1", nonce="nonce-1"):
    return {"idempotency_key": key, "nonce": nonce,
            "payload_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
            "sender_node": "turnerbook", "sent_at": "2026-09-29T01:30:00Z"}


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-peer-key-123456"}))


def _app(adapter):
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        if path.startswith("/v1/peer/ping") or path == "/v1/capabilities":
            app.router.add_route(method, path, handler)
    return app


@pytest.mark.asyncio
async def test_create_replay_conflict_readback_and_no_agent(adapter):
    headers = {"Authorization": "Bearer test-peer-key-123456"}
    body = _body()
    # Any bot/session/LLM entry point touched makes this test fail.
    forbidden = ("_run_agent", "_create_agent", "_ensure_session_db",
                 "_admit_to_live_bot_chat", "_queue_busy_peer_dm")
    with patch.multiple(adapter, **{name: None for name in forbidden}):
        async with TestClient(TestServer(_app(adapter))) as cli:
            first = await cli.post("/v1/peer/ping", json=body, headers=headers)
            assert first.status == 201, await first.text()
            created = await first.json()
            assert created["received_at"]
            replay = await cli.post("/v1/peer/ping", json={**body, "sent_at": "later"}, headers=headers)
            assert replay.status == 200
            assert await replay.json() == created
            conflict = await cli.post("/v1/peer/ping", json=_body(nonce="different"), headers=headers)
            assert conflict.status == 409
            readback = await cli.get("/v1/peer/ping/ticket-1", headers=headers)
            assert readback.status == 200
            assert await readback.json() == created
            absent = await cli.get("/v1/peer/ping/missing", headers=headers)
            assert absent.status == 404


@pytest.mark.asyncio
async def test_auth_body_limit_and_feature(adapter):
    headers = {"Authorization": "Bearer test-peer-key-123456"}
    async with TestClient(TestServer(_app(adapter))) as cli:
        assert (await cli.post("/v1/peer/ping", json=_body())).status == 401
        assert (await cli.get("/v1/peer/ping/ticket-1")).status == 401
        assert (await cli.post("/v1/peer/ping", data=b"x" * (ping.MAX_BODY_BYTES + 1), headers=headers)).status == 413
        caps = await cli.get("/v1/capabilities", headers=headers)
        feature = (await caps.json())["features"]["peer_ping"]
        assert feature["supported"] is True and feature["durable"] is True
        assert feature["retention_seconds"] == 7 * 86400


@pytest.mark.asyncio
async def test_rate_limit_and_expiry(adapter, monkeypatch):
    headers = {"Authorization": "Bearer test-peer-key-123456"}
    monkeypatch.setattr(ping, "PER_SENDER_PER_MINUTE", 1)
    async with TestClient(TestServer(_app(adapter))) as cli:
        assert (await cli.post("/v1/peer/ping", json=_body(), headers=headers)).status == 201
        assert (await cli.post("/v1/peer/ping", json=_body(), headers=headers)).status == 200
        second = {**_body(key="ticket-2"), "sender_node": "spoofed-other-node"}
        assert (await cli.post("/v1/peer/ping", json=second, headers=headers)).status == 429
        monkeypatch.setattr(ping, "RETENTION_SECONDS", -1)
        assert (await cli.get("/v1/peer/ping/ticket-1", headers=headers)).status == 404


@pytest.mark.asyncio
async def test_restart_readback_and_full_key_contract(adapter):
    headers = {"Authorization": "Bearer test-peer-key-123456"}
    key = "route/with?reserved#chars"
    async with TestClient(TestServer(_app(adapter))) as cli:
        first = await cli.post("/v1/peer/ping", json=_body(key=key), headers=headers)
        assert first.status == 201, await first.text()
    replacement = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-peer-key-123456"}))
    from urllib.parse import quote
    async with TestClient(TestServer(_app(replacement))) as cli:
        readback = await cli.get("/v1/peer/ping/" + quote(key, safe=""), headers=headers)
        assert readback.status == 200, await readback.text()
        assert (await readback.json())["idempotency_key"] == key


@pytest.mark.asyncio
async def test_no_key_gateway_refuses_ping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": ""}))
    async with TestClient(TestServer(_app(adapter))) as cli:
        assert (await cli.post("/v1/peer/ping", json=_body())).status == 401


def test_concurrent_create_is_one_durable_record(tmp_path):
    path = tmp_path / "peer_ping_receipts.db"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ping._post(path, _body(), "remote:127.0.0.1"), range(2)))
    assert sorted(status for status, _ in results) == [200, 201]
    assert results[0][1] == results[1][1]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM peer_ping").fetchone()[0] == 1
