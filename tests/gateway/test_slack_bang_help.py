"""Slack !help concise controls + bang rewrite + profile-isolated reactions.

SOURCE lane t_64f38cfe — few focused serial tests; no full suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands_slack_help import build_slack_help_text, format_reaction_status
from plugins.platforms.slack.adapter import SlackAdapter, _rewrite_known_bang_command


def _event(text: str, platform: Platform, *, profile: str | None = None) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="C1",
            user_id="U1",
            user_name="tester",
            chat_type="dm",
            profile=profile,
        ),
    )


def _runner(*, intake_adapter=None, config=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = config
    if intake_adapter is not None:
        runner._intake_adapter_for = lambda _source: intake_adapter
    return runner


def _slack_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="xoxb-fake", extra=extra or {})
    adapter = SlackAdapter(config)
    adapter._slash_aliases = adapter._resolve_slash_aliases()
    return adapter


class TestBangRewriteNormalizesCommandToken:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("!help", "/help"),
            ("!Help", "/Help"),
            ("! Help", "/Help"),
            ("!   HELP", "/HELP"),
            ("!busy queue", "/busy queue"),
            ("!steer  keep  spaces", "/steer  keep  spaces"),
            ("!nice work", "!nice work"),
            ("!commands", "/commands"),
        ],
    )
    def test_rewrite_cases(self, raw, expected):
        assert _rewrite_known_bang_command(raw) == expected

    def test_leading_whitespace_stripped_before_rewrite(self):
        # Production path: command_probe_text = _rewrite_known_bang_command(original.lstrip())
        assert _rewrite_known_bang_command("  !help".lstrip()) == "/help"

    def test_rewritten_help_variants_parse_as_help(self):
        for raw in ("!help", "!Help", "! Help", "!   HELP", "  !help"):
            rewritten = _rewrite_known_bang_command(raw.lstrip())
            event = MessageEvent(
                text=rewritten,
                source=SessionSource(
                    platform=Platform.SLACK, chat_id="C1", user_id="U1", chat_type="dm",
                ),
            )
            assert event.get_command() == "help", (raw, rewritten)


class TestSlackHelpBody:
    def test_disabled_reactions_and_proposed_not_active(self):
        text = build_slack_help_text(
            reaction_triggers=None,
            reaction_status_known=True,
            receiving_profile="shld-core-cos",
        )
        assert "not enabled" in text
        assert "Proposed, not active" in text
        assert "🔁 follow up" in text
        assert "📋 add to Slack Fleet List with a 30-character issue title" in text
        assert "message_agent" in text
        assert "canonical Bot Chat" in text
        assert "`!status`" in text
        assert "`!busy [queue|steer|interrupt|status]`" in text
        assert "`!steer <prompt>`" in text
        assert "None configured on this profile" in text
        # Must never claim the proposal is live.
        assert "proposal active" not in text.lower()
        assert "currently active reaction key" not in text.lower()

    def test_configured_reaction_fixture(self):
        text = build_slack_help_text(
            reaction_triggers={"thumbsup", "task"},
            reaction_status_known=True,
            configured_slash_aliases={"cos-busy": "busy"},
            receiving_profile="fixture-profile",
        )
        assert ":task:" in text and ":thumbsup:" in text
        assert "`/cos-busy` → `/busy`" in text
        assert "Proposed, not active" in text

    def test_authorization_filters_controls(self):
        text = build_slack_help_text(allowed_commands={"help", "status", "whoami"})
        assert "`!status`" in text
        assert "`!help`" in text
        assert "`!stop`" not in text
        assert "`!queue" not in text
        assert "`!steer" not in text

    def test_unknown_when_no_adapter_evidence(self):
        assert "unknown" in format_reaction_status(None, known=False)


@pytest.mark.asyncio
async def test_slack_help_uses_receiving_adapter_not_other_profile():
    adapter = _slack_adapter(extra={})
    assert adapter._slack_reaction_triggers() is None
    runner = _runner(intake_adapter=adapter)
    result = await runner._handle_help_command(
        _event("/help", Platform.SLACK, profile="shld-king")
    )
    assert "not enabled" in result
    assert "shld-king" in result
    assert "Proposed, not active" in result
    assert "`!status`" in result


@pytest.mark.asyncio
async def test_slack_help_configured_fixture_from_adapter():
    adapter = _slack_adapter(extra={"reaction_triggers": ["white_check_mark"], "slash_aliases": {}})
    runner = _runner(intake_adapter=adapter)
    result = await runner._handle_help_command(_event("!help", Platform.SLACK, profile="fixture"))
    assert ":white_check_mark:" in result
    assert "Proposed, not active" in result


@pytest.mark.asyncio
async def test_slack_help_ignores_foreign_secret_scope_reaction_triggers():
    """!help must not treat another profile's SLACK_REACTION_TRIGGERS as this adapter's config.

    Under shared-bot routing the active secret scope can belong to the routed profile while
    the intake adapter still belongs to the receiving bot. Live reaction routing may still
    read the scoped secret; !help must not.
    """
    from agent import secret_scope

    adapter = _slack_adapter(extra={})
    runner = _runner(intake_adapter=adapter)
    event = _event("!help", Platform.SLACK, profile="receiving")
    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        tok_a = secret_scope.set_secret_scope({"SOMETHING_ELSE": "x"}, profile_home="/tmp/scope-a")
        try:
            help_a = await runner._handle_help_command(event)
            live_a = adapter._slack_reaction_triggers()
        finally:
            secret_scope.reset_secret_scope(tok_a)

        tok_b = secret_scope.set_secret_scope(
            {"SLACK_REACTION_TRIGGERS": "thumbsup,task"},
            profile_home="/tmp/scope-b",
        )
        try:
            help_b = await runner._handle_help_command(event)
            live_b = adapter._slack_reaction_triggers()
        finally:
            secret_scope.reset_secret_scope(tok_b)
    finally:
        secret_scope.set_multiplex_active(previous)

    # Live reaction routing still sees the scoped secret (unchanged behaviour).
    assert live_a is None
    assert live_b == {"thumbsup", "task"}

    # !help must stay stable across scopes and must not claim reactions are enabled.
    assert help_a == help_b
    assert ":thumbsup:" not in help_a and ":task:" not in help_a
    assert "enabled for" not in help_a
    assert "unknown" in help_a or "not enabled" in help_a
    assert "Proposed, not active" in help_a

    # Own adapter config.extra still reports configured triggers.
    own = _slack_adapter(extra={"reaction_triggers": ["white_check_mark"]})
    own_runner = _runner(intake_adapter=own)
    own_help = await own_runner._handle_help_command(
        _event("!help", Platform.SLACK, profile="own-cfg")
    )
    assert ":white_check_mark:" in own_help
    assert "Proposed, not active" in own_help


@pytest.mark.asyncio
async def test_non_slack_help_unchanged_shared_catalog(monkeypatch):
    """Telegram /help still uses the shared catalog executor path."""
    called = {}

    def _fake_execute(command, **kwargs):
        called["command"] = command
        called["options"] = kwargs.get("options")
        return SimpleNamespace(text="SHARED_CATALOG_HELP")

    monkeypatch.setattr("gateway.slash_commands._execute", _fake_execute)
    runner = _runner()
    result = await runner._handle_help_command(_event("/help", Platform.TELEGRAM))
    assert result == "SHARED_CATALOG_HELP"
    assert called["command"] == "help"


@pytest.mark.asyncio
async def test_commands_still_full_catalog_on_slack(monkeypatch):
    """!commands / /commands must not be replaced by the concise Slack help."""
    called = {}

    def _fake_execute(command, **kwargs):
        called["command"] = command
        return SimpleNamespace(text="FULL_COMMANDS_PAGE")

    monkeypatch.setattr("gateway.slash_commands._execute", _fake_execute)
    runner = _runner()
    result = await runner._handle_commands_command(_event("/commands", Platform.SLACK))
    assert result == "FULL_COMMANDS_PAGE"
    assert called["command"] == "commands"


def test_import_path_is_worktree():
    import gateway.slash_commands_slack_help as mod
    import plugins.platforms.slack.adapter as ad

    root = "/Users/sheldon/.hermes/kanban/boards/fleet/workspaces/t_64f38cfe/source"
    assert mod.__file__.startswith(root)
    assert ad.__file__.startswith(root)
    assert "/Users/sheldon/.hermes/hermes-agent/" not in mod.__file__
    assert "/Users/sheldon/.hermes/hermes-agent/" not in ad.__file__
