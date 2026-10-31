"""gateway/fleet_message_bridge.py: the authority-owned fleet drain boundary.

Real ``SessionDB`` stores and the real ``owner_scope`` (multiplexed ``_profile_runtime_scope``);
fakes only for the authority RPC (``authority_delivery``), its discovery and Firestore.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_state
from gateway import fleet_message_bridge as bridge_mod
from tests.tools.test_fleet_message_admission import FakeDoor
from tests.tools.test_fleet_message_drain import MemoryStore
from tests.tools.test_fleet_message_drain import msg as _fixed_clock_msg
from tools import bot_live_delivery as live
from tools import fleet_message_drain as fmd
from tools import fleet_message_spool as spool

ENABLED = "fleet_messages:\n  drain_on_turn_end: true\n  target: emulator\n  emulator_host: 127.0.0.1:1\n"


def _profile(root: Path, name: str, *, enabled: bool = True):
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(ENABLED if enabled else "model: {}\n", encoding="utf-8")
    db = hermes_state.SessionDB(home / "state.db")
    return SimpleNamespace(db=db, profile_id=str(home), retiring=False,
                           runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True)))


class _Registry(list):
    def serving(self):
        return list(self)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    root = tmp_path / ".hermes"
    root.mkdir()
    store = MemoryStore()
    door = FakeDoor()
    seen = []

    def deliver(home, params):
        seen.append((Path(home).resolve(), Path(get_hermes_home()).resolve(), params))
        return door(home, params)

    monkeypatch.setattr(fmd, "store_for", lambda config: store)
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: dict(
        profile_home=str(Path(h).resolve()), session_id="", canonical=True, lease_id="a", live_session_id=""))
    monkeypatch.setattr(live, "authority_delivery", deliver)
    return SimpleNamespace(root=root, store=store, door=door, seen=seen)


def msg(minutes_ago, **kw):
    """A doc dated against the real clock: the bridge drains with no injected ``now``."""
    import datetime

    doc = _fixed_clock_msg(minutes_ago, **kw)
    created = fmd.utcnow() - datetime.timedelta(minutes=minutes_ago)
    doc.update(created_at=fmd.rfc3339(created), updated_at=fmd.rfc3339(created),
               expires_at=fmd.rfc3339(created + datetime.timedelta(hours=24)))
    return doc


def _runner(*authorities):
    return SimpleNamespace(session_authorities=_Registry(authorities), config=SimpleNamespace(multiplex_profiles=True))


def _tick(runner, bridge=None):
    bridge = bridge or bridge_mod.FleetMessageBridge(runner)
    return asyncio.run(bridge.tick()), bridge


def _doc(name):
    return f"{name}-{uuid.uuid4().hex[:8]}"


def test_each_profile_admits_only_its_own_messages_under_its_own_scope(world):
    alpha, beta = _profile(world.root, "alpha"), _profile(world.root, "beta")
    a_doc, b_doc = _doc("for-alpha"), _doc("for-beta")
    world.store.seed(a_doc, msg(5, to="alpha", body="alpha body"))
    world.store.seed(b_doc, msg(5, to="beta", body="beta body"))
    results, _ = _tick(_runner(alpha, beta))
    assert set(results.values()) == {"admitted"}
    assert len(world.seen) == 2
    for called_home, scoped_home, params in world.seen:
        assert called_home == scoped_home  # admitted into the profile whose scope is bound
        owner = called_home.name
        assert params["profile"] == owner
        assert (params["id"] == spool.native_delivery_id(a_doc)) == (owner == "alpha")
        assert f"{owner} body" in params["message"]
    # Each execution claim lives under its own recipient home only.
    assert spool.execution(Path(alpha.profile_id), a_doc) and not spool.execution(Path(alpha.profile_id), b_doc)
    assert spool.execution(Path(beta.profile_id), b_doc) and not spool.execution(Path(beta.profile_id), a_doc)


def test_profile_without_opt_in_is_never_drained(world):
    alpha, beta = _profile(world.root, "alpha"), _profile(world.root, "beta", enabled=False)
    b_doc = _doc("for-beta")
    world.store.seed(b_doc, msg(5, to="beta"))
    results, _ = _tick(_runner(alpha, beta))
    assert results[str(Path(beta.profile_id).resolve())] == "disabled"
    assert world.seen == []
    assert {to for to, _ in world.store.queries} == {"alpha"}
    assert world.store.get(b_doc).fields["status"] == "queued"


def _bot_chat(authority):
    db = authority.db
    db.create_session("bot-chat", source="cli")
    db.set_session_title("bot-chat", "Bot Chat")
    return "bot-chat"


def test_pending_human_admission_has_priority(world):
    alpha = _profile(world.root, "alpha")
    sid = _bot_chat(alpha)
    alpha.db._execute_write(lambda conn: conn.execute(
        "INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, lineage_json,"
        " payload_json, payload_digest, intent, status) VALUES ('adm-h', 'human-1', 'user', ?, '{}', '{}', 'd',"
        " 'queue', 'queued')", (sid,)))
    doc = _doc("waiting")
    world.store.seed(doc, msg(5, to="alpha"))
    results, _ = _tick(_runner(alpha))
    assert list(results.values()) == ["busy"]
    assert world.seen == [] and world.store.get(doc).fields["status"] == "queued"


def test_live_turn_lease_holder_defers_the_fleet_message(world):
    alpha = _profile(world.root, "alpha")
    sid = _bot_chat(alpha)
    assert alpha.db.try_acquire_session_turn_lease(sid, "cli-writer", ttl_seconds=60)
    world.store.seed(_doc("waiting"), msg(5, to="alpha"))
    results, _ = _tick(_runner(alpha))
    assert list(results.values()) == ["busy"] and world.seen == []
    assert alpha.db.session_turn_lease_holder(sid) == "cli-writer"  # the diagnostic never took it


def test_fleet_yields_one_boundary_after_two_consecutive_messages(world):
    alpha = _profile(world.root, "alpha")
    for i in range(3):
        world.store.seed(f"run-{i}", msg(10 - i, to="alpha"))
    runner = _runner(alpha)
    bridge = bridge_mod.FleetMessageBridge(runner)
    outcomes = []
    for _ in range(4):
        outcomes.append(asyncio.run(bridge.tick())[str(Path(alpha.profile_id).resolve())])
        for _, _, params in world.seen:
            if world.door.records[params["id"]]["status"] != "settled":
                world.door.settle(params["id"], status="settled")
    assert outcomes == ["admitted", "admitted", "yielded", "admitted"]
    assert [p["id"] for _, _, p in world.seen] == [spool.native_delivery_id(f"run-{i}") for i in range(3)]


def test_watcher_ticks_then_stops_on_cancellation(monkeypatch):
    ticks = []

    async def fake_tick(self):
        ticks.append(1)
        return {}

    monkeypatch.setattr(bridge_mod.FleetMessageBridge, "tick", fake_tick)

    async def run():
        task = asyncio.create_task(bridge_mod.fleet_drain_watcher(SimpleNamespace(), interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(ticks) >= 2


def test_bidirectional_recipients_each_receive_only_their_own_message(world):
    """E5 bidirectional recipient proof: alpha -> beta and beta -> alpha through the sender spool;
    each recipient's own authority admits exactly the message addressed to it, under its scope."""
    alpha, beta = _profile(world.root, "alpha"), _profile(world.root, "beta")
    a_home, b_home = Path(alpha.profile_id), Path(beta.profile_id)
    down = lambda *a, **k: 1  # registry write fails: local-first delivery must not need it
    to_beta = spool.spool_busy_dm(b_home, sender="alpha", recipient="beta", body="alpha to beta",
                                  writer=down, reader=lambda _m: None)["message_id"]
    to_alpha = spool.spool_busy_dm(a_home, sender="beta", recipient="alpha", body="beta to alpha",
                                   writer=down, reader=lambda _m: None)["message_id"]
    results, _ = _tick(_runner(alpha, beta))
    assert set(results.values()) == {"admitted"}
    by_home = {called.name: (scoped.name, params) for called, scoped, params in world.seen}
    assert set(by_home) == {"alpha", "beta"}
    assert by_home["beta"][0] == "beta" and by_home["beta"][1]["id"] == spool.native_delivery_id(to_beta)
    assert by_home["alpha"][0] == "alpha" and by_home["alpha"][1]["id"] == spool.native_delivery_id(to_alpha)
    assert "alpha to beta" in by_home["beta"][1]["message"] and "beta to alpha" in by_home["alpha"][1]["message"]
    assert by_home["beta"][1]["author"]["id"] == "bot:alpha" and by_home["alpha"][1]["author"]["id"] == "bot:beta"
