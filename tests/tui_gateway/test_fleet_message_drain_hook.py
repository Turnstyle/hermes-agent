"""Gateway wiring of the fleet_messages_v1 turn-end drain (tui_gateway ``_drain_fleet_messages_once``).

Proves: the drain runs from the post-turn follow-ups only (never from the idle poller), claims the
oldest queued doc once per turn end, hands it to the Bot Chat as the next turn, records the turn's
outcome on the doc, and gives an unstarted claim back instead of losing it.
"""
from __future__ import annotations

import contextlib
import dis
import queue
import threading
import time
import types
from types import SimpleNamespace

import pytest

from tests.tools.test_fleet_message_drain import NOW, MemoryStore, msg
from tools import fleet_message_drain as fmd

CONFIG = fmd.DrainConfig(target="emulator", emulator_host="127.0.0.1:1", limit=10, max_attempts=3)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tui_gateway import server
    from tools import bot_live_delivery
    from tools.process_registry import process_registry

    home = tmp_path / "profiles" / "tb-king"
    home.mkdir(parents=True)
    store = MemoryStore()
    monkeypatch.setattr(fmd, "drain_config", lambda cfg=None: CONFIG)
    monkeypatch.setattr(fmd, "store_for", lambda config: store)
    monkeypatch.setattr(fmd, "utcnow", lambda: NOW)
    monkeypatch.setattr(bot_live_delivery, "find_canonical_live_owner", lambda h: {
        "lease_id": "lease", "live_session_id": "live", "session_id": "chat"})
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda session, **kw: contextlib.nullcontext())
    monkeypatch.setattr(server, "_poll_bot_live_delivery_once", lambda sid, session: False)
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    submitted = []

    def submit(rid, sid, session, text, **kwargs):
        # Stand-in for a real turn: record, settle through the receipt, release the session.
        submitted.append(SimpleNamespace(rid=rid, text=text, kwargs=kwargs))
        kwargs["terminal_callback"](env_ns.outcome)
        with session["history_lock"]:
            session["running"] = False
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    session = {"history_lock": threading.RLock(), "running": False, "agent": object(), "session_key": "chat",
               "profile_home": str(home),
               "active_session_lease": SimpleNamespace(lease_id="lease", released=False)}
    env_ns = SimpleNamespace(server=server, store=store, session=session, submitted=submitted,
                             outcome={"status": "settled", "text": "ok"}, monkeypatch=monkeypatch)
    return env_ns


def turn_end(env):
    env.server._run_post_turn_followups("r", "live", env.session, {}, None)


def test_each_turn_end_delivers_the_next_queued_doc_once_in_order(env):
    for doc_id, minutes in (("m-2", 20), ("m-3", 5), ("m-1", 40)):
        env.store.seed(doc_id, msg(minutes))
    for _ in range(5):          # more turn ends than messages
        turn_end(env)
    assert [s.rid for s in env.submitted] == ["__fleet_msg__m-1", "__fleet_msg__m-2", "__fleet_msg__m-3"]
    assert [env.store.get(d).fields["status"] for d in ("m-1", "m-2", "m-3")] == ["done"] * 3
    assert env.store.reads == 5  # exactly one query per turn end
    first = env.submitted[0]
    assert "hello 40" in first.text and "m-1" in first.text
    assert first.kwargs["turn_author"] == {"id": "bot:tb-cndr", "name": "tb-cndr", "is_bot": True}
    assert env.session["running"] is False


def test_query_is_scoped_to_this_bot(env):
    env.store.seed("mine", msg(10))
    env.store.seed("theirs", msg(30, to="tb-cndr"))
    turn_end(env)
    turn_end(env)
    assert env.store.queries == [("tb-king", 10), ("tb-king", 10)]
    assert [s.rid for s in env.submitted] == ["__fleet_msg__mine"]
    assert env.store.get("theirs").fields["status"] == "queued"


def test_notify_wake_runs_as_a_wake_turn(env):
    env.store.seed("w-1", msg(5, kind="notify_wake", body="Kanban t_x is done"))
    turn_end(env)
    (sub,) = env.submitted
    assert "Kanban t_x is done" in sub.text
    assert sub.kwargs["turn_author"] is None
    assert sub.kwargs["display_metadata"] == {"notification_category": "result"}
    assert env.store.get("w-1").fields["status"] == "done"


def test_failed_turn_keeps_the_doc_and_requeues_it(env):
    env.store.seed("m-err", msg(5))
    env.outcome = {"status": "failed", "error": "provider overloaded"}
    turn_end(env)
    row = env.store.get("m-err")
    assert row.fields["status"] == "queued" and row.fields["attempts"] == 2
    assert "provider overloaded" in row.fields["last_error"]
    env.outcome = {"status": "settled", "text": "ok"}
    turn_end(env)
    assert env.store.get("m-err").fields["status"] == "done"
    assert len(env.submitted) == 2


def test_turn_that_cannot_start_is_recorded_not_lost(env):
    env.store.seed("m-ns", msg(5))
    env.monkeypatch.setattr(env.server, "_run_prompt_submit", lambda *a, **k: False)
    turn_end(env)
    row = env.store.get("m-ns")
    assert row.fields["status"] == "queued" and row.fields["attempts"] == 2
    assert "could not start" in row.fields["last_error"]
    assert env.session["running"] is False


def test_dispatch_exception_is_recorded_not_lost(env):
    env.store.seed("m-ex", msg(5))

    def boom(*a, **k):
        raise RuntimeError("dispatcher down")

    env.monkeypatch.setattr(env.server, "_run_prompt_submit", boom)
    turn_end(env)   # _hook_failure swallows it; the follow-up chain continues
    row = env.store.get("m-ex")
    assert row.fields["status"] == "queued" and "dispatcher down" in row.fields["last_error"]
    assert env.session["running"] is False


@pytest.mark.parametrize("fault", ["status_write", "status_write_committed", "bad_attempts"])
def test_pre_dispatch_fault_releases_session_and_recovers_message(env, fault):
    if fault == "bad_attempts":
        env.store.seed("m-bad", msg(5, attempts="invalid"))
        turn_end(env)
        row = env.store.get("m-bad")
        assert row.fields["status"] == "rejected" and "attempts" in row.fields["last_error"]
    else:
        env.store.seed("m-retry", msg(5))
        update = env.store.update
        failed = False

        def fail_read_once(doc_id, fields, update_time):
            nonlocal failed
            if fields.get("status") == "read" and not failed:
                failed = True
                if fault == "status_write_committed":
                    update(doc_id, fields, update_time)
                raise TimeoutError("transient status write")
            return update(doc_id, fields, update_time)

        env.monkeypatch.setattr(env.store, "update", fail_read_once)
        turn_end(env)
        row = env.store.get("m-retry")
        assert row.fields["status"] == "queued" and row.fields["attempts"] == 2
    assert env.session["running"] is False and not env.submitted
    if fault == "bad_attempts":
        env.store.seed("m-good", msg(3))
    turn_end(env)
    assert [item.rid for item in env.submitted] == [
        "__fleet_msg__m-good" if fault == "bad_attempts" else "__fleet_msg__m-retry"]
    assert env.session["running"] is False


@pytest.mark.parametrize("commit_before_timeout", [False, True])
def test_failed_turn_receipt_retries_without_replaying_the_turn(env, commit_before_timeout):
    env.store.seed("m-receipt", msg(5))
    env.outcome = {"status": "failed", "error": "provider unavailable"}
    update = env.store.update
    failed = False

    def fail_receipt_once(doc_id, fields, update_time):
        nonlocal failed
        if "attempts" in fields and not failed:
            failed = True
            if commit_before_timeout:
                update(doc_id, fields, update_time)
            raise TimeoutError("receipt acknowledgement lost")
        return update(doc_id, fields, update_time)

    env.monkeypatch.setattr(env.store, "update", fail_receipt_once)
    turn_end(env)
    assert len(env.submitted) == 1
    assert env.session["running"] is False
    assert env.store.get("m-receipt").fields["status"] in ("read", "queued")
    turn_end(env)
    row = env.store.get("m-receipt")
    assert row.fields["status"] == "queued" and row.fields["attempts"] == 2
    assert "provider unavailable" in row.fields["last_error"]
    assert len(env.submitted) == 1
    assert "_fleet_drain_pending" not in env.session


def test_prompt_that_wins_the_session_after_the_query_gets_the_doc_released(env):
    env.store.seed("m-rel", msg(5))
    real_claim = fmd.claim_next

    def claim_then_user_prompt(*a, **k):
        claimed = real_claim(*a, **k)
        env.session["running"] = True       # a user prompt was admitted while we queried
        return claimed

    env.monkeypatch.setattr(fmd, "claim_next", claim_then_user_prompt)
    env.server._drain_fleet_messages_once("live", env.session)
    row = env.store.get("m-rel")
    assert row.fields["status"] == "queued" and row.fields["attempts"] == 1   # no attempt counted
    assert not env.submitted


@pytest.mark.parametrize("change", [
    {"running": True}, {"queued_prompt": {"text": "x"}}, {"_closing": True},
    {"active_session_lease": None}, {"agent": None}])
def test_busy_or_non_owner_sessions_make_no_query(env, change):
    env.store.seed("m-busy", msg(5))
    env.session.update(change)
    assert env.server._drain_fleet_messages_once("live", env.session) is False
    assert env.store.reads == 0


def test_non_canonical_bot_chat_makes_no_query(env):
    env.store.seed("m-nc", msg(5))
    assert env.server._drain_fleet_messages_once("some-other-window", env.session) is False
    assert env.store.reads == 0


def test_disabled_config_builds_no_store(env):
    env.monkeypatch.setattr(fmd, "drain_config", lambda cfg=None: None)
    env.monkeypatch.setattr(fmd, "store_for", lambda config: pytest.fail("store built while disabled"))
    assert env.server._drain_fleet_messages_once("live", env.session) is False


def test_idle_poller_loop_makes_no_firestore_read(env):
    """The per-session poller runs all its idle work (mailbox, /loop, kanban, completions) for a while;
    the Firestore store must not see one query."""
    env.store.seed("m-idle", msg(5))
    server = env.server
    env.monkeypatch.setattr(server, "_BOT_DELIVERY_POLL_SECONDS", 0.05, raising=False)
    env.monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.05, raising=False)
    env.monkeypatch.setattr(server, "_notif_poll_kanban", lambda sid, session: None)
    stop = threading.Event()
    worker = threading.Thread(target=server._notification_poller_scoped_loop, args=(stop, "live", env.session))
    worker.start()
    time.sleep(1.5)
    stop.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert env.store.reads == 0 and env.store.commits == 0
    assert env.store.get("m-idle").fields["status"] == "queued"


def _referenced_names(fn) -> set:
    names, stack = set(), [fn.__code__]
    while stack:
        code = stack.pop()
        names.update(code.co_names)
        stack.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return names


def test_only_the_post_turn_followups_call_the_drain():
    """Static guard for the no-idle-polling rule: no other gateway function references the drain."""
    from tui_gateway import server

    callers = sorted(name for name, obj in vars(server).items()
                     if isinstance(obj, types.FunctionType) and name != "_drain_fleet_messages_once"
                     and "_drain_fleet_messages_once" in _referenced_names(obj))
    assert callers == ["_run_post_turn_followups"]
    importers = sorted(name for name, obj in vars(server).items()
                       if isinstance(obj, types.FunctionType) and "fleet_message_drain" in _referenced_names(obj))
    assert importers == ["_drain_fleet_messages_once"]
    assert dis  # keep the import honest for readers checking bytecode by hand
