"""Tests for opt-in profile-scoped Slack slash-command aliases (platforms.slack.extra.slash_aliases).

Covers:
(a) Alias matching by command matcher and translation with args preserved byte-for-byte;
(b) Unknown target refused with a log line and not registered;
(c) Reserved alias name ('status', 'archive') or colliding native name ('busy') refused with log line;
(d) Alias produces identical MessageEvent (text, message_type, source) as target slash;
(e) With no aliases, matcher pattern, _slash_command_text output, and manifest are identical to base;
(f) Manifest with aliases includes exactly the valid alias entries.
"""

import logging
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from hermes_cli.commands_platforms import (
    _SLACK_RESERVED_COMMANDS,
    slack_app_manifest,
    slack_native_slashes,
    validate_slack_slash_aliases,
)
from plugins.platforms.slack.adapter import SlackAdapter


def _build_adapter(extra=None):
    """Helper to construct a mock-backed SlackAdapter with custom extra config."""
    config = PlatformConfig(enabled=True, token="xoxb-fake", extra=extra)
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_BOT"
    adapter._running = True
    adapter.handle_message = AsyncMock()
    return adapter


class TestSlackSlashAliasesMatchingAndTranslation:
    """Requirement (a): alias is matched by the command matcher and translated with args
    preserved byte-for-byte, including internal/trailing spaces and empty args."""

    @pytest.mark.asyncio
    async def test_matcher_registers_and_matches_valid_aliases(self):
        adapter = _build_adapter(extra={"slash_aliases": {"cos-busy": "busy", "cos-status": "status"}})
        adapter._register_bolt_handlers()

        # Command matcher regex must match native commands and registered aliases
        pattern = adapter._slash_pattern
        assert pattern.match("/cos-busy") is not None
        assert pattern.match("/cos-status") is not None
        assert pattern.match("/busy") is not None
        assert pattern.match("/unregistered-alias") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw_text", "expected_text"),
        [
            ("arg1   arg2  ", "/busy arg1   arg2  "),
            ("   leading and trailing   ", "/busy    leading and trailing   "),
            ("internal\t\ttabs   spaces", "/busy internal\t\ttabs   spaces"),
            ("", "/busy"),
        ],
    )
    async def test_translation_preserves_args_byte_for_byte(self, raw_text, expected_text):
        adapter = _build_adapter(extra={"slash_aliases": {"cos-busy": "busy"}})
        command = {
            "command": "/cos-busy",
            "text": raw_text,
            "user_id": "U123",
            "channel_id": "C123",
            "team_id": "T123",
        }

        await adapter._handle_slash_command(command)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == expected_text
        assert event.message_type == MessageType.COMMAND

    @pytest.mark.asyncio
    async def test_translation_handles_missing_text_key(self):
        adapter = _build_adapter(extra={"slash_aliases": {"cos-status": "status"}})
        command = {
            "command": "/cos-status",
            "user_id": "U123",
            "channel_id": "C123",
            "team_id": "T123",
        }

        await adapter._handle_slash_command(command)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "/status"
        assert event.message_type == MessageType.COMMAND

    @pytest.mark.asyncio
    async def test_ephemeral_ack_shows_alias_name(self):
        adapter = _build_adapter(extra={"slash_aliases": {"cos-busy": "busy"}})
        adapter._register_bolt_handlers()

        # Retrieve the registered handler passed to _app.command
        adapter._app.command.assert_called_once()
        handler = adapter._app.command.call_args.args[0]
        # In Bolt, _app.command returns a decorator; find the registered function
        # Or execute the wrapped handle_hermes_command directly
        ack = AsyncMock()
        command = {"command": "/cos-busy", "text": "queue", "user_id": "U1", "channel_id": "C1"}

        # Call _handle_slash_command via the decorator registered on mock _app
        decorator = adapter._app.command.call_args[0]
        # Test handle_hermes_command closure
        registered_callbacks = [call.args[0] for call in adapter._app.command.return_value.call_args_list]
        assert len(registered_callbacks) == 1
        handle_hermes_cmd = registered_callbacks[0]

        await handle_hermes_cmd(ack, command)
        ack.assert_awaited_once_with(response_type="ephemeral", text="Running `/cos-busy`…")
        adapter.handle_message.assert_awaited_once()
        assert adapter.handle_message.await_args.args[0].text == "/busy queue"


class TestSlackSlashAliasesUnknownTargetRefusal:
    """Requirement (b): an unknown target is refused with a log line and not registered."""

    def test_unknown_target_refused_with_log_line(self, caplog):
        with caplog.at_level(logging.WARNING, logger="plugins.platforms.slack.adapter"):
            adapter = _build_adapter(
                extra={"slash_aliases": {"valid-cmd": "busy", "bogus-cmd": "nonexistent_target_12345"}}
            )

        assert "valid-cmd" in adapter._slash_aliases
        assert "bogus-cmd" not in adapter._slash_aliases
        assert adapter._slash_aliases == {"valid-cmd": "busy"}

        # Must log ONE clear refusal line
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "bogus-cmd" in r.getMessage() and "nonexistent_target_12345" in r.getMessage()
        ]
        assert len(warning_records) == 1
        assert "is not an existing gateway command" in warning_records[0].getMessage()


class TestSlackSlashAliasesReservedAndCollidingRefusal:
    """Requirement (c): a reserved alias name such as 'status' or 'archive', or one colliding
    with a native slash like 'busy', is refused."""

    def test_reserved_names_and_collisions_refused_with_log_lines(self, caplog):
        with caplog.at_level(logging.WARNING, logger="plugins.platforms.slack.adapter"):
            adapter = _build_adapter(
                extra={
                    "slash_aliases": {
                        "status": "busy",          # reserved by Slack
                        "archive": "stop",         # reserved by Slack
                        "busy": "stop",            # collides with existing native slash
                        "cos-stop": "stop",        # valid
                    }
                }
            )

        assert "status" not in adapter._slash_aliases
        assert "archive" not in adapter._slash_aliases
        assert "busy" not in adapter._slash_aliases
        assert adapter._slash_aliases == {"cos-stop": "stop"}

        # Check refusal log lines
        status_warnings = [
            r for r in caplog.records
            if "status" in r.getMessage() and "Slack-reserved command" in r.getMessage()
        ]
        assert len(status_warnings) == 1

        archive_warnings = [
            r for r in caplog.records
            if "archive" in r.getMessage() and "Slack-reserved command" in r.getMessage()
        ]
        assert len(archive_warnings) == 1

        busy_warnings = [
            r for r in caplog.records
            if "busy" in r.getMessage() and "collides with an existing native slash command" in r.getMessage()
        ]
        assert len(busy_warnings) == 1

    def test_duplicate_aliases_refused(self, caplog):
        with caplog.at_level(logging.WARNING, logger="plugins.platforms.slack.adapter"):
            # Same alias configured twice with different targets (e.g. through casing/slashes)
            adapter = _build_adapter(
                extra={"slash_aliases": {"cos-busy": "busy", "/COS-BUSY": "stop"}}
            )

        assert adapter._slash_aliases == {"cos-busy": "busy"}
        dup_warnings = [
            r for r in caplog.records
            if "already registered as an alias" in r.getMessage()
        ]
        assert len(dup_warnings) == 1


class TestSlackSlashAliasesAuthorizationParity:
    """Requirement (d): the alias produces the identical MessageEvent (text, message_type, source)
    as the plain target slash, i.e. same authorization path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_type", ["dm", "group"])
    async def test_alias_produces_identical_message_event_to_target(self, chat_type):
        channel_id = "D12345" if chat_type == "dm" else "C12345"
        raw_cmd_native = {
            "command": "/busy",
            "text": "queue",
            "user_id": "UUSER",
            "channel_id": channel_id,
            "team_id": "TTEAM",
            "thread_ts": "1234.5678",
        }
        raw_cmd_alias = {
            "command": "/cos-busy",
            "text": "queue",
            "user_id": "UUSER",
            "channel_id": channel_id,
            "team_id": "TTEAM",
            "thread_ts": "1234.5678",
        }

        # 1. Native slash
        adapter_native = _build_adapter(extra={})
        await adapter_native._handle_slash_command(raw_cmd_native)
        event_native: MessageEvent = adapter_native.handle_message.await_args.args[0]

        # 2. Alias slash
        adapter_alias = _build_adapter(extra={"slash_aliases": {"cos-busy": "busy"}})
        await adapter_alias._handle_slash_command(raw_cmd_alias)
        event_alias: MessageEvent = adapter_alias.handle_message.await_args.args[0]

        # Assert identical MessageEvent attributes
        assert event_alias.text == event_native.text == "/busy queue"
        assert event_alias.message_type == event_native.message_type == MessageType.COMMAND
        assert event_alias.source.chat_id == event_native.source.chat_id == channel_id
        assert event_alias.source.chat_type == event_native.source.chat_type == chat_type
        assert event_alias.source.user_id == event_native.source.user_id == "UUSER"
        assert event_alias.source.scope_id == event_native.source.scope_id == "TTEAM"
        assert event_alias.source.thread_id == event_native.source.thread_id == "1234.5678"


class TestSlackSlashAliasesZeroChangeWithoutAliases:
    """Requirement (e): with no aliases, the matcher pattern, _slash_command_text output and
    the manifest are identical to before (compare against base behavior)."""

    def test_matcher_pattern_identical_without_aliases(self):
        adapter = _build_adapter(extra={})
        adapter._register_bolt_handlers()

        native_names = [name for name, _d, _h in slack_native_slashes()]
        expected_pattern = r"^/(?:" + "|".join(re.escape(n) for n in native_names) + r")$"
        assert adapter._slash_pattern.pattern == expected_pattern

    def test_slash_command_text_identical_without_aliases(self):
        command = {"command": "/status", "text": "verbose"}
        assert SlackAdapter._slash_command_text(command) == "/status verbose"

        command_empty = {"command": "/status", "text": ""}
        assert SlackAdapter._slash_command_text(command_empty) == "/status"

        command_hermes = {"command": "/hermes", "text": "compact"}
        assert SlackAdapter._slash_command_text(command_hermes) == "/compress"

    def test_manifest_identical_without_aliases(self):
        native_slashes = slack_native_slashes()
        expected_manifest = {
            "features": {
                "slash_commands": [
                    {
                        "command": f"/{name}",
                        "description": desc or f"Run /{name}",
                        "should_escape": False,
                        "url": "https://hermes-agent.local/slack/commands",
                        **({"usage_hint": usage} if usage else {}),
                    }
                    for name, desc, usage in native_slashes
                ]
            }
        }
        manifest_default = slack_app_manifest()
        manifest_explicit_none = slack_app_manifest(slash_aliases=None)
        manifest_empty_dict = slack_app_manifest(slash_aliases={})

        assert manifest_default == expected_manifest
        assert manifest_explicit_none == expected_manifest
        assert manifest_empty_dict == expected_manifest


class TestSlackSlashAliasesManifestGeneration:
    """Requirement (f): the manifest with aliases includes exactly the valid alias entries."""

    def test_manifest_includes_valid_aliases_with_descriptions_and_hints(self, monkeypatch):
        # Provide available slots under the 50-command cap so all 5 aliases can be included
        monkeypatch.setattr(
            "hermes_cli.commands_platforms.slack_native_slashes",
            lambda: [("hermes", "Talk to Hermes", "[args]")],
        )
        aliases = {
            "cos-busy": "busy",
            "cos-stop": "stop",
            "cos-queue": "queue",
            "cos-steer": "steer",
            "cos-status": "status",
            "invalid-one": "nonexistent_target",
        }
        manifest = slack_app_manifest(slash_aliases=aliases)
        slashes = manifest["features"]["slash_commands"]
        slash_by_cmd = {entry["command"]: entry for entry in slashes}

        # Invalid target must NOT be in manifest
        assert "/invalid-one" not in slash_by_cmd

        # All valid aliases must be present
        for alias in ("cos-busy", "cos-stop", "cos-queue", "cos-steer", "cos-status"):
            assert f"/{alias}" in slash_by_cmd

        # Verify entry details for cos-busy
        busy_entry = slash_by_cmd["/cos-busy"]
        assert busy_entry["description"] == "Alias for /busy — Control how messages behave while Hermes is working"
        assert busy_entry["usage_hint"] == "[queue|steer|interrupt|status]"
        assert busy_entry["should_escape"] is False
        assert busy_entry["url"] == "https://hermes-agent.local/slack/commands"

        # Verify entry details for cos-status (status has no usage hint)
        status_entry = slash_by_cmd["/cos-status"]
        assert status_entry["description"] == "Alias for /status — Show session, model, token, and context info"
        assert "usage_hint" not in status_entry
        assert status_entry["should_escape"] is False

        # Verify entry details for cos-queue
        queue_entry = slash_by_cmd["/cos-queue"]
        assert queue_entry["description"] == "Alias for /queue — Queue a prompt for the next turn, or list/edit/rm/move/clear queued prompts"
        assert queue_entry["usage_hint"] == "[<prompt>|list|edit N <prompt>|rm N|move A B|clear]"

    def test_manifest_reads_from_raw_config(self, monkeypatch):
        # Provide available slots under the 50-command cap
        monkeypatch.setattr(
            "hermes_cli.commands_platforms.slack_native_slashes",
            lambda: [("hermes", "Talk to Hermes", "[args]")],
        )
        # When slash_aliases is None, read from profile config
        mock_raw_config = {
            "platforms": {
                "slack": {
                    "extra": {
                        "slash_aliases": {
                            "cos-busy": "busy",
                            "cos-status": "status",
                        }
                    }
                }
            }
        }
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: mock_raw_config)

        manifest = slack_app_manifest()
        slashes = manifest["features"]["slash_commands"]
        slash_by_cmd = {entry["command"]: entry for entry in slashes}

        assert "/cos-busy" in slash_by_cmd
        assert "/cos-status" in slash_by_cmd

    def test_manifest_respects_50_command_cap_on_real_registry(self, caplog):
        """M1 requirement: card's 5-alias config on real registry yields at most 50 commands,
        and the overflow is warned and deterministic (native commands never dropped)."""
        card_aliases = {
            "cos-busy": "busy",
            "cos-stop": "stop",
            "cos-queue": "queue",
            "cos-steer": "steer",
            "cos-status": "status",
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.commands"):
            manifest = slack_app_manifest(slash_aliases=card_aliases)

        slashes = manifest["features"]["slash_commands"]
        assert len(slashes) <= 50
        assert len(slashes) == 50

        # Native commands are never dropped
        native_slashes = slack_native_slashes()
        native_cmds = {f"/{name}" for name, _d, _h in native_slashes}
        manifest_cmds = {entry["command"] for entry in slashes}
        assert native_cmds.issubset(manifest_cmds)

        # Overflow warnings logged for all 5 aliases
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        for alias in sorted(card_aliases.keys()):
            assert any(f"Refusing slash alias '{alias}' in manifest: manifest command cap (50) reached" in w for w in warnings)

    def test_manifest_deterministic_trimming_with_partial_slots(self, monkeypatch, caplog):
        """M1 requirement: when partial slots remain under the 50-command cap, excess aliases
        are trimmed deterministically (alphabetical order) with warnings logged."""
        dummy_natives = [(f"native-{i:02d}", f"desc-{i}", "") for i in range(48)]
        monkeypatch.setattr("hermes_cli.commands_platforms.slack_native_slashes", lambda: dummy_natives)

        aliases = {
            "cos-e": "busy",
            "cos-b": "busy",
            "cos-a": "busy",
            "cos-d": "busy",
            "cos-c": "busy",
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.commands"):
            manifest = slack_app_manifest(slash_aliases=aliases)

        slashes = manifest["features"]["slash_commands"]
        assert len(slashes) == 50
        cmd_names = [entry["command"] for entry in slashes]

        # First 48 are the native commands
        assert cmd_names[:48] == [f"/{name}" for name, _d, _h in dummy_natives]
        # Next 2 slots are deterministically filled by sorted aliases: cos-a, cos-b
        assert cmd_names[48:] == ["/cos-a", "/cos-b"]

        # Excess aliases cos-c, cos-d, cos-e are warned
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("cos-c" in w and "manifest command cap (50) reached" in w for w in warnings)
        assert any("cos-d" in w and "manifest command cap (50) reached" in w for w in warnings)
        assert any("cos-e" in w and "manifest command cap (50) reached" in w for w in warnings)


class TestSlackSlashAliasesPluginAndWrapperTargetRefusal:
    """Requirement H1: Alias targets must be built-in gateway commands only.
    Refuse '/hermes' (the Slack wrapper) explicitly, and refuse ANY plugin-registered
    command name, with a logged warning and the alias dropped."""

    def test_plugin_and_wrapper_targets_refused(self, tmp_path, monkeypatch, caplog):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        # Isolated HOME / HERMES_HOME
        isolated_home = tmp_path / "home"
        isolated_hermes_home = tmp_path / "hermes_home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_hermes_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(isolated_home))
        monkeypatch.setenv("HERMES_HOME", str(isolated_hermes_home))

        # Real PluginManager registering 'hermes' and a custom non-wrapper plugin command
        manager = PluginManager()
        ctx = PluginContext(PluginManifest(name="test-plugin", source="user"), manager)

        async def dummy_handler(raw_args):
            return "dummy plugin executed"

        reg_hermes = ctx.register_command("hermes", dummy_handler)
        assert reg_hermes is not None
        reg_custom = ctx.register_command("custom-cmd", dummy_handler)
        assert reg_custom is not None
        assert "hermes" in manager._plugin_commands
        assert "custom-cmd" in manager._plugin_commands

        monkeypatch.setattr("hermes_cli.plugins._ensure_plugins_discovered", lambda: manager)

        alias_config = {
            "cos-hermes": "/hermes",
            "cos-custom": "/custom-cmd",
            "cos-busy": "busy",
        }

        with caplog.at_level(logging.WARNING, logger="plugins.platforms.slack.adapter"):
            adapter = _build_adapter(extra={"slash_aliases": alias_config})

        # 'cos-hermes' and 'cos-custom' MUST BE REFUSED
        assert "cos-hermes" not in adapter._slash_aliases
        assert "cos-custom" not in adapter._slash_aliases
        # Valid built-in target must be preserved
        assert "cos-busy" in adapter._slash_aliases
        assert adapter._slash_aliases == {"cos-busy": "busy"}

        # Warnings must be logged for both refusals
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("cos-hermes" in msg and "Slack wrapper command" in msg for msg in warnings)
        assert any("cos-custom" in msg and "plugin command" in msg for msg in warnings)


class TestSlackSlashAliasesMalformedConfig:
    """Requirement L1: Log a warning when the alias config is a malformed falsy value
    ([], '', 0, False) or a non-mapping, instead of silently ignoring it."""

    @pytest.mark.parametrize("falsy_val", [[], "", 0, False, [1, 2, 3], "invalid_string", 42])
    def test_malformed_alias_config_refused_with_warning(self, falsy_val, caplog):
        with caplog.at_level(logging.WARNING, logger="plugins.platforms.slack.adapter"):
            adapter = _build_adapter(extra={"slash_aliases": falsy_val})

        assert adapter._slash_aliases == {}
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Refusing platforms.slack.extra.slash_aliases: expected a mapping" in w for w in warnings)
