"""``hermes chat -Q`` as a dispatcher's re-run of a failed bot delivery (#111721).

A failed delivery turn persists its user row before the provider call; the policy-gated re-run
replays the same session and payload in a fresh process. Told so via HERMES_RESUME_UNANSWERED_TURN,
the re-run resumes that row instead of appending a second copy of the DM.
"""

from __future__ import annotations

import os
import datetime
import shutil
from types import SimpleNamespace

import cli
import pytest
from tools import fleet_message_drain as fmd
from agent.context_compressor import _DB_PERSISTED_MARKER
from tools.bot_relay import RESUME_UNANSWERED_TURN_ENV


def test_quiet_bot_chat_uses_configured_queued_timeout(monkeypatch, tmp_path):
    from hermes_cli.cli_single_query import _drain_quiet_bot_chat
    from tests.tools.test_fleet_message_drain import ME, NOW, MemoryStore, msg

    home = tmp_path / "profiles" / ME
    store = MemoryStore()
    store.seed("too-old", msg(11))
    monkeypatch.setattr(fmd, "drain_config", lambda: fmd.DrainConfig(
        target="emulator", emulator_host="fake", queued_timeout_seconds=600))
    monkeypatch.setattr(fmd, "store_for", lambda _: store)
    monkeypatch.setattr(fmd, "utcnow", lambda: NOW)
    agent = SimpleNamespace(_session_title_hint="Bot Chat", _session_db=SimpleNamespace(db_path=home / "state.db"))
    _drain_quiet_bot_chat(SimpleNamespace(agent=agent), [])
    row = store.get("too-old").fields
    assert row["status"] == "expired" and "10 minutes" in row["sender_notice"]


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
    the_cli = SimpleNamespace(
        agent=agent, conversation_history=[], session_id="s-1",
        _release_active_session=lambda: None,
        _claim_active_session=lambda *a, **k: True,
    )
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(the_cli, "first")
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
    the_cli = SimpleNamespace(
        agent=agent, conversation_history=[], session_id="s-1",
        _release_active_session=lambda: None,
        _claim_active_session=lambda *a, **k: True,
    )
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(the_cli, "first")
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
    the_cli = SimpleNamespace(
        agent=agent,
        conversation_history=history,
        session_id="s-1",
        _release_active_session=lambda: None,
        _claim_active_session=lambda *a, **k: True,
    )
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
        cli._run_quiet_single_query(
            SimpleNamespace(
                agent=agent,
                conversation_history=[],
                session_id="s-1",
                _release_active_session=lambda: None,
                _claim_active_session=lambda *a, **k: True,
            ),
            "hello",
        )
    except SystemExit as exc:
        assert exc.code == 0
    assert seen["env_during_turn"] is None and seen["report_during_turn"] is False
    assert seen["report_at_linger"] == {"pid": os.getpid(), "exit_code": 0, "error": "", "reply": "ok"}
    # Another process's record is not this child's report.
    assert qsq.read_turn_report(str(report), os.getpid() + 1) is None


def test_late_follow_up_does_not_recreate_abandoned_report_directory(tmp_path):
    from hermes_cli.quiet_single_query import write_turn_report

    report_dir = tmp_path / "one-shot-report"
    report_dir.mkdir()
    report = report_dir / "turn.json"
    write_turn_report(str(report), exit_code=0)
    assert report.exists()
    shutil.rmtree(report_dir)
    write_turn_report(str(report), exit_code=0, reply="late follow-up")
    assert not report_dir.exists()


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
        cli._run_quiet_single_query(
            SimpleNamespace(
                agent=agent,
                conversation_history=[],
                session_id="s-1",
                _release_active_session=lambda: None,
                _claim_active_session=lambda *a, **k: True,
            ),
            "hello",
        )
    except SystemExit as exc:
        assert exc.code == 0
    assert seen["report_before_follow_up"] == "asking the teammate"
    assert qsq.read_turn_report(str(report), os.getpid())["reply"] == "teammate says: done"
    assert ("teammate says: done",) in printed, "the report and stdout name the same answer"


def _quiet_cli_with_real_session_lease(session_id: str = "s-1"):
    """Minimal CLI stub whose claim/release use the real active-session registry."""
    from cli import HermesCLI

    the_cli = object.__new__(HermesCLI)
    the_cli.session_id = session_id
    the_cli.config = {}
    the_cli._active_session_lease = None
    the_cli._console_print = lambda *_a, **_k: None
    the_cli.conversation_history = []
    return the_cli


def test_idle_post_turn_linger_does_not_hold_the_session_lease(monkeypatch):
    """After a successful -Q turn the exit linger is idle: another delivery must be able to claim the same stored session,
    and a writer still holding it when the linger ends owns the row's finalization (the lingerer never ends it)."""
    from hermes_cli import quiet_single_query as qsq
    from hermes_cli.active_sessions import release_active_session, try_acquire_active_session

    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_handed_off_session_ids", set())

    session_id = "bot-chat-shared"
    the_cli = _quiet_cli_with_real_session_lease(session_id)
    assert the_cli._claim_active_session("cli") is True

    linger_probe = {}

    def linger_during_idle_wait(session_id_arg, run_turn, **kwargs):
        other_lease, refusal = try_acquire_active_session(
            session_id=session_id,
            surface="desktop",
            config={},
            metadata={"live_session_id": "other-bot-delivery"},
        )
        linger_probe["other_lease"] = other_lease
        linger_probe["refusal"] = refusal
        return {"waited": [], "completed": [], "timed_out": []}

    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", linger_during_idle_wait)
    the_cli.agent = SimpleNamespace(
        run_conversation=lambda **kwargs: {"final_response": "ok"},
        session_id=session_id,
    )

    try:
        try:
            cli._run_quiet_single_query(the_cli, "hello")
        except SystemExit as exc:
            assert exc.code == 0
    finally:
        the_cli._release_active_session()
        if linger_probe.get("other_lease") is not None:
            release_active_session(linger_probe["other_lease"])

    assert linger_probe["other_lease"] is not None, (
        "another writer must acquire the session while the one-shot lingers idle"
    )
    assert linger_probe["refusal"] is None
    assert session_id in cli._handed_off_session_ids, (
        "the lingerer must not finalize a Bot Chat another writer holds (#88234)"
    )


def test_follow_up_turn_holds_lease_and_reloads_stored_transcript(monkeypatch):
    """A notify follow-up re-claims the lease for its turn and continues from SQLite when another writer appended rows during the idle linger."""
    from hermes_cli import quiet_single_query as qsq
    from hermes_cli.active_sessions import SESSION_NOT_OWNED, try_acquire_active_session

    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli.atexit, "register", lambda *_a, **_k: None)

    session_id = "bot-chat-follow-up"
    stored_after_other_writer = [
        {"role": "assistant", "content": "gateway reply"},
        {"role": "user", "content": "teammate ping"},
    ]
    row_counts = iter([1, 2, 2])

    class _SessionDb:
        def message_count(self, sid):
            assert sid == session_id
            return next(row_counts)

        def get_messages_as_conversation(self, sid, repair_alternation=True):
            assert sid == session_id
            return list(stored_after_other_writer)

    the_cli = _quiet_cli_with_real_session_lease(session_id)
    the_cli._session_db = _SessionDb()
    the_cli.conversation_history = [{"role": "user", "content": "stale in-memory"}]
    assert the_cli._claim_active_session("cli") is True

    follow_obs = {"turn": 0}

    def run_conversation(**kwargs):
        follow_obs["turn"] += 1
        if follow_obs["turn"] == 1:
            return {"final_response": "first answer"}
        other_lease, refusal = try_acquire_active_session(
            session_id=session_id,
            surface="desktop",
            config={},
            metadata={"live_session_id": "blocked-during-follow-up"},
        )
        follow_obs["other_blocked"] = other_lease is None and getattr(refusal, "reason", None) == SESSION_NOT_OWNED
        follow_obs["history"] = list(kwargs["conversation_history"])
        return {"final_response": "follow answer", "messages": stored_after_other_writer}

    def linger_with_follow_up(session_id_arg, run_turn, **kwargs):
        return run_turn("notify: teammate done")

    monkeypatch.setattr(qsq, "continue_quiet_notify_completions", linger_with_follow_up)
    the_cli.agent = SimpleNamespace(run_conversation=run_conversation, session_id=session_id)

    try:
        try:
            cli._run_quiet_single_query(the_cli, "hello")
        except SystemExit as exc:
            assert exc.code == 0
    finally:
        the_cli._release_active_session()

    assert follow_obs["turn"] == 2
    assert follow_obs.get("other_blocked") is True
    assert follow_obs["history"] == stored_after_other_writer
