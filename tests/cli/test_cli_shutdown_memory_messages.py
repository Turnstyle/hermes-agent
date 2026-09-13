"""Regression tests for #15165 (CLI sibling site) — CLI exit cleanup must
forward the agent's conversation transcript to ``shutdown_memory_provider``
so memory providers' ``on_session_end`` hooks see the real messages.

Before the fix, ``_run_cleanup`` called
``shutdown_memory_provider(getattr(agent, 'conversation_history', None) or [])``.
``AIAgent`` has no ``conversation_history`` attribute — so the ``or []``
branch always fired and providers got an empty list on CLI exit. This
mirrors the gateway bug fixed in the same commit (gateway/run.py uses
``_session_messages``, which IS set on ``AIAgent``).

The fix reads ``_session_messages`` (same attribute the gateway path uses)
with an ``isinstance(..., list)`` guard so MagicMock-based agents in
other tests keep their existing no-arg behaviour.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@patch("hermes_cli.plugins.invoke_hook")
def test_cleanup_forwards_session_messages(mock_invoke_hook):
    """_run_cleanup forwards a populated ``_session_messages`` list."""
    import cli as cli_mod

    transcript = [
        {"role": "user", "content": "remember my dog is named Biscuit"},
        {"role": "assistant", "content": "Got it — Biscuit."},
    ]

    agent = MagicMock()
    agent.session_id = "cli-session-id"
    agent._session_messages = transcript

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False

    agent.shutdown_memory_provider.assert_called_once_with(transcript)






@patch("hermes_cli.plugins.invoke_hook")
def test_cleanup_provider_exception_is_swallowed(mock_invoke_hook):
    """A raising ``shutdown_memory_provider`` must not crash CLI exit."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "cli-session-id"
    agent._session_messages = [{"role": "user", "content": "x"}]
    agent.shutdown_memory_provider.side_effect = RuntimeError("boom")

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()  # must not raise
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False

    agent.shutdown_memory_provider.assert_called_once()










def _real_agent(db, session_id, session_messages):
    """Build the real persistence seam without the heavyweight LLM client."""
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent.platform = "cli"
    agent.model = "test-model"
    agent._session_messages = session_messages
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    agent._cached_system_prompt = "test system prompt"
    agent._session_init_model_config = None
    agent._parent_session_id = None

    agent._pending_cli_user_message = None
    agent._session_persist_lock = threading.RLock()
    return agent


def _claimed_cli_recovery(db, session_id, *, text="retry me exactly"):
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    original = {"role": "user", "content": text}
    stamp_pending_input_admission(
        original, admission_id="cli-pre-provider", surface_key=f"cli:{session_id}",
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input", "text": text,
            "input_kind": "plain_text", "exact": True, "lossy": False,
            "loss_reasons": [], "safe_to_replay": True,
            "provider_call_sent": False, "executable": False,
        },
        live_messages=[original], session_ids=[session_id],
        admission_id="cli-pre-provider", surface_key=f"cli:{session_id}",
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, "cli-pre-provider", resend_id="cli-resend",
        claim_session_id=session_id,
    )
    resend = {"role": "user", "content": text}
    stamp_pending_input_admission(
        resend, admission_id="cli-resend", surface_key=f"cli:{session_id}",
    )
    db.append_message(
        session_id, "user", text,
        display_metadata=resend["display_metadata"],
    )
    return resend


def test_cli_pre_provider_exception_returns_exact_recovery_claim(tmp_path, monkeypatch):
    """A worker failure before the physical provider seam cannot strand a live-PID claim."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import get_pending_input_recovery
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-pre-provider-failure"
    db.create_session(session_id=session_id, source="cli")
    resend = _claimed_cli_recovery(db, session_id)
    agent = _real_agent(db, session_id, [resend])
    agent._pending_cli_user_message = resend
    agent._pending_input_recovery_claim_id = "cli-pre-provider"
    agent._pending_input_recovery_resend_id = "cli-resend"
    agent._active_session_turn_lease_holder = None
    agent.run_conversation = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("failed before provider")
    )
    agent._summarize_api_error = lambda exc: str(exc)

    cli = object.__new__(cli_mod.HermesCLI)
    cli.session_id = session_id
    cli.agent = agent
    cli.conversation_history = [resend]
    cli._sudo_password_callback = None
    cli._approval_callback = None
    cli._secret_capture_callback = None
    cli._pending_moa_config = None
    cli._pending_one_turn_model_restore = None
    cli._flush_credit_notices = lambda: None
    turn = SimpleNamespace(voice_prefix="", stream_callback=None, result=None)
    monkeypatch.setattr("tools.tts_streaming.take_speech_interrupted", lambda: False)

    cli._chat_run_agent(turn, resend["content"])

    record = get_pending_input_recovery(db, "cli-pre-provider")
    assert record["status"] == "pending"
    assert record["previous_resend_id"] == "cli-resend"
    assert db.get_messages_as_conversation(session_id) == []
    assert cli.conversation_history == []
    assert agent._session_messages == []
    assert agent._pending_cli_user_message is None
    assert agent._pending_input_recovery_claim_id is None
    assert turn.result["recovery_returned_without_dispatch"] is True
    assert turn.result["provider_call_sent"] is False


def test_cli_facade_exception_preserves_identity_until_surface_history_cleanup(
    tmp_path, monkeypatch,
):
    """The shared facade must not clear IDs before CLI removes its own staged row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import get_pending_input_recovery
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-facade-pre-provider-failure"
    db.create_session(session_id=session_id, source="cli")
    resend = _claimed_cli_recovery(db, session_id)
    agent = _real_agent(db, session_id, [resend])
    agent._pending_cli_user_message = resend
    agent._pending_input_recovery_claim_id = "cli-pre-provider"
    agent._pending_input_recovery_resend_id = "cli-resend"
    agent._active_session_turn_lease_holder = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: session_id
    agent.log_prefix = ""
    agent._vprint = lambda *_a, **_k: None
    agent.status_callback = None
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._pending_redirect = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._summarize_api_error = lambda exc: str(exc)

    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("facade failed before provider")
        ),
    )
    cli = object.__new__(cli_mod.HermesCLI)
    cli.session_id = session_id
    cli.agent = agent
    cli.conversation_history = [resend]
    cli._sudo_password_callback = None
    cli._approval_callback = None
    cli._secret_capture_callback = None
    cli._pending_moa_config = None
    cli._pending_one_turn_model_restore = None
    cli._flush_credit_notices = lambda: None
    turn = SimpleNamespace(voice_prefix="", stream_callback=None, result=None)
    monkeypatch.setattr("tools.tts_streaming.take_speech_interrupted", lambda: False)

    cli._chat_run_agent(turn, resend["content"])

    record = get_pending_input_recovery(db, "cli-pre-provider")
    assert record["status"] == "pending"
    assert record["previous_resend_id"] == "cli-resend"
    assert db.get_messages_as_conversation(session_id) == []
    assert cli.conversation_history == []
    assert agent._session_messages == []
    assert agent._pending_cli_user_message is None
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None
    assert turn.result["recovery_returned_without_dispatch"] is True


def test_cli_claim_stage_fault_rebinds_authoritative_db_and_returns_pending(
    tmp_path, monkeypatch,
):
    """A reopened CLI DB owns claim, rollback, and later provider settlement."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import (
        get_pending_input_recovery,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )
    from hermes_cli.prompt_stash import ComposerSubmission
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-reopened-db-stage-fault"
    surface = f"cli:{session_id}"
    recovery_id = "reopened-db-recovery"
    db.create_session(session_id=session_id, source="cli")
    original = {"role": "user", "content": "retry through reopened db"}
    stamp_pending_input_admission(
        original, admission_id=recovery_id, surface_key=surface,
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": original["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original], session_ids=[session_id],
        admission_id=recovery_id, surface_key=surface,
        accepted_input_durably_committed=False,
    )
    agent = _real_agent(db, session_id, [])
    # Simulate startup's first DB open failing, followed by the CLI reopening
    # state.db while the cached agent still holds None.
    agent._session_db = None
    agent._session_db_created = False

    cli = object.__new__(cli_mod.HermesCLI)
    cli._session_db = db
    cli.session_id = session_id
    cli.agent = agent
    cli.conversation_history = []
    cli._restore_pending_input_recoveries_to_stash = lambda: 0
    cli._secret_capture_callback = None
    cli._ensure_runtime_credentials = lambda: True
    cli._active_agent_route_signature = "route"
    cli._resolve_turn_agent_config = lambda _message: {
        "signature": "route", "model": "model", "runtime": None,
        "request_overrides": None,
    }
    cli._init_agent = lambda **_kwargs: True
    cli._chat_route_images = lambda message, _images: message
    cli._chat_release_turn_audio = lambda _turn: None
    console = MagicMock()
    console.print.side_effect = RuntimeError("terminal failed before worker start")
    monkeypatch.setattr(cli_mod, "ChatConsole", lambda: console)
    submission = ComposerSubmission(
        original["content"], recovery_admission_id=recovery_id,
        recovery_surface_key=surface,
    )

    assert cli.chat(submission) is None

    record = get_pending_input_recovery(db, recovery_id)
    assert agent._session_db is db
    assert record["status"] == "pending"
    assert record["previous_resend_id"]
    assert db.get_messages_as_conversation(session_id) == []
    assert cli.conversation_history == []
    assert agent._session_messages == []
    assert agent._pending_cli_user_message is None
    assert agent._pending_input_recovery_claim_id is None
    assert agent._pending_input_recovery_resend_id is None


def test_cli_close_preserves_merged_compaction_carrier_and_bridge(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.conversation_compression import _merge_anchor_into_user_message
    from agent.pending_input_recovery import (
        pending_input_admission,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-close-merged-carrier"
    surface = f"cli:{session_id}"
    recovery_id = "merged-close-recovery"
    db.create_session(session_id=session_id, source="cli")
    carrier = {
        "role": "user",
        "content": "[Your active task list was preserved across context compression]",
        "_todo_snapshot_synthetic": True,
        "display_metadata": {"scaffolding": "preserve-me"},
    }
    accepted = {"role": "user", "content": "finish the deployment"}
    stamp_pending_input_admission(
        accepted, admission_id=recovery_id, surface_key=surface,
    )
    _merge_anchor_into_user_message(carrier, accepted)
    db.append_message(
        session_id, "user", carrier["content"],
        display_metadata=carrier["display_metadata"],
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": accepted["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[carrier], session_ids=[session_id],
        admission_id=recovery_id, surface_key=surface,
        accepted_input_durably_committed=True,
    )
    agent = _real_agent(db, session_id, [carrier])
    agent._pending_cli_user_message = carrier
    cli = object.__new__(cli_mod.HermesCLI)
    cli._session_db = db
    cli.session_id = session_id
    cli.agent = agent
    cli.conversation_history = [carrier]

    cli._persist_active_session_before_close()

    stored = db.get_messages_as_conversation(session_id)
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert stored[0]["content"] == (
        "[Your active task list was preserved across context compression]"
    )
    assert stored[0]["display_metadata"]["scaffolding"] == "preserve-me"
    assert pending_input_admission(stored[0]) == (None, None)
    assert stored[1]["content"] == "Continuing from the retained context handoff."
    assert [message["role"] for message in cli.conversation_history] == [
        "user", "assistant",
    ]
    assert pending_input_admission(cli.conversation_history[0]) == (None, None)




def test_cli_close_preflush_resumed_prefix_is_not_duplicated(tmp_path, monkeypatch):
    """A signal during the turn-start flush preserves the old DB prefix once.

    The pause is after ``_persist_session`` records its live snapshot but before
    its normal DB flush. The close helper must retain the distinct CLI baseline.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-close-preflush-resume"
    db.create_session(session_id=session_id, source="cli")
    loaded = [
        {"role": "user", "content": "old prompt"},
        {"role": "assistant", "content": "old answer"},
    ]
    for message in loaded:
        db.append_message(
            session_id=session_id,
            role=message["role"],
            content=message["content"],
        )

    live_messages = list(loaded) + [{"role": "user", "content": "new prompt"}]
    agent = _real_agent(db, session_id, [])
    entered_flush = threading.Event()
    release_flush = threading.Event()
    flush_calls = 0

    def _pause_before_flush(
        messages: list[dict[str, Any]],
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> None:
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 1:
            # The worker has assigned its snapshot and is now paused before its
            # regular DB write. The concurrent close call must stay live.
            agent._session_messages = messages
            entered_flush.set()
            assert release_flush.wait(timeout=5)
        from run_agent import AIAgent

        # Runtime accepts None; the stub keeps that optional contract explicit.
        return AIAgent._flush_messages_to_session_db(
            agent,
            messages,
            conversation_history if conversation_history is not None else [],
        )

    agent._flush_messages_to_session_db = _pause_before_flush
    worker = threading.Thread(
        target=lambda: agent._persist_session(live_messages, loaded),
        daemon=True,
    )
    worker.start()
    assert entered_flush.wait(timeout=5)

    cli = object.__new__(cli_mod.HermesCLI)
    cli.conversation_history = list(loaded) + [{"role": "user", "content": "ui prompt"}]
    cli.session_id = session_id
    cli.agent = agent
    close_started = threading.Event()
    close_finished = threading.Event()

    def _close_while_worker_flushes():
        close_started.set()
        cli._persist_active_session_before_close()
        close_finished.set()

    close_worker = threading.Thread(target=_close_while_worker_flushes, daemon=True)
    close_worker.start()
    assert close_started.wait(timeout=5)
    # The per-agent persistence lock holds the close flush until the normal
    # turn-start write has stamped its durable markers.
    assert not close_finished.wait(timeout=0.1)

    release_flush.set()
    worker.join(timeout=5)
    close_worker.join(timeout=5)
    assert not worker.is_alive()
    assert not close_worker.is_alive()

    stored = db.get_messages_as_conversation(session_id)
    assert [m["content"] for m in stored] == [
        "old prompt",
        "old answer",
        "new prompt",
    ]




def test_cli_close_hands_staged_user_marker_to_turn_start(tmp_path, monkeypatch):
    """A close before turn setup does not duplicate the CLI-staged user row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-close-staged-user"
    db.create_session(session_id=session_id, source="cli")
    prefix = [
        {"role": "user", "content": "old prompt"},
        {"role": "assistant", "content": "old answer"},
    ]
    agent = _real_agent(db, session_id, prefix)
    agent._flush_messages_to_session_db(prefix, [])
    staged = {"role": "user", "content": "new prompt"}
    # `chat()` copies a completed agent transcript before it stages the next
    # user input, so close initially sees the prior agent snapshot only.
    cli_history = list(prefix) + [staged]
    agent._pending_cli_user_message = staged

    cli = object.__new__(cli_mod.HermesCLI)
    cli.conversation_history = cli_history
    cli.session_id = session_id
    cli.agent = agent

    # Close appends only the pending UI dict, while treating the durable prefix
    # as its baseline. Turn setup then reuses the marked dict without re-writing.
    cli._persist_active_session_before_close()
    assert staged["_db_persisted"] is True

    worker_messages = list(prefix) + [staged]
    agent._persist_session(worker_messages, prefix)

    stored = db.get_messages_as_conversation(session_id)
    assert [m["content"] for m in stored] == [
        "old prompt",
        "old answer",
        "new prompt",
    ]


def test_cli_close_never_reappends_durable_inert_recovery(tmp_path, monkeypatch):
    """Worker-return/renderer gap: clean close retains recovery, not an active row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import (
        get_pending_input_recovery, stage_pending_input_recovery,
        stamp_pending_input_admission,
    )
    from hermes_cli.prompt_stash import PromptStash
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-close-recovered-input"
    db.create_session(session_id=session_id, source="cli")
    staged = {"role": "user", "content": "retry me explicitly"}
    stamp_pending_input_admission(
        staged, admission_id="admission-cli-close", surface_key=f"cli:{session_id}",
    )
    accepted = {
        "version": 1, "kind": "accepted_user_input", "text": staged["content"],
        "input_kind": "plain_text", "exact": True, "lossy": False,
        "loss_reasons": [], "safe_to_replay": True,
        "provider_call_sent": False, "executable": False,
    }
    stage_pending_input_recovery(
        db,
        accepted_input=accepted,
        live_messages=[staged],
        session_ids=[session_id],
        admission_id="admission-cli-close",
        surface_key=f"cli:{session_id}",
        accepted_input_durably_committed=False,
    )
    agent = _real_agent(db, session_id, [staged])
    agent._pending_cli_user_message = staged
    cli = object.__new__(cli_mod.HermesCLI)
    cli.conversation_history = [staged]
    cli.session_id = session_id
    cli.agent = agent
    cli._session_db = db
    cli._prompt_stash = PromptStash()

    cli._persist_active_session_before_close()

    assert db.get_messages_as_conversation(session_id) == []
    assert agent._pending_cli_user_message is None
    assert cli.conversation_history == []
    assert get_pending_input_recovery(db, "admission-cli-close")["status"] == "pending"

    # A fresh process can project the record into its composer, but does not run it.
    restored = object.__new__(cli_mod.HermesCLI)
    restored._session_db = db
    restored._prompt_stash = PromptStash()
    restored._pending_input_recoveries_restored = False
    assert restored._restore_pending_input_recoveries_to_stash() == 1
    assert restored._prompt_stash.peek().text == "retry me explicitly"
    assert get_pending_input_recovery(db, "admission-cli-close")["status"] == "pending"


def test_cli_duplicate_text_projection_claims_exact_recovery_id(tmp_path, monkeypatch):
    """PromptStash text is paired with one durable ID, never text-searched."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import (
        get_pending_input_recovery, stage_pending_input_recovery,
        stamp_pending_input_admission,
    )
    from hermes_cli.prompt_stash import PromptStash
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-duplicate-recovery-text"
    db.create_session(session_id=session_id, source="cli")
    text = "same visible draft"
    for admission_id in ("duplicate-a", "duplicate-b"):
        staged = {"role": "user", "content": text}
        stamp_pending_input_admission(
            staged, admission_id=admission_id, surface_key=f"cli:{session_id}",
        )
        stage_pending_input_recovery(
            db,
            accepted_input={
                "version": 1, "kind": "accepted_user_input", "text": text,
                "input_kind": "plain_text", "exact": True, "lossy": False,
                "loss_reasons": [], "safe_to_replay": True,
                "provider_call_sent": False, "executable": False,
            },
            live_messages=[staged], session_ids=[session_id],
            admission_id=admission_id, surface_key=f"cli:{session_id}",
            accepted_input_durably_committed=False,
        )

    cli = object.__new__(cli_mod.HermesCLI)
    cli._session_db = db
    cli._prompt_stash = PromptStash()
    cli._pending_input_recoveries_restored = False
    cli.session_id = session_id
    cli.conversation_history = []
    agent = _real_agent(db, session_id, [])
    cli.agent = agent

    assert cli._restore_pending_input_recoveries_to_stash() == 2
    assert [entry.text for entry in cli._prompt_stash.items] == [text, text]
    projected_entry = cli._prompt_stash.peek()
    projected_id = projected_entry.recovery_admission_id
    other_id = ({"duplicate-a", "duplicate-b"} - {projected_id}).pop()

    # Typing the same characters without restoring this entry carries no
    # provenance and cannot claim either durable record.
    cli._chat_stage_user_message(agent, text)
    assert get_pending_input_recovery(db, projected_id)["status"] == "pending"
    assert get_pending_input_recovery(db, other_id)["status"] == "pending"
    cli.conversation_history = []
    agent._session_messages = []
    agent._pending_cli_user_message = None

    cli._chat_stage_user_message(
        agent,
        text,
        recovery_admission_id=projected_id,
        recovery_surface_key=projected_entry.recovery_surface_key,
    )

    assert get_pending_input_recovery(db, projected_id)["status"] == "claimed"
    assert get_pending_input_recovery(db, other_id)["status"] == "pending"
    # Model a process death after the worker durably flushed the successor but
    # before provider entry. A graceful close now correctly returns this claim
    # to pending, so it cannot stand in for the crash boundary.
    agent._flush_messages_to_session_db(cli.conversation_history, [])
    assert get_pending_input_recovery(db, projected_id)["status"] == "claimed"
    assert get_pending_input_recovery(db, other_id)["status"] == "pending"
    assert [m["content"] for m in db.get_messages_as_conversation(session_id)] == [text]

    claimed_at = get_pending_input_recovery(db, projected_id)["claimed_at"]
    monkeypatch.setattr(
        "agent.pending_input_recovery.time.time", lambda: claimed_at + 31.0,
    )
    # This in-process test simulates a fresh process; make the prior claimant's
    # PID liveness match that crash/restart boundary.
    monkeypatch.setattr(
        "agent.pending_input_recovery._claim_owner_is_alive", lambda *_args: False,
    )

    # A restart retry takes over the exact claim and first withdraws the prior
    # unentered successor, keeping one active/model-visible copy.
    restarted = object.__new__(cli_mod.HermesCLI)
    restarted._session_db = db
    restarted._prompt_stash = PromptStash()
    restarted._pending_input_recoveries_restored = False
    restarted.session_id = session_id
    restarted.conversation_history = []
    restarted.agent = _real_agent(db, session_id, [])
    assert restarted._restore_pending_input_recoveries_to_stash() == 2
    projected_entry = next(
        entry for entry in restarted._prompt_stash.items
        if entry.recovery_admission_id == projected_id
    )
    restarted._chat_stage_user_message(
        restarted.agent,
        text,
        recovery_admission_id=projected_id,
        recovery_surface_key=projected_entry.recovery_surface_key,
    )
    assert db.get_messages_as_conversation(session_id) == []
    restarted.agent._flush_messages_to_session_db(
        restarted.conversation_history, [],
    )
    assert [m["content"] for m in db.get_messages_as_conversation(session_id)] == [text]

    from agent.conversation_loop import _ack_pending_input_recovery_after_provider_response
    from agent.pending_input_recovery import mark_pending_input_recovery_dispatch_started

    resend_id = restarted.agent._pending_input_recovery_resend_id
    mark_pending_input_recovery_dispatch_started(
        db, projected_id, resend_id=resend_id,
    )
    from agent.pending_input_recovery import mark_pending_input_recovery_provider_returned

    mark_pending_input_recovery_provider_returned(
        db, projected_id, resend_id=resend_id,
    )
    db.append_message(session_id, "assistant", "done")
    _ack_pending_input_recovery_after_provider_response(restarted.agent)
    assert get_pending_input_recovery(db, projected_id)["status"] == "consumed"


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("delivered", "consumed"), ("retry", "pending")],
)
def test_recover_resolution_withdraws_restart_resend_everywhere(
    tmp_path, monkeypatch, action, expected_status,
):
    """Explicit resolution cannot leave a restart-restored resend executable."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
        mark_pending_input_recovery_provider_returned,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )
    from hermes_cli.prompt_stash import PromptStash
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-recover-restart-row"
    recovery_id = "admission-restart"
    resend_id = "resend-restart"
    surface = f"cli:{session_id}"
    db.create_session(session_id=session_id, source="cli")

    original = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        original, admission_id=recovery_id, surface_key=surface,
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1,
            "kind": "accepted_user_input",
            "text": "deploy once",
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "loss_reasons": [],
            "safe_to_replay": True,
            "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original],
        session_ids=[session_id],
        admission_id=recovery_id,
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, recovery_id, resend_id=resend_id, claim_session_id=session_id,
    )
    mark_pending_input_recovery_dispatch_started(
        db, recovery_id, resend_id=resend_id,
    )

    resend = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        resend, admission_id=resend_id, surface_key=surface,
    )
    db.append_message(
        session_id,
        "user",
        resend["content"],
        display_metadata=resend["display_metadata"],
    )
    cli = object.__new__(cli_mod.HermesCLI)
    cli._session_db = db
    cli._prompt_stash = PromptStash()
    cli._composer_recovery_entry = None
    cli.conversation_history = [resend]
    cli.agent = SimpleNamespace(_session_messages=[resend])

    cli._handle_recover_command(f"/recover {recovery_id} {action}")

    assert get_pending_input_recovery(db, recovery_id)["status"] == expected_status
    assert db.get_messages_as_conversation(session_id) == []
    assert cli.conversation_history == []
    assert cli.agent._session_messages == []
    if action == "retry":
        assert cli._prompt_stash.peek().recovery_admission_id == recovery_id


def test_recover_delivered_preserves_durable_answered_pair_in_cli_history(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
        mark_pending_input_recovery_provider_returned,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )
    from hermes_cli.prompt_stash import PromptStash
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-recover-answered"
    surface = f"cli:{session_id}"
    db.create_session(session_id=session_id, source="cli")
    original = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        original, admission_id="recovery-answered", surface_key=surface,
    )
    stage_pending_input_recovery(
        db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": original["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original],
        session_ids=[session_id],
        admission_id="recovery-answered",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        db, "recovery-answered", resend_id="resend-answered",
        claim_session_id=session_id,
    )
    resend = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        resend, admission_id="resend-answered", surface_key=surface,
    )
    answer = {"role": "assistant", "content": "deployed"}
    db.append_message(
        session_id, "user", resend["content"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        db, "recovery-answered", resend_id="resend-answered",
    )
    mark_pending_input_recovery_provider_returned(
        db, "recovery-answered", resend_id="resend-answered",
    )
    db.append_message(session_id, "assistant", answer["content"])

    cli = object.__new__(cli_mod.HermesCLI)
    cli._session_db = db
    cli._prompt_stash = PromptStash()
    cli._composer_recovery_entry = None
    cli.conversation_history = [resend, answer]
    cli.agent = SimpleNamespace(_session_messages=[resend, answer])

    cli._handle_recover_command("/recover recovery-answered delivered")

    record = get_pending_input_recovery(db, "recovery-answered")
    assert record["status"] == "consumed"
    assert record["provider_response_received"] is True
    assert cli.conversation_history == [resend, answer]
    assert cli.agent._session_messages == [resend, answer]
    assert [
        (message["role"], message["content"])
        for message in db.get_messages_as_conversation(session_id)
    ] == [("user", "deploy once"), ("assistant", "deployed")]






def test_cli_close_uses_clean_override_for_shortened_pending_snapshot(tmp_path, monkeypatch):
    """Close retains the clean user text when its snapshot omits the prefix."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import cli as cli_mod
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-close-shortened-noted-pending"
    db.create_session(session_id=session_id, source="cli")
    prefix = [
        {"role": "user", "content": "old prompt"},
        {"role": "assistant", "content": "old answer"},
    ]
    for message in prefix:
        db.append_message(
            session_id=session_id,
            role=message["role"],
            content=message["content"],
        )

    agent = _real_agent(db, session_id, [])
    staged = {"role": "user", "content": "[MODEL NOTE]\n\nnew prompt"}
    agent._pending_cli_user_message = staged
    # The normal worker index is relative to the full resumed history, while a
    # close before its first persistence flush sees only this staged dict.
    agent._persist_user_message_idx = len(prefix)
    agent._persist_user_message_override = "new prompt"
    agent._persist_user_message_timestamp = None

    cli = object.__new__(cli_mod.HermesCLI)
    cli.conversation_history = list(prefix) + [staged]
    cli.session_id = session_id
    cli.agent = agent

    cli._persist_active_session_before_close()

    assert [m["content"] for m in db.get_messages_as_conversation(session_id)] == [
        "old prompt",
        "old answer",
        "new prompt",
    ]
    assert staged["_db_persisted"] is True




def test_cli_close_builds_prompt_before_creating_first_session_row(tmp_path, monkeypatch):
    """First-turn close persistence must not leave a NULL prompt snapshot."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import agent.conversation_loop as loop_mod
    import cli as cli_mod
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "cli-close-first-turn"
    agent = _real_agent(db, session_id, [])
    agent._session_db_created = False
    agent._cached_system_prompt = None
    staged = {"role": "user", "content": "first prompt"}
    agent._pending_cli_user_message = staged

    def _build_prompt(target, _system_message, _history):
        target._cached_system_prompt = "close-built-system-prompt"

    monkeypatch.setattr(loop_mod, "_restore_or_build_system_prompt", _build_prompt)

    cli = object.__new__(cli_mod.HermesCLI)
    cli.conversation_history = [staged]
    cli.session_id = session_id
    cli.agent = agent

    cli._persist_active_session_before_close()

    session = db.get_session(session_id)
    assert session is not None
    assert session["system_prompt"] == "close-built-system-prompt"
    assert [m["content"] for m in db.get_messages_as_conversation(session_id)] == [
        "first prompt"
    ]
