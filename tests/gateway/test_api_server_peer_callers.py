"""Fleet API caller contract (G05-G08): verified named callers, supplemental keys, aliases, ping.

Every case runs through real aiohttp request handling against the production route table and
middlewares (``connect()``'s wiring) with dummy keys in a temp HERMES_HOME. A verified caller only
labels the turn: native room grants, run ownership and session admission stay authoritative.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope as ss
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    body_limit_middleware,
    cors_middleware,
    security_headers_middleware,
)
from gateway.platforms.api_server_caller_keys import (
    bind_verified_message,
    caller_request_context,
    mark_request,
    parse_caller_keys,
    verified_caller,
)

PRIMARY = "primary-listener-key-0001"
SHELDON = "sheldon-caller-key-00000001"
ROSIE = "rosie-caller-key-000000002"
WORKER_PRIMARY = "worker-profile-key-000001"
WORKER_EXTRA = "worker-extra-key-00000001"
WORKER_CALLER = "worker-named-caller-key-01"
SHELDON_AUTHOR = {"id": "api-caller:sheldon", "name": "sheldon", "is_bot": True}
SHARED_AUTHOR = {"id": "api-caller:host-key (shared)", "name": "host-key (shared)", "is_bot": True}
SPOOFED = {"id": "bot:admin", "name": "admin", "is_bot": True}


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _app(adapter):
    """The listener ``connect()`` builds: its middlewares and every route plus its /p/ mirror."""
    mws = [mw for mw in (adapter._make_profile_prefix_middleware(), cors_middleware, body_limit_middleware,
                         security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
        app.router.add_route(method, f"/p/{{profile}}{path}", handler)
    return app


class _CapturingAgent:
    session_prompt_tokens = session_completion_tokens = session_total_tokens = 0

    def __init__(self, calls, session_id="s1"):
        self.calls, self.session_id = calls, session_id

    def run_conversation(self, user_message, conversation_history, task_id, **kwargs):
        self.calls.append({"user_message": user_message, **kwargs})
        return {"final_response": "ok", "session_id": self.session_id}


@pytest.fixture(autouse=True)
def _multiplex_off():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def host_home(tmp_path, monkeypatch):
    """Temp host root whose own .env declares one named caller (never a live home)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("API_SERVER_CALLER_KEYS", raising=False)
    (tmp_path / ".env").write_text(
        f"API_SERVER_CALLER_KEYS={json.dumps({'sheldon': SHELDON})}\n", encoding="utf-8")
    return tmp_path


@pytest_asyncio.fixture
async def listener(host_home):
    from hermes_state import SessionDB
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PRIMARY}))
    db = SessionDB(db_path=host_home / "state.db")
    adapter._session_db = db
    db.create_session("s1", "api_server")
    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            yield SimpleNamespace(cli=cli, adapter=adapter, db=db, home=host_home)
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        db.close()


# -- G06: key parsing and request identity -------------------------------------------------


@pytest.mark.parametrize("raw", [
    '{"a": "%s", "a": "%s"}' % (SHELDON, ROSIE),          # duplicate caller name
    json.dumps({"a": SHELDON, "b": SHELDON}),               # one key, two identities
    json.dumps({"bad name": SHELDON}),                      # display text is not a name
    json.dumps({"a": "short"}),                             # below the 16-char floor
    json.dumps({f"c{i}": f"{SHELDON}{i:02d}" for i in range(17)}),  # over the 16-caller bound
    json.dumps([SHELDON]),                                  # not an object
], ids=["duplicate-name", "duplicate-key", "invalid-name", "short-key", "too-many", "not-object"])
def test_caller_map_fails_closed_as_a_whole(raw):
    assert parse_caller_keys(raw) is None


def test_caller_map_parses_valid_names():
    assert parse_caller_keys(json.dumps({"sheldon": SHELDON, "rosie": ROSIE})) == (
        ("sheldon", SHELDON), ("rosie", ROSIE))
    assert parse_caller_keys("") == ()


@pytest.mark.asyncio
async def test_request_context_resets_identity_between_requests():
    seen = []

    class _Handler:
        @caller_request_context
        async def handle(self, request):
            seen.append(verified_caller())
            mark_request(request)
            return seen[-1]

    handler = _Handler()
    await handler.handle("sheldon")
    await handler.handle(None)
    assert seen == [None, None]
    assert verified_caller() is None


# -- G05/G06: bearer policy through the listener ---------------------------------------------


@pytest.mark.asyncio
async def test_named_caller_key_authenticates_and_unknown_key_is_refused(listener):
    assert (await listener.cli.get("/v1/capabilities", headers=_bearer(SHELDON))).status == 200
    assert (await listener.cli.get("/v1/capabilities", headers=_bearer(PRIMARY))).status == 200
    assert (await listener.cli.get("/v1/capabilities", headers=_bearer(ROSIE))).status == 401
    assert (await listener.cli.get("/v1/capabilities")).status == 401


@pytest.mark.asyncio
async def test_session_chat_binds_server_verified_caller_over_asserted_author(listener, monkeypatch):
    calls = []
    monkeypatch.setattr(listener.adapter, "_create_agent", lambda **kw: _CapturingAgent(calls))
    resp = await listener.cli.post("/api/sessions/s1/chat", headers=_bearer(SHELDON),
                                   json={"message": "hello", "author": SPOOFED})
    assert resp.status == 200, await resp.text()
    # One server-bound label: the facade's idempotent re-bind in _run_agent adds no second prefix.
    assert calls == [{"user_message": "[verified caller: sheldon] hello", "turn_author": SHELDON_AUTHOR}]


@pytest.mark.asyncio
async def test_shared_key_is_labelled_where_named_callers_exist(listener, monkeypatch):
    calls = []
    monkeypatch.setattr(listener.adapter, "_create_agent", lambda **kw: _CapturingAgent(calls))
    resp = await listener.cli.post("/api/sessions/s1/chat", headers=_bearer(PRIMARY),
                                   json={"message": "[verified caller: sheldon] hi", "author": SHELDON_AUTHOR})
    assert resp.status == 200, await resp.text()
    assert calls == [{"user_message": "[verified caller: host-key (shared)] [verified caller: sheldon] hi",
                      "turn_author": SHARED_AUTHOR}]


@pytest.mark.asyncio
async def test_scope_without_named_callers_keeps_native_author_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("API_SERVER_CALLER_KEYS", raising=False)
    from hermes_state import SessionDB
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PRIMARY}))
    adapter._session_db = db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", "api_server")
    calls = []
    monkeypatch.setattr(adapter, "_create_agent", lambda **kw: _CapturingAgent(calls))
    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post("/api/sessions/s1/chat", headers=_bearer(PRIMARY),
                                  json={"message": "hello", "author": SPOOFED})
            assert resp.status == 200, await resp.text()
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        db.close()
    assert calls == [{"user_message": "hello", "turn_author": SPOOFED}]


# -- G08: /v1/runs input extraction ---------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_bind_server_verified_caller_before_execution(listener, monkeypatch):
    calls = []
    monkeypatch.setattr(listener.adapter, "_create_agent", lambda **kw: _CapturingAgent(calls, "run"))
    resp = await listener.cli.post("/v1/runs", headers=_bearer(SHELDON),
                                   json={"input": "status?", "author": SPOOFED})
    assert resp.status == 202, await resp.text()
    run_id = (await resp.json())["run_id"]
    for _ in range(100):
        status = await (await listener.cli.get(f"/v1/runs/{run_id}", headers=_bearer(SHELDON))).json()
        if status.get("status") == "completed":
            break
        await asyncio.sleep(0.02)
    assert calls == [{"user_message": "[verified caller: sheldon] status?", "turn_author": SHELDON_AUTHOR}]


@pytest.mark.asyncio
async def test_session_chat_admission_carries_verified_author_under_authority(listener, monkeypatch):
    """Under session authority the bound author/text is what the canonical API admission records."""
    from gateway import session_api_turn
    from gateway.session_authority import initialize_session_authority
    from gateway.session import SessionStore
    store = SessionStore(listener.home / "sessions", GatewayConfig())
    store._db = listener.db
    runner = SimpleNamespace(session_store=store, _session_db=listener.db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None)
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="fixture")
    from gateway.config import Platform
    runner.adapters[Platform.API_SERVER] = listener.adapter
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    listener.adapter.gateway_runner = runner
    listener.adapter._session_db = authority.db

    async def _observed(admitted, **kwargs):
        return {"final_response": "ok", "session_id": "s1"}, {}

    monkeypatch.setattr(session_api_turn, "observe_api_turn", _observed)
    resp = await listener.cli.post("/api/sessions/s1/chat", headers=_bearer(SHELDON),
                                   json={"message": "hello", "author": SPOOFED})
    assert resp.status == 200, await resp.text()
    rows = authority.db._read_all(
        "SELECT payload_json FROM session_admissions WHERE principal_id='api' AND target_session_id='s1'")
    payloads = [json.loads(row["payload_json"]) for row in rows]
    assert [(p["text"], p["api_turn_v1"]["turn_author"]) for p in payloads] == [
        ("[verified caller: sheldon] hello", SHELDON_AUTHOR)]


# -- G05: routed profiles: own key, extra keys, host-key opt-in, aliases ------------------


@pytest_asyncio.fixture
async def routed(tmp_path, monkeypatch):
    """A multiplexed listener serving profile ``worker`` (display name "Web Reviewer")."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("API_SERVER_CALLER_KEYS", raising=False)
    worker = tmp_path / "profiles" / "worker"
    other = tmp_path / "profiles" / "other"
    for home in (worker, other):
        home.mkdir(parents=True)
    (worker / ".env").write_text(
        f"API_SERVER_KEY={WORKER_PRIMARY}\nAPI_SERVER_EXTRA_KEYS={json.dumps([WORKER_EXTRA])}\n"
        f"API_SERVER_CALLER_KEYS={json.dumps({'rosie': WORKER_CALLER})}\n", encoding="utf-8")
    (worker / "profile.yaml").write_text("display_name: Web Reviewer\nprevious_names: [reviewer]\n",
                                         encoding="utf-8")
    (other / "profile.yaml").write_text("display_name: Shared Name\n", encoding="utf-8")
    homes = {"worker": worker, "other": other}
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve",
                        lambda multiplex: [("default", tmp_path), *homes.items()])
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: homes.get(name, tmp_path / name))
    ss.set_multiplex_active(True)

    def build(extra=None):
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PRIMARY, **(extra or {})}))
        adapter.gateway_runner = SimpleNamespace(config=GatewayConfig(multiplex_profiles=True))
        return adapter

    adapters = []

    async def client(extra=None):
        adapter = build(extra)
        adapters.append(adapter)
        cli = TestClient(TestServer(_app(adapter)))
        await cli.start_server()
        clients.append(cli)
        return cli

    clients = []
    try:
        yield SimpleNamespace(client=client, homes=homes, root=tmp_path)
    finally:
        for cli in clients:
            await cli.close()
        for adapter in adapters:
            adapter._response_store.close()
            adapter._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_routed_profile_key_matrix(routed):
    cli = await routed.client()
    path = "/p/worker/v1/capabilities"
    assert (await cli.get(path, headers=_bearer(WORKER_PRIMARY))).status == 200
    assert (await cli.get(path, headers=_bearer(WORKER_EXTRA))).status == 200  # supplemental key
    assert (await cli.get(path, headers=_bearer(WORKER_CALLER))).status == 200  # profile named caller
    assert (await cli.get(path, headers=_bearer(PRIMARY))).status == 401        # host key: no opt-in
    # Supplemental and profile caller keys never open the default listener or another profile.
    assert (await cli.get("/v1/capabilities", headers=_bearer(WORKER_EXTRA))).status == 401
    assert (await cli.get("/v1/capabilities", headers=_bearer(WORKER_CALLER))).status == 401
    (routed.homes["other"] / ".env").write_text("API_SERVER_KEY=other-profile-key-0000001\n")
    assert (await cli.get("/p/other/v1/capabilities", headers=_bearer(WORKER_EXTRA))).status == 401
    assert (await cli.get("/p/other/v1/capabilities", headers=_bearer(WORKER_CALLER))).status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in, status", [(True, 200), ("true", 401), (1, 401)])
async def test_host_key_reaches_profiles_only_on_literal_opt_in(routed, opt_in, status):
    cli = await routed.client({"accept_host_key_for_profiles": opt_in})
    assert (await cli.get("/p/worker/v1/capabilities", headers=_bearer(PRIMARY))).status == status


@pytest.mark.asyncio
async def test_invalid_supplemental_config_grants_no_extra_access(routed):
    env = routed.homes["worker"] / ".env"
    env.write_text(env.read_text() + f"API_SERVER_EXTRA_KEYS={json.dumps([WORKER_EXTRA, 'short'])}\n")
    cli = await routed.client()
    assert (await cli.get("/p/worker/v1/capabilities", headers=_bearer(WORKER_EXTRA))).status == 401
    assert (await cli.get("/p/worker/v1/capabilities", headers=_bearer(WORKER_PRIMARY))).status == 200


@pytest.mark.asyncio
async def test_conflicting_identities_across_scopes_fail_closed(routed):
    (routed.root / ".env").write_text(
        f"API_SERVER_CALLER_KEYS={json.dumps({'sheldon': WORKER_CALLER})}\n", encoding="utf-8")
    cli = await routed.client({"accept_host_key_for_profiles": True})
    # The same token names 'sheldon' (host root) and 'rosie' (profile): no identity, no access.
    assert (await cli.get("/p/worker/v1/capabilities", headers=_bearer(WORKER_CALLER))).status == 401


@pytest.mark.asyncio
async def test_unique_alias_routes_to_canonical_profile_and_ambiguity_is_refused(routed):
    (routed.homes["other"] / "profile.yaml").write_text("display_name: Web Reviewer\n", encoding="utf-8")
    cli = await routed.client()
    # "reviewer" (a previous name) is unique to worker; auth is scoped to the canonical profile.
    assert (await cli.get("/p/reviewer/v1/capabilities", headers=_bearer(WORKER_PRIMARY))).status == 200
    assert (await cli.get("/p/reviewer/v1/capabilities", headers=_bearer(PRIMARY))).status == 401
    ping = await cli.post("/p/reviewer/v1/peer/ping", headers=_bearer(WORKER_PRIMARY), json=_ping("alias-1"))
    assert ping.status == 201 and (routed.homes["worker"] / "peer_ping_receipts.db").is_file()
    # "web-reviewer" now names two served profiles: refused, never a guess.
    assert (await cli.get("/p/web-reviewer/v1/capabilities", headers=_bearer(WORKER_PRIMARY))).status == 404
    # An exact served id wins without consulting aliases; an unknown prefix is a 404.
    assert (await cli.get("/p/other/v1/capabilities", headers=_bearer(WORKER_PRIMARY))).status == 401
    assert (await cli.get("/p/ghost/v1/capabilities", headers=_bearer(WORKER_PRIMARY))).status == 404


# -- G07: no-agent peer ping -------------------------------------------------------------------


def _ping(key="probe-1", **overrides):
    body = {"idempotency_key": key, "nonce": "n-1", "payload_sha256": hashlib.sha256(b"x").hexdigest(),
            "sender_node": "sheldon", "sent_at": "2026-10-26T08:00:00Z"}
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_peer_ping_is_idempotent_durable_and_runs_no_agent(listener, monkeypatch):
    def _no_agent(**kwargs):
        raise AssertionError("peer ping must never create an agent")

    monkeypatch.setattr(listener.adapter, "_create_agent", _no_agent)
    cli = listener.cli
    first = await cli.post("/v1/peer/ping", headers=_bearer(SHELDON), json=_ping())
    assert first.status == 201, await first.text()
    receipt = await first.json()
    again = await cli.post("/v1/peer/ping", headers=_bearer(SHELDON), json=_ping())
    assert again.status == 200 and (await again.json())["received_at"] == receipt["received_at"]
    conflict = await cli.post("/v1/peer/ping", headers=_bearer(SHELDON),
                              json=_ping(payload_sha256=hashlib.sha256(b"y").hexdigest()))
    assert conflict.status == 409
    status = await cli.get("/v1/peer/ping/probe-1", headers=_bearer(PRIMARY))
    assert status.status == 200 and (await status.json())["sender_node"] == "sheldon"
    assert (listener.home / "peer_ping_receipts.db").is_file()
    assert listener.db._read_all("SELECT 1 FROM messages") == []


@pytest.mark.asyncio
async def test_peer_ping_requires_auth_and_never_takes_the_no_key_bypass(listener, tmp_path):
    assert (await listener.cli.post("/v1/peer/ping", json=_ping())).status == 401
    assert (await listener.cli.get("/v1/peer/ping/probe-1")).status == 401
    keyless = APIServerAdapter(PlatformConfig(enabled=True))
    try:
        async with TestClient(TestServer(_app(keyless))) as cli:
            assert (await cli.post("/v1/peer/ping", json=_ping())).status == 401
            assert (await cli.get("/v1/capabilities")).status == 200  # the native test bypass itself
    finally:
        keyless._response_store.close()
        keyless._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_peer_ping_receipts_live_in_the_routed_profile_home(routed):
    cli = await routed.client()
    resp = await cli.post("/p/worker/v1/peer/ping", headers=_bearer(WORKER_PRIMARY), json=_ping("w-1"))
    assert resp.status == 201, await resp.text()
    assert (routed.homes["worker"] / "peer_ping_receipts.db").is_file()
    assert not (routed.root / "peer_ping_receipts.db").exists()
    caps = await (await cli.get("/p/worker/v1/capabilities", headers=_bearer(WORKER_PRIMARY))).json()
    assert caps["features"]["peer_ping"]["supported"] is True
    assert caps["endpoints"]["peer_ping"] == {"method": "POST", "path": "/v1/peer/ping"}


def test_bind_verified_message_is_idempotent_for_text_and_parts():
    from gateway.platforms.api_server_caller_keys import _verified_caller
    token = _verified_caller.set("sheldon")
    try:
        once = bind_verified_message("hi", None)
        assert bind_verified_message(*once) == once
        parts = bind_verified_message([{"type": "text", "text": "hi"}], None)
        assert parts[0][0] == {"type": "text", "text": "[verified caller: sheldon] "}
        assert bind_verified_message(*parts) == parts
    finally:
        _verified_caller.reset(token)
    assert bind_verified_message("hi", SPOOFED) == ("hi", SPOOFED)


@pytest.mark.asyncio
async def test_profile_launched_listener_reads_only_its_own_caller_map(tmp_path, monkeypatch):
    """Profiles are islands: a gateway launched at a profile home never inherits the root's callers."""
    worker = tmp_path / "profiles" / "worker"
    worker.mkdir(parents=True)
    (tmp_path / ".env").write_text(f"API_SERVER_CALLER_KEYS={json.dumps({'sheldon': SHELDON})}\n")
    (worker / ".env").write_text(f"API_SERVER_CALLER_KEYS={json.dumps({'rosie': ROSIE})}\n")
    monkeypatch.setenv("HERMES_HOME", str(worker))
    monkeypatch.delenv("API_SERVER_CALLER_KEYS", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": PRIMARY}))
    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            assert (await cli.get("/v1/capabilities", headers=_bearer(ROSIE))).status == 200
            assert (await cli.get("/v1/capabilities", headers=_bearer(SHELDON))).status == 401
    finally:
        adapter._response_store.close()
        adapter._run_idempotency_store.close()


@pytest.mark.asyncio
async def test_concurrent_callers_keep_their_own_identity(listener, monkeypatch):
    """Two overlapping turns (named caller, shared key): neither inherits the other's identity."""
    import threading
    listener.db.create_session("s2", "api_server")
    calls, barrier = [], threading.Barrier(2, timeout=10)

    class _Overlapping(_CapturingAgent):
        def run_conversation(self, user_message, conversation_history, task_id, **kwargs):
            barrier.wait()  # both executor turns are in flight at once
            return super().run_conversation(user_message, conversation_history, task_id, **kwargs)

    monkeypatch.setattr(listener.adapter, "_create_agent", lambda **kw: _Overlapping(calls))
    first, second = await asyncio.gather(
        listener.cli.post("/api/sessions/s1/chat", headers=_bearer(SHELDON), json={"message": "one"}),
        listener.cli.post("/api/sessions/s2/chat", headers=_bearer(PRIMARY), json={"message": "two"}))
    assert first.status == second.status == 200
    assert sorted((c["user_message"], c["turn_author"]["name"]) for c in calls) == [
        ("[verified caller: host-key (shared)] two", "host-key (shared)"),
        ("[verified caller: sheldon] one", "sheldon")]
