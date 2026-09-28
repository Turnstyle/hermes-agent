"""``hermes chat -Q`` as a dispatcher's re-run of a failed bot delivery (#111721).

A failed delivery turn persists its user row before the provider call; the policy-gated re-run
replays the same session and payload in a fresh process. Told so via HERMES_RESUME_UNANSWERED_TURN,
the re-run resumes that row instead of appending a second copy of the DM.
"""

from __future__ import annotations

import contextlib
import os
import datetime
from types import SimpleNamespace
import json
import time

import cli
import pytest
from tools import fleet_message_drain as fmd
from agent.context_compressor import _DB_PERSISTED_MARKER
from tools.bot_relay import RESUME_UNANSWERED_TURN_ENV


def test_delivery_linger_releases_lease_then_reacquires_both_and_reloads(monkeypatch, tmp_path):
    from hermes_cli import active_sessions, quiet_single_query as qsq
    from tools.bot_relay import TurnBusyError, acquire_turn_lock

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(qsq.BOT_CHAT_DELIVERY_PROFILE_ENV, "ops")
    lease, refusal = active_sessions.try_acquire_active_session(
        session_id="s-1", surface="cli", config={}, metadata={"live_session_id": "s-1"})
    assert refusal is None

    fresh = [{"role": "user", "content": "first", "_row_id": 1},
             {"role": "assistant", "content": "first reply", "_row_id": 2},
             {"role": "user", "content": "another delivery", "_row_id": 3},
             {"role": "assistant", "content": "other reply", "_row_id": 4}]

    class DB:
        def resolve_resume_session_id(self, sid):
            return sid

        def get_messages_as_conversation(self, sid, **kwargs):
            assert kwargs == {"repair_alternation": True, "include_row_ids": True}
            return list(fresh)

    seen = []

    def run_conversation(**kwargs):
        seen.append(kwargs)
        return {"final_response": "first reply" if len(seen) == 1 else "follow reply",
                "messages": fresh if len(seen) > 1 else fresh[:2]}

    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1", _session_db=DB(),
                            _session_title_hint="Bot Chat")
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="unused"))

    class CLI:
        config = {}
        session_id = "s-1"
        conversation_history = []
        _active_session_lease = lease

        def _release_active_session(self):
            if self._active_session_lease is not None:
                self._active_session_lease.release()
                self._active_session_lease = None

    owner = CLI()
    owner.agent = agent
    drained = []

    def check_drain(_cli, history):
        assert owner._active_session_lease is not None
        assert history == fresh
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(tmp_path, "ops", 0):
                pass
        drained.append(True)

    monkeypatch.setattr("hermes_cli.cli_single_query._drain_quiet_bot_chat", check_drain)

    def linger(_sid, run_turn, **kwargs):
        assert owner._active_session_lease is None
        assert json.loads(active_sessions._state_path().read_text())["entries"] == []
        with acquire_turn_lock(tmp_path, "ops", 0.5):
            pass
        with kwargs["turn_guard"]() as acquired:
            assert acquired
            assert owner._active_session_lease is not None
            with pytest.raises(TurnBusyError):
                with acquire_turn_lock(tmp_path, "ops", 0):
                    pass
            result = run_turn("notify")
        assert owner._active_session_lease is None
        return result

    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", linger)
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(owner, "first")
    assert exc.value.code == 0
    assert seen[1]["conversation_history"] == fresh
    assert drained == [True]
    assert qsq.BOT_CHAT_DELIVERY_PROFILE_ENV not in os.environ


def test_busy_follow_up_does_not_pop_notifications(monkeypatch, tmp_path, caplog):
    from hermes_cli import quiet_single_query as qsq
    from tools.bot_relay import acquire_turn_lock

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.process_registry.process_registry.wait_for_pending_completions",
                        lambda *_a, **_k: {"timed_out": False})
    popped = []
    monkeypatch.setattr("tools.process_registry.process_registry.drain_notifications",
                        lambda **_k: popped.append(True))
    monkeypatch.setattr("tools.process_registry.process_registry.pending_owned_notifications",
                        lambda **_k: [{"id": "event-1", "session_id": "proc-1"}])
    owner = SimpleNamespace(agent=SimpleNamespace(_session_db=object()), session_id="s-1", config={})
    with acquire_turn_lock(tmp_path, "ops", 0.5):
        result = qsq.continue_quiet_notify_completions(
            "s-1", lambda _: pytest.fail("follow-up ran"), max_rounds=1, linger_budget=0,
            turn_guard=lambda: qsq.bot_chat_follow_up_lock(owner, "ops", time.monotonic()))
    assert result is None and popped == []
    assert "Bot Chat follow-up skipped" in caplog.text
    assert "child_pid=" in caplog.text and "delivery_pid=" in caplog.text
    assert "event-1" in caplog.text and "proc-1" in caplog.text


@pytest.mark.parametrize("route", ("live", "queue", "warning", "live_error", "queue_error"))
def test_refused_follow_up_hands_off_before_consuming(tmp_path, monkeypatch, caplog, route):
    from hermes_cli import quiet_single_query as qsq
    from tools import bot_live_delivery, fleet_message_drain as fmd
    from tools.process_registry import process_registry

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    events = [{"type": "completion", "id": "event-1", "session_id": "proc-1", "session_key": "s-1",
               "command": "echo reply", "exit_code": 0, "output": "teammate reply"}]
    actions = []
    monkeypatch.setattr(process_registry, "wait_for_pending_completions",
                        lambda *_a, **_k: {"timed_out": False})
    monkeypatch.setattr(process_registry, "pending_owned_notifications", lambda **_k: list(events))

    def drain(**_kw):
        actions.append("pop")
        return [(events.pop(0), "formatted")]

    monkeypatch.setattr(process_registry, "drain_notifications", drain)
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery",
                        lambda event, consumer: actions.append("claim") or "claim-token")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery",
                        lambda event, claim: actions.append("complete"))
    owner = {"profile_home": str(home), "session_id": "s-1", "lease_id": "lease",
             "live_session_id": "s-1"}
    monkeypatch.setattr(bot_live_delivery, "find_canonical_live_owner",
                        lambda _home: owner if route in ("live", "live_error") else None)
    ids = []

    def deliver(_home, _owner, message, *, delivery_id):
        actions.append("live")
        ids.append(delivery_id)
        assert "teammate reply" in message and not actions.count("pop")
        if route == "live_error":
            raise RuntimeError("owner disappeared")
        return {"status": "queued"}

    monkeypatch.setattr(bot_live_delivery, "deliver_to_live_owner", deliver)
    monkeypatch.setattr(fmd, "drain_config_for_home",
                        lambda _home: object() if route in ("queue", "live_error", "queue_error") else None)

    def enqueue(sender, recipient, message, *, config):
        actions.append("queue")
        assert sender == recipient == "ops" and "teammate reply" in message
        assert "pop" not in actions
        if route == "queue_error":
            raise RuntimeError("queue unavailable")
        return "queued-1"

    monkeypatch.setattr(fmd, "enqueue_message", enqueue)
    result = qsq.continue_quiet_notify_completions(
        "s-1", lambda _text: pytest.fail("follow-up ran"), max_rounds=1, linger_budget=0,
        turn_guard=lambda: contextlib.nullcontext(False))
    assert result is None
    if route in ("warning", "queue_error"):
        assert actions == (["queue"] if route == "queue_error" else []) and events
        assert "event-1" in caplog.text and "lost at child exit" in caplog.text
    else:
        assert actions[-3:] == ["pop", "claim", "complete"]
        assert ("live" in actions) == (route in ("live", "live_error"))
        assert ("queue" in actions) == (route in ("queue", "live_error"))
        assert ("route=live_owner" if route == "live" else "route=fleet_queue") in caplog.text
        if route == "live":
            import hashlib
            assert ids == [hashlib.sha256(b'["s-1",["event-1"]]').hexdigest()]


def test_refused_follow_up_reaches_real_live_owner_mailbox(tmp_path, monkeypatch):
    from hermes_cli import quiet_single_query as qsq
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox
    from tools.process_registry import process_registry

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    db.create_session("s-1", source="desktop")
    db.set_session_title("s-1", "Bot Chat")
    db.close()
    lease, refusal = try_acquire_active_session(
        session_id="s-1", surface="desktop", config={}, registry_home=home,
        metadata={"live_session_id": "ui-1", "bot_live_delivery_consumer": True})
    assert refusal is None
    event = {"type": "completion", "id": "event-1", "session_id": "proc-1", "session_key": "s-1",
             "command": "echo reply", "exit_code": 0, "output": "teammate reply"}
    queued = [event]
    monkeypatch.setattr(process_registry, "wait_for_pending_completions", lambda *_a, **_k: {"timed_out": False})
    monkeypatch.setattr(process_registry, "pending_owned_notifications", lambda **_k: list(queued))
    monkeypatch.setattr(process_registry, "drain_notifications", lambda **_k: [(queued.pop(), "text")])
    try:
        qsq.continue_quiet_notify_completions(
            "s-1", lambda _text: pytest.fail("follow-up ran"), max_rounds=1, linger_budget=0,
            turn_guard=lambda: contextlib.nullcontext(False))
        owner = mailbox.find_canonical_live_owner(home)
        claimed = mailbox.claim_pending_delivery(home, owner)
        assert claimed is not None and "teammate reply" in claimed["message"]
        assert queued == []
    finally:
        lease.release()


def test_no_pending_notify_does_not_wait_for_held_flock(tmp_path, monkeypatch):
    from hermes_cli import quiet_single_query as qsq
    from tools.bot_relay import acquire_turn_lock

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, "wait_for_pending_completions", lambda *_a, **_k: {"timed_out": True})
    monkeypatch.setattr(process_registry, "pending_owned_notifications", lambda **_k: [])
    owner = SimpleNamespace(agent=SimpleNamespace(_session_db=object()), session_id="s-1", config={})
    with acquire_turn_lock(tmp_path, "ops", 0.5):
        start = time.monotonic()
        result = qsq.continue_quiet_notify_completions(
            "s-1", lambda _text: pytest.fail("follow-up ran"), max_rounds=1, linger_budget=0,
            turn_guard=lambda: qsq.bot_chat_follow_up_lock(owner, "ops", time.monotonic() + 2.0))
        assert time.monotonic() - start < 0.5
    assert result is None


def test_refused_lease_does_not_hold_profile_flock(tmp_path, monkeypatch):
    import subprocess
    import sys
    import threading
    from hermes_cli import quiet_single_query as qsq
    from tools.bot_relay import acquire_turn_lock

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    holder = subprocess.Popen([sys.executable, "-c", (
        "import time\n"
        "from hermes_cli.active_sessions import try_acquire_active_session\n"
        "lease, refusal = try_acquire_active_session(session_id='s-1', surface='desktop', config={}, "
        "metadata={'live_session_id': 's-1'})\n"
        "print('ok' if refusal is None else 'refused', flush=True)\n"
        "input()\n")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "HERMES_HOME": str(home)})
    try:
        assert holder.stdout.readline().strip() == "ok"
        owner = SimpleNamespace(agent=SimpleNamespace(_session_db=SimpleNamespace(
            resolve_resume_session_id=lambda sid: sid)), session_id="s-1", config={})
        outcome = []

        def follow_up():
            with qsq.bot_chat_follow_up_lock(owner, "ops", time.monotonic() + 1.5) as acquired:
                outcome.append(acquired)

        worker = threading.Thread(target=follow_up)
        worker.start()
        try:
            time.sleep(0.3)
            with acquire_turn_lock(tmp_path, "ops", 0.5):
                assert worker.is_alive()
        finally:
            worker.join(timeout=3)
        assert outcome == [False]
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=5)


def test_personal_quiet_run_does_not_release_active_session(monkeypatch):
    from hermes_cli import quiet_single_query as qsq

    monkeypatch.delenv(qsq.BOT_CHAT_DELIVERY_PROFILE_ENV, raising=False)
    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", lambda *_a, **_k: None)
    monkeypatch.setattr("hermes_cli.cli_single_query._drain_quiet_bot_chat", lambda *_a: None)
    released = []
    owner = SimpleNamespace(
        agent=SimpleNamespace(run_conversation=lambda **_k: {"final_response": "ok"}, session_id="s-1"),
        session_id="s-1", conversation_history=[], _release_active_session=lambda: released.append(True))
    with pytest.raises(SystemExit):
        cli._run_quiet_single_query(owner, "hi")
    assert released == []


def test_partial_failed_delivery_keeps_lease_through_notify_wait(monkeypatch):
    from hermes_cli import quiet_single_query as qsq

    monkeypatch.setenv(qsq.BOT_CHAT_DELIVERY_PROFILE_ENV, "ops")
    monkeypatch.setattr(qsq, "quiet_notify_linger_seconds", lambda: 0)
    released = []
    owner = SimpleNamespace(
        agent=SimpleNamespace(run_conversation=lambda **_k: {"partial": True, "final_response": ""},
                              session_id="s-1"),
        session_id="s-1", conversation_history=[], config={},
        _release_active_session=lambda: released.append(True))

    def linger(_sid, _turn, **kwargs):
        assert "turn_guard" not in kwargs
        assert released == []
        return None

    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", linger)
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(owner, "hi")
    assert exc.value.code == 1 and released == []


def test_bot_chat_quiet_turn_drains_one_message_without_changing_stdout(monkeypatch, capsys, tmp_path):
    from hermes_cli import quiet_single_query as qsq

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = fmd.DrainConfig(target="emulator", emulator_host="unused")
    monkeypatch.setattr(fmd, "drain_config", lambda: config)
    now = fmd.utcnow()
    fields = {"message_id": "m1", "from": "sender", "to": "ops", "kind": "dm", "body": "hello",
              "status": "queued", "attempts": 0, "created_at": fmd.rfc3339(now),
              "updated_at": fmd.rfc3339(now), "expires_at": fmd.rfc3339(now + datetime.timedelta(hours=24))}

    class Store:
        def __init__(self):
            self.fields = fields
            self.version = 0

        def query_queued(self, to, limit):
            return [fmd.Row("m1", dict(self.fields), str(self.version))] if self.fields["status"] == "queued" else []

        def update(self, doc_id, changes, update_time):
            assert doc_id == "m1" and update_time == str(self.version)
            self.fields.update(changes)
            self.version += 1
            return str(self.version)

    store = Store()
    monkeypatch.setattr(fmd, "store_for", lambda _: store)
    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", lambda *a, **kw: None)
    turns = []

    def run_conversation(**kwargs):
        turns.append(kwargs)
        if len(turns) == 2:
            print("drained output")
        return {"final_response": "first reply" if len(turns) == 1 else "drained reply",
                "messages": [{"role": "assistant", "content": "saved"}]}

    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1", _session_title_hint="Bot Chat")
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"), "first")
    assert exc.value.code == 0
    assert len(turns) == 2
    assert turns[1]["conversation_history"] == [{"role": "assistant", "content": "saved"}]
    assert turns[1]["turn_author"]["id"] == "bot:sender"
    assert store.fields["status"] == "done"
    assert capsys.readouterr().out == "first reply\n"


def test_uncertain_mark_read_requeues_instead_of_stranding_at_read(monkeypatch, capsys, tmp_path):
    """Checker HIGH (VERDICT-CLAUDE): mark_read commits, then the write reply is lost. The -Q drain
    must reconcile before record_error, so the doc goes back to queued, not a stuck 'read'."""
    from hermes_cli import quiet_single_query as qsq

    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(target="emulator", emulator_host="unused"))
    now = fmd.utcnow()
    fields = {"message_id": "m1", "from": "sender", "to": "ops", "kind": "dm", "body": "hello",
              "status": "queued", "attempts": 0, "created_at": fmd.rfc3339(now),
              "updated_at": fmd.rfc3339(now), "expires_at": fmd.rfc3339(now + datetime.timedelta(hours=24))}

    class Store:
        version = 0

        def query_queued(self, to, limit):
            return [fmd.Row("m1", dict(fields), str(self.version))] if fields["status"] == "queued" else []

        def get(self, doc_id):
            return fmd.Row("m1", dict(fields), str(self.version))

        def update(self, doc_id, changes, update_time):
            assert update_time == str(self.version)
            fields.update(changes)
            self.version += 1
            if changes.get("status") == "read":
                raise TimeoutError("write committed, reply lost")
            return str(self.version)

    monkeypatch.setattr(fmd, "store_for", lambda _: Store())
    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", lambda *a, **kw: None)
    turns = []

    def run_conversation(**kwargs):
        turns.append(kwargs)
        return {"final_response": "first reply", "messages": []}

    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1", _session_title_hint="Bot Chat")
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"), "first")
    assert exc.value.code == 0 and len(turns) == 1
    assert fields["status"] == "queued" and fields["attempts"] == 1
    assert capsys.readouterr().out == "first reply\n"


def _quiet_turn(monkeypatch, history, marker):
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if marker is None:
        monkeypatch.delenv(RESUME_UNANSWERED_TURN_ENV, raising=False)
    else:
        monkeypatch.setenv(RESUME_UNANSWERED_TURN_ENV, marker)
    seen = {}

    def run_conversation(**kwargs):
        seen["history"] = list(kwargs["conversation_history"])
        seen["env"] = os.environ.get(RESUME_UNANSWERED_TURN_ENV)
        return {"final_response": "ok"}

    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1")
    the_cli = SimpleNamespace(agent=agent, conversation_history=history, session_id="s-1")
    try:
        cli._run_quiet_single_query(the_cli, "hello")
    except SystemExit as exc:
        assert exc.code == 0
    return agent, seen


def test_rerun_resumes_the_unanswered_user_row_it_already_persisted(monkeypatch):
    tail = {"role": "user", "content": "hello"}
    agent, seen = _quiet_turn(monkeypatch, [{"role": "assistant", "content": "earlier"}, tail], "1")
    # The persisted row leaves the history and becomes this turn's staged user dict, already
    # durable — so the agent reuses it as the turn's user message and the flush writes no new row.
    assert seen["history"] == [{"role": "assistant", "content": "earlier"}]
    assert agent._pending_cli_user_message is tail
    assert tail[_DB_PERSISTED_MARKER] is True
    assert seen["env"] is None, "the marker is consumed before the turn so tool subprocesses never inherit it"


def test_without_the_marker_or_an_unanswered_tail_nothing_is_adopted(monkeypatch):
    """A person re-sending the same word after a failed turn has sent a second real message; an
    answered tail is not a re-run either. Only the dispatcher's explicit marker plus an identical
    unanswered tail adopts a row."""
    tail = {"role": "user", "content": "hello"}
    agent, seen = _quiet_turn(monkeypatch, [tail], None)
    assert seen["history"] == [tail] and not hasattr(agent, "_pending_cli_user_message")
    assert _DB_PERSISTED_MARKER not in tail

    answered = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "done"}]
    agent, seen = _quiet_turn(monkeypatch, list(answered), "1")
    assert seen["history"] == answered and not hasattr(agent, "_pending_cli_user_message")


def test_rerun_adopts_the_dm_behind_the_failed_attempts_tool_scaffolding(monkeypatch):
    """A 503 after a tool round persisted user + assistant(tool_calls) + tool before the failure text,
    and the dispatcher retries it. The DM is still unanswered: the re-run adopts it and drops the
    failed attempt's scaffolding from the in-memory turn instead of appending a second copy."""
    tail = {"role": "user", "content": "hello"}
    scaffolding = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    agent, seen = _quiet_turn(monkeypatch, [{"role": "assistant", "content": "earlier"}, tail, *scaffolding], "1")
    assert seen["history"] == [{"role": "assistant", "content": "earlier"}]
    assert agent._pending_cli_user_message is tail and tail[_DB_PERSISTED_MARKER] is True


def test_turn_report_is_written_before_the_exit_linger_and_the_path_is_not_inherited(monkeypatch, tmp_path):
    """A spawner that bounds only the turn (cron Bot Chat lane, #113608) reads the outcome from
    HERMES_QUIET_TURN_REPORT_FILE: written the moment the turn ends — before the one-shot exit
    linger — stamped with this pid, and the variable is popped before the turn spawns anything."""
    from hermes_cli import quiet_single_query as qsq

    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    report = tmp_path / "turn.json"
    monkeypatch.setenv(qsq.TURN_REPORT_FILE_ENV, str(report))
    seen = {}

    def run_conversation(**kwargs):
        seen["env_during_turn"] = os.environ.get(qsq.TURN_REPORT_FILE_ENV)
        seen["report_during_turn"] = report.exists()
        return {"final_response": "ok"}

    def linger(*args, **kwargs):
        seen["report_at_linger"] = qsq.read_turn_report(str(report), os.getpid())
        return {"waited": [], "completed": [], "timed_out": []}

    monkeypatch.setattr("tools.process_registry.process_registry.wait_for_pending_completions", linger)
    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1")
    try:
        cli._run_quiet_single_query(SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"), "hello")
    except SystemExit as exc:
        assert exc.code == 0
    assert seen["env_during_turn"] is None and seen["report_during_turn"] is False
    assert seen["report_at_linger"] == {"pid": os.getpid(), "exit_code": 0, "error": "", "reply": "ok"}
    # Another process's record is not this child's report.
    assert qsq.read_turn_report(str(report), os.getpid() + 1) is None


def test_a_follow_up_turn_rewrites_the_report_with_the_answer_it_displaces(monkeypatch, tmp_path):
    """The report says what this run will print. When a teammate's reply during the linger runs a
    follow-up turn whose answer displaces the first (the quiet run's final-answer contract), the
    report is rewritten with it, so a relay booking the child at its cap relays that answer (#114980)."""
    from hermes_cli import quiet_single_query as qsq

    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    report = tmp_path / "turn.json"
    monkeypatch.setenv(qsq.TURN_REPORT_FILE_ENV, str(report))
    answers = iter(["asking the teammate", "teammate says: done"])
    seen = {}

    def run_conversation(**kwargs):
        return {"final_response": next(answers), "messages": []}

    def linger_then_follow_up(session_id, run_turn, **kwargs):
        seen["report_before_follow_up"] = qsq.read_turn_report(str(report), os.getpid())["reply"]
        return run_turn("teammate: done")

    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", linger_then_follow_up)
    monkeypatch.setattr("tools.process_registry.process_registry.wait_for_pending_completions",
                        lambda *a, **k: {"waited": [], "completed": [], "timed_out": []})
    agent = SimpleNamespace(run_conversation=run_conversation, session_id="s-1")
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(a))
    try:
        cli._run_quiet_single_query(SimpleNamespace(agent=agent, conversation_history=[], session_id="s-1"), "hello")
    except SystemExit as exc:
        assert exc.code == 0
    assert seen["report_before_follow_up"] == "asking the teammate"
    assert qsq.read_turn_report(str(report), os.getpid())["reply"] == "teammate says: done"
    assert ("teammate says: done",) in printed, "the report and stdout name the same answer"
