"""Regression tests for CLI fresh-session commands."""

from __future__ import annotations

import importlib
import os
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from tools.todo_tool import TodoStore


class _FakeCompressor:
    """Minimal stand-in for ContextCompressor."""

    def __init__(self):
        self.last_prompt_tokens = 500
        self.last_completion_tokens = 200
        self.last_total_tokens = 700
        self.compression_count = 3
        self._context_probed = True


class _FakeAgent:
    def __init__(self, session_id: str, session_start):
        self.session_id = session_id
        self.session_start = session_start
        self.model = "anthropic/claude-opus-4.6"
        self._last_flushed_db_idx = 7
        self._todo_store = TodoStore()
        self._todo_store.write(
            [{"id": "t1", "content": "unfinished task", "status": "in_progress"}]
        )
        self.commit_memory_session = MagicMock()
        self._invalidate_system_prompt = MagicMock()

        # Token counters (non-zero to verify reset)
        self.session_total_tokens = 1000
        self.session_input_tokens = 600
        self.session_output_tokens = 400
        self.session_prompt_tokens = 550
        self.session_completion_tokens = 350
        self.session_cache_read_tokens = 100
        self.session_cache_write_tokens = 50
        self.session_reasoning_tokens = 80
        self.session_api_calls = 5
        self.session_estimated_cost_usd = 0.42
        self.session_cost_status = "estimated"
        self.session_cost_source = "openrouter"
        self.context_compressor = _FakeCompressor()

    def reset_session_state(self):
        """Mirror the real AIAgent.reset_session_state()."""
        self.session_total_tokens = 0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_api_calls = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.session_cost_source = "none"
        if hasattr(self, "context_compressor") and self.context_compressor:
            self.context_compressor.last_prompt_tokens = 0
            self.context_compressor.last_completion_tokens = 0
            self.context_compressor.last_total_tokens = 0
            self.context_compressor.compression_count = 0
            self.context_compressor._context_probed = False


def _make_cli(env_overrides=None, config_overrides=None, **kwargs):
    """Create a HermesCLI instance with minimal mocking."""
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    if config_overrides:
        _clean_config.update(config_overrides)
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    if env_overrides:
        clean_env.update(env_overrides)
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as _cli_mod

        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            _cli_mod.__dict__, {"CLI_CONFIG": _clean_config}
        ):
            return _cli_mod.HermesCLI(**kwargs)


def _prepare_cli_with_active_session(tmp_path):
    cli = _make_cli()
    cli._session_db = SessionDB(db_path=tmp_path / "state.db")
    cli._session_db.create_session(session_id=cli.session_id, source="cli", model=cli.model)

    cli.agent = _FakeAgent(cli.session_id, cli.session_start)
    cli.conversation_history = [{"role": "user", "content": "hello"}]

    old_session_start = cli.session_start - timedelta(seconds=1)
    cli.session_start = old_session_start
    cli.agent.session_start = old_session_start

    # Bypass the destructive-slash confirmation gate — these tests focus on
    # the new-session mechanics, not the confirm prompt itself (covered in
    # tests/cli/test_destructive_slash_confirm.py).
    cli._confirm_destructive_slash = lambda *_a, **_kw: "once"
    return cli


@pytest.fixture(autouse=True)
def _reset_session_id_context():
    from gateway.session_context import _UNSET, _VAR_MAP

    yield
    os.environ.pop("HERMES_SESSION_ID", None)
    _VAR_MAP["HERMES_SESSION_ID"].set(_UNSET)


def test_new_command_creates_real_fresh_session_and_resets_agent_state(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    old_session_start = cli.session_start

    cli.process_command("/new")

    assert cli.session_id != old_session_id

    old_session = cli._session_db.get_session(old_session_id)
    assert old_session is not None
    assert old_session["end_reason"] == "new_session"

    new_session = cli._session_db.get_session(cli.session_id)
    assert new_session is not None

    cli._session_db.append_message(cli.session_id, role="user", content="next turn")

    assert cli.agent.session_id == cli.session_id
    assert cli.agent._last_flushed_db_idx == 0
    assert cli.agent._todo_store.read() == []
    assert cli.session_start > old_session_start
    assert cli.agent.session_start == cli.session_start
    cli.agent._invalidate_system_prompt.assert_called_once()






def test_new_session_delivers_context_engine_boundary_synchronously(tmp_path):
    """The context-engine on_session_end must fire during /new itself.

    It is cheap local state work and ordering-sensitive: it must land before
    reset_session_state() rebinds the engine to the new session. The LLM-bound
    provider extraction is what gets deferred, not this."""
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id

    engine_calls = []
    cli.agent.context_compressor.on_session_end = (
        lambda sid, msgs: engine_calls.append((sid, list(msgs)))
    )

    cli.process_command("/new")

    assert engine_calls == [(old_session_id, [{"role": "user", "content": "hello"}])]


def test_run_cleanup_flushes_pending_memory_manager_work(tmp_path):
    """A '/new then quit' must not drop the queued old-session extraction.

    _run_cleanup gives the manager's serialized worker a bounded drain via
    flush_pending() before shutdown_all()'s short-fuse drain runs."""
    import cli as _cli_mod

    agent = MagicMock()
    mm = MagicMock()
    mm.flush_pending.return_value = True
    agent._memory_manager = mm
    agent._session_messages = []

    old_ref = _cli_mod._active_agent_ref
    _cli_mod._active_agent_ref = agent
    _cli_mod._cleanup_done = False
    try:
        _cli_mod._run_cleanup(notify_session_finalize=False)
    finally:
        _cli_mod._cleanup_done = True
        _cli_mod._active_agent_ref = old_ref

    mm.flush_pending.assert_called_once_with(timeout=10)






def test_clear_command_starts_new_session_before_redrawing(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.console = MagicMock()
    cli.show_banner = MagicMock()

    old_session_id = cli.session_id
    cli.process_command("/clear")

    assert cli.session_id != old_session_id
    assert cli._session_db.get_session(old_session_id)["end_reason"] == "new_session"
    assert cli._session_db.get_session(cli.session_id) is not None
    cli.console.clear.assert_called_once()
    cli.show_banner.assert_called_once()
    assert cli.conversation_history == []


@pytest.mark.parametrize("command", ["/new", "/clear"])
def test_session_boundary_returns_dispatch_none_recovery_to_pending(
    tmp_path, command,
):
    """A composer claim remains explicitly retryable after /new or /clear."""
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    surface = f"cli:{old_session_id}"
    recovery_id = "session-boundary-recovery"
    resend_id = "session-boundary-resend"
    original = {"role": "user", "content": "retry after rotation"}
    stamp_pending_input_admission(
        original, admission_id=recovery_id, surface_key=surface,
    )
    stage_pending_input_recovery(
        cli._session_db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": original["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original], session_ids=[old_session_id],
        admission_id=recovery_id, surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        cli._session_db, recovery_id, resend_id=resend_id,
        claim_session_id=old_session_id,
    )
    resend = {"role": "user", "content": original["content"]}
    stamp_pending_input_admission(
        resend, admission_id=resend_id, surface_key=surface,
    )
    cli._session_db.append_message(
        old_session_id, "user", resend["content"],
        display_metadata=resend["display_metadata"],
    )
    cli.conversation_history = [resend]
    cli.agent._session_db = cli._session_db
    cli.agent._session_persist_lock = threading.RLock()
    cli.agent._active_session_turn_lease_holder = None
    cli.agent._session_messages = [resend]
    cli.agent._pending_cli_user_message = resend
    cli.agent._pending_input_recovery_claim_id = recovery_id
    cli.agent._pending_input_recovery_resend_id = resend_id
    cli.agent._pending_input_recovery_provider_completed = None
    if command == "/clear":
        cli.console = MagicMock()
        cli.show_banner = MagicMock()

    cli.process_command(command)

    record = get_pending_input_recovery(cli._session_db, recovery_id)
    assert record["status"] == "pending"
    assert record["previous_resend_id"] == resend_id
    assert cli._session_db.get_messages_as_conversation(old_session_id) == []
    assert cli.agent._pending_input_recovery_claim_id is None
    assert cli.agent._pending_input_recovery_resend_id is None
    assert cli.agent._pending_cli_user_message is None
    projected = [
        entry for entry in cli._prompt_stash.items
        if entry.recovery_admission_id == recovery_id
    ]
    assert len(projected) == 1
    assert cli.conversation_history == []


def test_new_session_preserves_merged_compaction_carrier_and_bridge(tmp_path):
    from agent.conversation_compression import _merge_anchor_into_user_message
    from agent.pending_input_recovery import (
        pending_input_admission,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    surface = f"cli:{old_session_id}"
    carrier = {
        "role": "user",
        "content": "[Your active task list was preserved across context compression]",
        "_todo_snapshot_synthetic": True,
        "display_metadata": {"scaffolding": "keep-at-new-boundary"},
    }
    accepted = {"role": "user", "content": "finish the deployment"}
    stamp_pending_input_admission(
        accepted, admission_id="merged-new-recovery", surface_key=surface,
    )
    _merge_anchor_into_user_message(carrier, accepted)
    cli._session_db.append_message(
        old_session_id, "user", carrier["content"],
        display_metadata=carrier["display_metadata"],
    )
    stage_pending_input_recovery(
        cli._session_db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input",
            "text": accepted["content"], "input_kind": "plain_text",
            "exact": True, "lossy": False, "loss_reasons": [],
            "safe_to_replay": True, "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[carrier], session_ids=[old_session_id],
        admission_id="merged-new-recovery", surface_key=surface,
        accepted_input_durably_committed=True,
    )
    cli.conversation_history = [carrier]
    cli.agent._session_db = cli._session_db
    cli.agent._session_persist_lock = threading.RLock()
    cli.agent._active_session_turn_lease_holder = None
    cli.agent._session_messages = [carrier]
    cli.agent._pending_cli_user_message = carrier

    cli.process_command("/new")

    stored = cli._session_db.get_messages_as_conversation(old_session_id)
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert stored[0]["content"] == (
        "[Your active task list was preserved across context compression]"
    )
    assert stored[0]["display_metadata"]["scaffolding"] == (
        "keep-at-new-boundary"
    )
    assert pending_input_admission(stored[0]) == (None, None)
    assert stored[1]["content"] == "Continuing from the retained context handoff."


@pytest.mark.parametrize("command", ["/new", "/clear"])
def test_new_and_clear_preserve_inert_timeout_recovery_without_submitting(
    tmp_path, command,
):
    from agent.pending_input_recovery import (
        get_pending_input_recovery, stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    cli = _prepare_cli_with_active_session(tmp_path)
    if command == "/clear":
        cli.console = MagicMock()
        cli.show_banner = MagicMock()
    old_session_id = cli.session_id
    staged = {"role": "user", "content": "resume this manually"}
    stamp_pending_input_admission(
        staged, admission_id="admission-new-clear", surface_key=f"cli:{old_session_id}",
    )
    stage_pending_input_recovery(
        cli._session_db,
        accepted_input={
            "version": 1, "kind": "accepted_user_input", "text": staged["content"],
            "input_kind": "plain_text", "exact": True, "lossy": False,
            "loss_reasons": [], "safe_to_replay": True,
            "provider_call_sent": False, "executable": False,
        },
        live_messages=[staged],
        session_ids=[old_session_id],
        admission_id="admission-new-clear",
        surface_key=f"cli:{old_session_id}",
        accepted_input_durably_committed=False,
    )
    cli.conversation_history = [staged]
    cli.agent._session_messages = [staged]
    cli.agent._pending_cli_user_message = staged
    queued_before = cli._pending_input.qsize()

    cli.process_command(command)

    assert cli.session_id != old_session_id
    assert cli.conversation_history == []
    assert cli._pending_input.qsize() == queued_before
    assert get_pending_input_recovery(
        cli._session_db, "admission-new-clear"
    )["status"] == "pending"
    assert cli._prompt_stash.peek().text == "resume this manually"


def test_new_reconciles_ack_failure_without_orphaning_answered_pair(tmp_path):
    from agent.pending_input_recovery import (
        claim_pending_input_recovery,
        get_pending_input_recovery,
        mark_pending_input_recovery_dispatch_started,
        mark_pending_input_recovery_provider_returned,
        stage_pending_input_recovery,
        stamp_pending_input_admission,
    )

    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    surface = f"cli:{old_session_id}"
    original = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        original, admission_id="recovery-answered", surface_key=surface,
    )
    stage_pending_input_recovery(
        cli._session_db,
        accepted_input={
            "version": 1,
            "kind": "accepted_user_input",
            "text": original["content"],
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "loss_reasons": [],
            "safe_to_replay": True,
            "provider_call_sent": False,
            "executable": False,
        },
        live_messages=[original],
        session_ids=[old_session_id],
        admission_id="recovery-answered",
        surface_key=surface,
        accepted_input_durably_committed=False,
    )
    claim_pending_input_recovery(
        cli._session_db,
        "recovery-answered",
        resend_id="resend-answered",
        claim_session_id=old_session_id,
    )
    resend = {"role": "user", "content": "deploy once"}
    stamp_pending_input_admission(
        resend, admission_id="resend-answered", surface_key=surface,
    )
    answer = {"role": "assistant", "content": "deployed"}
    cli._session_db.append_message(
        old_session_id,
        "user",
        resend["content"],
        display_metadata=resend["display_metadata"],
    )
    mark_pending_input_recovery_dispatch_started(
        cli._session_db,
        "recovery-answered",
        resend_id="resend-answered",
    )
    mark_pending_input_recovery_provider_returned(
        cli._session_db,
        "recovery-answered",
        resend_id="resend-answered",
    )
    cli._session_db.append_message(
        old_session_id, "assistant", answer["content"],
    )
    cli.conversation_history = [resend, answer]
    cli.agent._session_messages = [resend, answer]
    boundary = []
    cli.agent.context_compressor.on_session_end = (
        lambda _sid, messages: boundary.extend(messages)
    )

    cli.process_command("/new")

    record = get_pending_input_recovery(cli._session_db, "recovery-answered")
    assert record["status"] == "consumed"
    assert record["provider_response_received"] is True
    assert [
        (message["role"], message["content"])
        for message in cli._session_db.get_messages_as_conversation(old_session_id)
    ] == [("user", "deploy once"), ("assistant", "deployed")]
    assert [(message["role"], message["content"]) for message in boundary] == [
        ("user", "deploy once"), ("assistant", "deployed"),
    ]


def test_new_session_resets_token_counters(tmp_path):
    """Regression test for #2099: /new must zero all token counters."""
    cli = _prepare_cli_with_active_session(tmp_path)

    # Verify counters are non-zero before reset
    agent = cli.agent
    assert agent.session_total_tokens > 0
    assert agent.session_api_calls > 0
    assert agent.context_compressor.compression_count > 0

    cli.process_command("/new")

    # All agent token counters must be zero
    assert agent.session_total_tokens == 0
    assert agent.session_input_tokens == 0
    assert agent.session_output_tokens == 0
    assert agent.session_prompt_tokens == 0
    assert agent.session_completion_tokens == 0
    assert agent.session_cache_read_tokens == 0
    assert agent.session_cache_write_tokens == 0
    assert agent.session_reasoning_tokens == 0
    assert agent.session_api_calls == 0
    assert agent.session_estimated_cost_usd == 0.0
    assert agent.session_cost_status == "unknown"
    assert agent.session_cost_source == "none"

    # Context compressor counters must also be zero
    comp = agent.context_compressor
    assert comp.last_prompt_tokens == 0
    assert comp.last_completion_tokens == 0
    assert comp.last_total_tokens == 0
    assert comp.compression_count == 0
    assert comp._context_probed is False


def test_new_session_with_title(capsys):
    """new_session(title=...) creates a session and sets the title."""
    cli = _make_cli()
    cli._session_db = MagicMock()
    cli.agent = _FakeAgent("old_session_id", datetime.now())
    cli.conversation_history = []

    cli.new_session(title="My Test Session")

    # Assert set_session_title was called with the new session ID and sanitized title
    cli._session_db.set_session_title.assert_called_once()
    call_args = cli._session_db.set_session_title.call_args
    assert call_args[0][0] == cli.session_id
    assert call_args[0][1] == "My Test Session"

    captured = capsys.readouterr()
    assert "My Test Session" in captured.out
