"""Tests for the Ctrl+S prompt stash wiring inside HermesCLI.

The state machine itself is covered by tests/cli/test_prompt_stash.py. These
tests verify the cli.py side:
  - HermesCLI.__init__ creates a PromptStash
  - the layout hook makes room for the stash browse panel
  - _render_stash_panel renders bounded, display-width-correct rows
  - the status-bar indicator appears / disappears with stash contents

Follows the prompt_toolkit-stub construction pattern from
tests/cli/test_cli_extension_hooks.py.
"""

from __future__ import annotations

import importlib
import queue
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_cli(**kwargs):
    """Create a HermesCLI with prompt_toolkit stubbed out."""
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
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
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


@pytest.fixture(scope="module")
def cli():
    return _make_cli()


class TestStashStateInit:
    def test_cli_has_prompt_stash(self, cli):
        assert hasattr(cli, "_prompt_stash")

    def test_stash_starts_empty(self, cli):
        # Duck-typed rather than isinstance: _make_cli reloads the `cli`
        # module, which re-imports hermes_cli.prompt_toolkit stubs and can
        # yield a distinct-but-equivalent PromptStash class object.
        stash = cli._prompt_stash
        assert type(stash).__name__ == "PromptStash"
        assert len(stash) == 0
        assert stash.panel_open is False
        assert stash.indicator() == ""
        assert stash.placeholder_hint() == ""

    def test_stash_is_per_instance_not_shared(self):
        """Two CLIs must not share one stash — drafts would leak across sessions."""
        a = _make_cli()
        b = _make_cli()
        a._prompt_stash.stash("only in a")
        assert len(a._prompt_stash) == 1
        assert len(b._prompt_stash) == 0


class TestRecoveryComposerIdentity:
    @staticmethod
    def _event(text=""):
        buffer = MagicMock()
        buffer.text = text
        buffer.cursor_position = len(text)
        app = MagicMock()
        app.current_buffer = buffer
        return MagicMock(app=app), buffer

    def test_restore_carries_entry_identity_through_submission(self):
        cli = _make_cli()
        cli._prompt_stash.clear()
        cli._attached_images = []
        cli._prompt_stash.stash(
            "retry exactly",
            recovery_admission_id="recovery-a",
            recovery_surface_key="cli:session-a",
        )
        event, buffer = self._event()
        cli._tui_restore_stash_payload(event, cli._prompt_stash.pop())
        assert cli._composer_recovery_entry.recovery_admission_id == "recovery-a"

        cli._tui_multiline_shortcuts = False
        cli._tui_enter_overlay = MagicMock(return_value=False)
        cli._tui_enter_inline_command = MagicMock(return_value=False)
        cli._agent_running = False
        cli._pending_input = queue.Queue()
        cli._inline_pastes = MagicMock()
        cli._tui_handle_enter(event)

        submitted = cli._pending_input.get_nowait()
        assert submitted == "retry exactly"
        assert submitted.recovery_admission_id == "recovery-a"
        assert submitted.recovery_surface_key == "cli:session-a"
        assert cli._composer_recovery_entry is None
        buffer.reset.assert_called_once_with(append_to_history=True)

    def test_restored_whitespace_recovery_submits_exact_text_and_identity(self):
        cli = _make_cli()
        cli._prompt_stash.clear()
        cli._attached_images = []
        raw = "  retry exactly\n"
        cli._prompt_stash.stash(
            raw,
            recovery_admission_id="recovery-whitespace",
            recovery_surface_key="cli:session-a",
        )
        event, _buffer = self._event()
        cli._tui_restore_stash_payload(event, cli._prompt_stash.pop())
        cli._tui_multiline_shortcuts = False
        cli._tui_enter_overlay = MagicMock(return_value=False)
        cli._tui_enter_inline_command = MagicMock(return_value=False)
        cli._agent_running = False
        cli._pending_input = queue.Queue()
        cli._inline_pastes = MagicMock()

        cli._tui_handle_enter(event)

        submitted = cli._pending_input.get_nowait()
        assert str(submitted) == raw
        assert submitted.recovery_admission_id == "recovery-whitespace"
        assert submitted.recovery_surface_key == "cli:session-a"

    def test_edit_then_retype_same_text_is_ordinary(self):
        cli = _make_cli()
        cli._prompt_stash.clear()
        cli._attached_images = []
        cli._prompt_stash.stash(
            "same text",
            recovery_admission_id="recovery-a",
            recovery_surface_key="cli:session-a",
        )
        event, buffer = self._event()
        cli._tui_restore_stash_payload(event, cli._prompt_stash.pop())
        cli._tui_reconcile_prompt_recoveries = MagicMock(return_value=0)
        buffer.text = "edited"
        cli._tui_prev_text_len = 0
        cli._tui_prev_newline_count = 0
        cli._tui_paste_just_collapsed = False
        cli._skip_paste_collapse = False
        cli._tui_on_text_changed(buffer)
        cli._tui_reconcile_prompt_recoveries.assert_called_once_with()
        buffer.text = "same text"

        cli._tui_multiline_shortcuts = False
        cli._tui_enter_overlay = MagicMock(return_value=False)
        cli._tui_enter_inline_command = MagicMock(return_value=False)
        cli._agent_running = False
        cli._pending_input = queue.Queue()
        cli._inline_pastes = MagicMock()
        cli._tui_handle_enter(event)

        submitted = cli._pending_input.get_nowait()
        assert type(submitted) is str
        assert not hasattr(submitted, "recovery_admission_id")

    def test_restored_recovery_can_be_stashed_again_without_losing_identity(self):
        cli = _make_cli()
        cli._attached_images = []
        cli._prompt_stash.stash(
            "retry later",
            recovery_admission_id="recovery-a",
            recovery_surface_key="cli:session-a",
        )
        event, buffer = self._event()
        cli._tui_restore_stash_payload(event, cli._prompt_stash.pop())

        cli._tui_handle_prompt_stash(event)

        entry = cli._prompt_stash.peek()
        assert entry.text == "retry later"
        assert entry.recovery_admission_id == "recovery-a"
        assert entry.recovery_surface_key == "cli:session-a"
        assert cli._composer_recovery_entry is None
        buffer.reset.assert_called_once_with()

    def test_restored_recovery_forces_queue_while_agent_busy(self):
        cli = _make_cli()
        cli._prompt_stash.clear()
        cli._attached_images = []
        cli._prompt_stash.stash(
            "retry later",
            recovery_admission_id="recovery-a",
            recovery_surface_key="cli:session-a",
        )
        event, _buffer = self._event()
        cli._tui_restore_stash_payload(event, cli._prompt_stash.pop())
        cli._tui_multiline_shortcuts = False
        cli._tui_enter_overlay = MagicMock(return_value=False)
        cli._tui_enter_inline_command = MagicMock(return_value=False)
        cli._agent_running = True
        cli.busy_input_mode = "steer"
        cli.agent = MagicMock()
        cli._pending_input = queue.Queue()
        cli._inline_pastes = MagicMock()
        cli._tui_handle_enter(event)

        submitted = cli._pending_input.get_nowait()
        assert submitted.recovery_admission_id == "recovery-a"
        cli.agent.steer.assert_not_called()

    def test_process_sanitizers_preserve_only_byte_identical_submission(self):
        from hermes_cli.prompt_stash import ComposerSubmission

        cli = _make_cli()
        submitted = ComposerSubmission(
            "retry exactly",
            recovery_admission_id="recovery-a",
            recovery_surface_key="cli:session-a",
        )
        cli._tui_unwrap_input = MagicMock(
            return_value=(submitted, False, False)
        )
        cli._typed_voice_stop = MagicMock(return_value=False)
        cli._pending_resume_sessions = None
        cli.handle_bang_shell = MagicMock(return_value=False)
        cli._print_user_message_preview = MagicMock()
        cli._turn_summary_begin = MagicMock()
        cli._app = MagicMock()
        cli.chat = MagicMock()
        cli._tui_after_turn = MagicMock()

        cli._tui_process_one_input(submitted)

        actual = cli.chat.call_args.args[0]
        assert actual == "retry exactly"
        assert actual.recovery_admission_id == "recovery-a"
        assert actual.recovery_surface_key == "cli:session-a"

    def test_duplicate_text_recoveries_are_distinct_and_idempotent(self):
        cli = _make_cli()
        base = {
            "version": 1,
            "kind": "accepted_user_input",
            "status": "pending",
            "safe_to_replay": True,
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "provider_call_sent": False,
            "text": "identical",
            "surface_key": "cli:session-a",
        }
        assert cli._chat_project_pending_input_recovery({**base, "admission_id": "a"})
        assert cli._chat_project_pending_input_recovery({**base, "admission_id": "b"})
        assert not cli._chat_project_pending_input_recovery({**base, "admission_id": "a"})
        assert [entry.recovery_admission_id for entry in cli._prompt_stash.items] == ["b", "a"]

        cli._prompt_stash.open_panel()
        cli._prompt_stash.move_cursor(1)
        assert cli._prompt_stash.restore_at_cursor() == ("identical", [])
        assert cli._prompt_stash.take_last_restored_entry().recovery_admission_id == "a"
        assert cli._prompt_stash.peek().recovery_admission_id == "b"

    @pytest.mark.parametrize(
        "changes",
        [
            {"input_kind": "structured", "safe_to_replay": False},
            {"dispatch_state": "started"},
            {"exact": False, "lossy": True},
        ],
    )
    def test_structured_lossy_or_uncertain_recovery_is_not_replayable(self, changes):
        cli = _make_cli()
        value = {
            "version": 1,
            "kind": "accepted_user_input",
            "status": "pending",
            "safe_to_replay": True,
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "provider_call_sent": False,
            "text": "do not guess",
            "surface_key": "cli:session-a",
            "admission_id": "recovery-a",
            **changes,
        }
        assert not cli._chat_project_pending_input_recovery(value)
        assert len(cli._prompt_stash) == 0

    def test_uncertain_dispatch_warns_by_id_without_exposing_prompt(self):
        cli = _make_cli()
        cli._session_db = MagicMock()
        cli._pending_input_recoveries_restored = False
        value = {
            "status": "claimed",
            "dispatch_state": "started",
            "admission_id": "recovery-sensitive-a",
            "text": "raw prompt must stay hidden",
            "surface_key": "cli:session-a",
        }
        import cli as cli_mod

        with patch(
            "agent.pending_input_recovery.list_pending_input_recoveries",
            return_value=[value],
        ), patch.object(cli_mod, "_cprint") as printed:
            assert cli._restore_pending_input_recoveries_to_stash() == 0

        warning = printed.call_args.args[0]
        assert "recovery-sensitive-a" in warning
        assert "uncertain provider dispatch" in warning
        assert "raw prompt must stay hidden" not in warning
        assert len(cli._prompt_stash) == 0

    def test_more_than_max_recoveries_are_bounded_deduped_and_reported(self):
        from hermes_cli.prompt_stash import MAX_STASH_ITEMS

        cli = _make_cli()
        cli._session_db = MagicMock()
        cli._pending_input_recovery_backlog_ids = ()
        values = [
            {
                "version": 1,
                "kind": "accepted_user_input",
                "status": "pending",
                "safe_to_replay": True,
                "input_kind": "plain_text",
                "exact": True,
                "lossy": False,
                "provider_call_sent": False,
                "text": "same raw prompt must not appear in backlog warning",
                "surface_key": "cli:session-a",
                "admission_id": f"recovery-{index:02d}",
            }
            for index in range(MAX_STASH_ITEMS + 1)
        ]
        import cli as cli_mod

        with patch(
            "agent.pending_input_recovery.list_pending_input_recoveries",
            return_value=values,
        ), patch.object(cli_mod, "_cprint") as printed:
            assert cli._restore_pending_input_recoveries_to_stash() == MAX_STASH_ITEMS
            assert cli._restore_pending_input_recoveries_to_stash() == 0

        ids = [entry.recovery_admission_id for entry in cli._prompt_stash.items]
        assert len(ids) == MAX_STASH_ITEMS
        assert len(set(ids)) == MAX_STASH_ITEMS
        assert cli._pending_input_recovery_backlog_ids == (f"recovery-{MAX_STASH_ITEMS:02d}",)
        warning = "\n".join(call.args[0] for call in printed.call_args_list)
        assert "1 additional pending input remains durable" in warning
        assert f"recovery-{MAX_STASH_ITEMS:02d}" in warning
        assert "same raw prompt must not appear" not in warning

    def test_recovery_backlog_refills_as_composer_slot_opens(self):
        from hermes_cli.prompt_stash import MAX_STASH_ITEMS

        cli = _make_cli()
        cli._session_db = MagicMock()
        values = [
            {
                "version": 1,
                "kind": "accepted_user_input",
                "status": "pending",
                "safe_to_replay": True,
                "input_kind": "plain_text",
                "exact": True,
                "lossy": False,
                "provider_call_sent": False,
                "text": "identical",
                "surface_key": "cli:session-a",
                "admission_id": f"recovery-{index:02d}",
            }
            for index in range(MAX_STASH_ITEMS + 2)
        ]
        import cli as cli_mod

        with patch(
            "agent.pending_input_recovery.list_pending_input_recoveries",
            return_value=values,
        ), patch.object(cli_mod, "_cprint"):
            assert cli._restore_pending_input_recoveries_to_stash() == MAX_STASH_ITEMS
            restored_entry = cli._prompt_stash.peek()
            cli._prompt_stash.pop()
            cli._composer_recovery_entry = restored_entry
            assert cli._restore_pending_input_recoveries_to_stash() == 1

        ids = {entry.recovery_admission_id for entry in cli._prompt_stash.items}
        assert len(ids) == MAX_STASH_ITEMS
        assert restored_entry.recovery_admission_id not in ids
        assert restored_entry.recovery_admission_id not in cli._pending_input_recovery_backlog_ids
        assert cli._pending_input_recovery_backlog_ids == (f"recovery-{MAX_STASH_ITEMS + 1:02d}",)

    def test_delete_pages_to_next_recovery_without_resolving_deleted_id(self):
        from hermes_cli.prompt_stash import MAX_STASH_ITEMS

        cli = _make_cli()
        cli._session_db = MagicMock()
        values = [
            {
                "version": 1,
                "kind": "accepted_user_input",
                "status": "pending",
                "safe_to_replay": True,
                "input_kind": "plain_text",
                "exact": True,
                "lossy": False,
                "provider_call_sent": False,
                "text": "identical",
                "surface_key": "cli:session-a",
                "admission_id": f"recovery-{index:02d}",
            }
            for index in range(MAX_STASH_ITEMS + 1)
        ]
        import cli as cli_mod

        with patch(
            "agent.pending_input_recovery.list_pending_input_recoveries",
            return_value=values,
        ), patch.object(cli_mod, "_cprint"):
            cli._restore_pending_input_recoveries_to_stash()
            cli._prompt_stash.open_panel()
            deleted_id = cli._prompt_stash.peek().recovery_admission_id
            event = MagicMock()
            cli._tui_handle_stash_panel_delete(event)

        ids = {entry.recovery_admission_id for entry in cli._prompt_stash.items}
        assert len(ids) == MAX_STASH_ITEMS
        assert deleted_id not in ids
        assert f"recovery-{MAX_STASH_ITEMS:02d}" in ids
        assert cli._pending_input_recovery_backlog_ids == (deleted_id,)
        assert deleted_id in cli._suppressed_pending_input_recovery_ids
        event.app.invalidate.assert_called_once_with()


class TestKeybindingRegistration:
    """Behavioral coverage for the stash keybinding surface.

    NOTE: the Ctrl+S / panel-navigation handlers are registered inside
    ``HermesCLI.run()``'s local ``KeyBindings`` instance, so they are not
    importable without launching the TUI. Asserting on cli.py's SOURCE TEXT
    to prove they exist is the banned change-detector antipattern (root
    AGENTS.md, "Never read source code in tests"): it passes when the
    handler exists but is wired wrong, and fails on a correct rename.

    The regression that broke PR #4771 (a rebase silently dropping the
    keybinding) is instead guarded where the behavior actually lives — the
    stash state machine below and in test_prompt_stash.py, which every
    handler delegates to. Extracting run()'s bindings into a standalone
    registrar would make direct handler tests possible; that refactor is
    deliberately out of scope for this salvage.
    """

    def test_extension_hook_still_a_noop(self, cli):
        """The stash binding lives in run(), not in the wrapper extension hook."""
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()
        assert cli._register_extra_tui_keybindings(kb, input_area=None) is None
        assert kb.bindings == []


class TestStartupProjectionOrder:
    def test_recovery_projection_precedes_layout_and_first_render(self):
        cli = _make_cli()
        order = []
        cli._claim_active_session = MagicMock(return_value=True)
        cli._tui_print_startup = MagicMock()

        def init_run_state():
            order.append("run-state")
            assert cli._session_db is not None

        def restore():
            order.append("recovery")
            assert cli._session_db is not None
            return 0

        cli._tui_init_run_state = init_run_state
        cli._restore_pending_input_recoveries_to_stash = restore
        cli._tui_build_key_bindings = MagicMock(
            side_effect=lambda: order.append("keybindings") or MagicMock()
        )
        cli._tui_build_layout = MagicMock(
            side_effect=lambda _kb: order.append("layout") or (MagicMock(), MagicMock())
        )
        cli._tui_build_application = MagicMock(return_value=MagicMock())
        cli._install_resize_recovery = MagicMock()
        cli._tui_install_signal_handlers = MagicMock()
        cli._tui_stdin_usable = MagicMock(return_value=False)
        cli._print_exit_summary = MagicMock()

        import cli as cli_mod

        with patch.object(cli_mod.threading, "Thread", return_value=MagicMock()), patch.object(
            cli_mod.atexit, "register"
        ), patch.object(cli_mod, "_run_cleanup"):
            cli.run()

        assert order == ["run-state", "recovery", "keybindings", "layout"]


class TestLayoutSlot:
    def test_layout_includes_stash_panel_when_present(self, cli):
        cli._stash_panel_widget = "stash-panel"
        try:
            children = cli._build_tui_layout_children(
                sudo_widget="sudo",
                secret_widget="secret",
                approval_widget="approval",
                clarify_widget="clarify",
                spinner_widget="spinner",
                spacer="spacer",
                status_bar="status",
                input_rule_top="top-rule",
                image_bar="image-bar",
                input_area="input-area",
                input_rule_bot="bottom-rule",
                voice_status_bar="voice-status",
                completions_menu="completions-menu",
            )
            assert "stash-panel" in children
            # Panel sits directly above the status bar.
            assert children.index("stash-panel") < children.index("status")
        finally:
            cli._stash_panel_widget = None

    def test_layout_omits_stash_panel_when_absent(self, cli):
        cli._stash_panel_widget = None
        children = cli._build_tui_layout_children(
            sudo_widget="sudo",
            secret_widget="secret",
            approval_widget="approval",
            clarify_widget="clarify",
            spinner_widget="spinner",
            spacer="spacer",
            status_bar="status",
            input_rule_top="top-rule",
            image_bar="image-bar",
            input_area="input-area",
            input_rule_bot="bottom-rule",
            voice_status_bar="voice-status",
            completions_menu="completions-menu",
        )
        assert None not in children


class TestRenderStashPanel:
    """Contributor's panel renderer, now measured in display cells."""

    @staticmethod
    def _rows(cli, count=3, width=100):
        stash = type(cli._prompt_stash)()
        for i in range(count):
            stash.stash(f"draft number {i}")
        return cli._render_stash_panel(stash.panel_rows(), 0, width)

    def test_returns_fragments(self, cli):
        frags = self._rows(cli)
        assert frags
        assert all(isinstance(f, tuple) and len(f) == 2 for f in frags)

    def test_header_and_footer_present(self, cli):
        text = "".join(t for _, t in self._rows(cli))
        assert "📌 Stash" in text
        assert "Ctrl+S" in text
        assert "Enter=restore" in text
        assert "D=delete" in text

    def test_row_per_entry(self, cli):
        text = "".join(t for _, t in self._rows(cli, count=3))
        for i in range(3):
            assert f"[{i + 1}]" in text

    def test_singular_plural_item_label(self, cli):
        one = "".join(t for _, t in self._rows(cli, count=1))
        assert "(1 item)" in one
        two = "".join(t for _, t in self._rows(cli, count=2))
        assert "(2 items)" in two

    @pytest.mark.parametrize("width", [16, 20, 30, 40, 60, 80, 120, 400])
    def test_no_line_exceeds_terminal_width(self, cli, width):
        """Rows must never bleed past the terminal — the bug the PR's three
        follow-up commits kept failing to fix by tweaking len()."""
        from prompt_toolkit.utils import get_cwidth

        text = "".join(t for _, t in self._rows(cli, count=3, width=width))
        for line in text.split("\n"):
            if line:
                assert get_cwidth(line) <= max(width, 12), (
                    f"line {line!r} is {get_cwidth(line)} cells, width={width}"
                )

    def test_multiline_draft_renders_as_one_row(self, cli):
        stash = type(cli._prompt_stash)()
        stash.stash("first line\nsecond line\nthird line")
        frags = cli._render_stash_panel(stash.panel_rows(), 0, 100)
        text = "".join(t for _, t in frags)
        # header + 1 entry row + footer = 3 rendered lines
        assert len([ln for ln in text.split("\n") if ln]) == 3

    def test_wide_glyph_preview_does_not_overflow(self, cli):
        """CJK previews are 2 cells per char — must still fit the box."""
        from prompt_toolkit.utils import get_cwidth

        stash = type(cli._prompt_stash)()
        stash.stash("中文" * 80)
        frags = cli._render_stash_panel(stash.panel_rows(), 0, 60)
        for line in "".join(t for _, t in frags).split("\n"):
            if line:
                assert get_cwidth(line) <= 60

    def test_cursor_row_is_styled_differently(self, cli):
        stash = type(cli._prompt_stash)()
        stash.stash("a")
        stash.stash("b")
        styles = {s for s, _ in cli._render_stash_panel(stash.panel_rows(), 1, 100)}
        assert "class:subagent-selected" in styles

    def test_empty_list_still_renders_frame(self, cli):
        frags = cli._render_stash_panel([], 0, 80)
        text = "".join(t for _, t in frags)
        assert "(0 items)" in text


class TestStatusBarIndicator:
    def test_no_indicator_when_stash_empty(self, cli):
        cli._prompt_stash.clear()
        cli._status_bar_visible = True
        text = "".join(t for _, t in cli._get_status_bar_fragments())
        assert "📌" not in text

    def test_indicator_appears_after_stashing(self, cli):
        cli._prompt_stash.clear()
        cli._status_bar_visible = True
        cli._prompt_stash.stash("a parked draft")
        try:
            text = "".join(t for _, t in cli._get_status_bar_fragments())
            assert "📌 1" in text
        finally:
            cli._prompt_stash.clear()

    def test_indicator_count_tracks_stash_size(self, cli):
        cli._prompt_stash.clear()
        cli._status_bar_visible = True
        cli._prompt_stash.stash("a")
        cli._prompt_stash.stash("b")
        try:
            text = "".join(t for _, t in cli._get_status_bar_fragments())
            assert "📌 2" in text
        finally:
            cli._prompt_stash.clear()

    def test_indicator_clears_after_restore(self, cli):
        cli._prompt_stash.clear()
        cli._status_bar_visible = True
        cli._prompt_stash.stash("a")
        cli._prompt_stash.pop()
        text = "".join(t for _, t in cli._get_status_bar_fragments())
        assert "📌" not in text


class TestFmtStashAge:
    def test_age_buckets(self, cli):
        import time

        now = time.monotonic()
        assert cli._fmt_stash_age(now) == "just now"
        assert cli._fmt_stash_age(now - 30).endswith("s ago")
        assert "min ago" in cli._fmt_stash_age(now - 300)
        assert cli._fmt_stash_age(now - 7200).endswith("h ago")
