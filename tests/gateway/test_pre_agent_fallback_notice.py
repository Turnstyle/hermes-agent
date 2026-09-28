"""A fallback resolved during gateway credential resolution (before any AIAgent exists) must carry a
user-visible notice through the agent's one-shot fallback-notice mechanism (#74349).

Drives the production entry point — ``GatewayTurnMixin._resolve_session_agent_runtime`` bound to the
runner, then ``TurnRunner.run_sync`` — so the pop in run_turn.py and the attach in run_turn_runner.py
are both pinned (a helper-only test stays green with either removed)."""
import json
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gateway.run_turn import GatewayTurnMixin
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.session import Platform, SessionSource
from gateway.turn_context import TurnContext
from hermes_cli.auth import AuthError


class _RecordingAgent:
    built_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).built_kwargs = kwargs
        self.model = kwargs["model"]
        self.session_id = kwargs.get("session_id")
        self.tools = []
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200_000)
        self.session_prompt_tokens = self.session_completion_tokens = 0

    def run_conversation(self, _message, **_kwargs):
        return {"final_response": "ok", "messages": []}


def _runner_with_real_runtime_resolution():
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = runner._prefill_messages = None
    runner._pending_model_notes = runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._agent_config_signature.return_value = ("sig",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    for lane in ("_is_telegram_topic_lane", "_is_discord_auto_thread_lane", "_is_relay_discord_channel_lane"):
        getattr(runner, lane).return_value = False
    # Production resolution: no /model override, no channel override.
    runner._resolve_session_key_or_none.return_value = "test-session-key"
    runner._peek_session_state.return_value = None
    runner._sessions_map.return_value = {}
    runner._resolve_session_agent_runtime = types.MethodType(GatewayTurnMixin._resolve_session_agent_runtime, runner)
    # Pass the resolved runtime straight through so the kwargs handed to AIAgent are the resolved ones.
    # (Production pops ``request_overrides`` out of the runtime into the route; mirror that so the
    # resolved kwargs never carry it twice.)
    runner._resolve_turn_agent_config.side_effect = lambda _msg, model, rt: {
        "model": model, "runtime": {k: v for k, v in rt.items() if k != "request_overrides"}}
    return runner


def test_credential_resolution_fallback_reaches_agent_notice_not_agent_kwargs():
    from gateway.run_turn_runner import TurnRunner

    fb = {"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "k", "base_url": "u"}
    runner = _runner_with_real_runtime_resolution()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="hi", history=[], session_id="sid", session_key="test-session-key", user_config={},
        AIAgent=_RecordingAgent, resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    def primary_auth_fails(**kw):
        if kw.get("requested") is None:  # the primary, resolved from config.yaml
            raise AuthError("expired")
        return dict(fb)  # the fallback entry, walked by resolve_runtime_with_fallback

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=primary_auth_fails), \
         patch("hermes_cli.runtime_provider._get_model_config",
               return_value={"provider": "openai-codex", "default": "gpt-5.6-sol"}), \
         patch("gateway.run._load_gateway_config",
               return_value={"fallback_providers": [{"provider": "anthropic", "model": "claude-sonnet-5"}]}), \
         patch("gateway.run._resolve_gateway_model", return_value="gpt-5.6-sol"), \
         patch("gateway.run._get_channel_override", return_value=None):
        result = TurnRunner(runner, ctx).run_sync()

    assert result["final_response"] == "ok"
    agent = ctx.agent_holder[0]
    notice = agent._pending_fallback_notice
    assert "openai-codex/gpt-5.6-sol" in notice and "anthropic/claude-sonnet-5" in notice
    assert "_fallback_notice" not in _RecordingAgent.built_kwargs
    # Consumed by the turn: a later resolution without fallback must not re-attach a stale notice.
    assert runner._pre_agent_fallback_notice is None


def test_model_override_fast_path_clears_stale_notice():
    """The /model-override fast path returns before the pop; a notice stashed by an earlier resolution
    (hygiene, inbound, another session) must not survive to attach to this session's turn."""
    runner = _runner_with_real_runtime_resolution()
    runner._pre_agent_fallback_notice = "⚠️ Provider fallback: stale"
    override = {"model": "claude-sonnet-5", "provider": "anthropic", "api_key": "k", "base_url": "u"}
    runner._peek_session_state.return_value = SimpleNamespace(conversation=SimpleNamespace(model_override=override))
    with patch("gateway.run._resolve_gateway_model", return_value="gpt-5.6-sol"), \
         patch("gateway.run._credential_pool_for_provider", return_value=None):
        model, runtime = runner._resolve_session_agent_runtime(session_key="test-session-key")
    assert (model, runtime["provider"]) == ("claude-sonnet-5", "anthropic")
    assert runner._pre_agent_fallback_notice is None


def test_same_provider_pre_agent_fallback_does_not_pin_next_bot_chat_turn(monkeypatch):
    """The row records the model used, while a healthy next turn returns to the primary."""
    from gateway.run_turn_runner import TurnRunner

    row = {"model": "gpt-6-sol", "model_config": "{}"}

    class SessionDB:
        def get_session(self, _session_id):
            return row

        def update_session_meta(self, _session_id, model_config, *, model):
            row.update(model=model, model_config=model_config)

    runner = _runner_with_real_runtime_resolution()
    runner._session_db = SimpleNamespace(_db=SessionDB())
    runner._sync_session_model_from_agent = types.MethodType(GatewayTurnMixin._sync_session_model_from_agent, runner)
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="hi", history=[], session_id="sid", session_key="test-session-key", user_config={},
        AIAgent=_RecordingAgent, resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    fallback = {"provider": "openai-codex", "model": "gpt-6-luna", "api_key": "k", "base_url": "u"}

    def primary_auth_fails(**kw):
        if kw.get("requested") is None:
            raise AuthError("expired")
        return dict(fallback)

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=primary_auth_fails), \
         patch("hermes_cli.runtime_provider._get_model_config",
               return_value={"provider": "openai-codex", "default": "gpt-6-sol"}), \
         patch("gateway.run._load_gateway_config",
               return_value={"fallback_providers": [{"provider": "openai-codex", "model": "gpt-6-luna"}]}), \
         patch("gateway.run._resolve_gateway_model", return_value="gpt-6-sol"), \
         patch("gateway.run._get_channel_override", return_value=None):
        assert TurnRunner(runner, ctx).run_sync()["final_response"] == "ok"

    assert row["model"] == "gpt-6-luna"
    assert json.loads(row["model_config"])["gateway_runtime"]["fallback_active"] is True
    assert "_fallback_entry" not in _RecordingAgent.built_kwargs

    # Bot Chat's next API request reads the persisted row after primary auth recovers.
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr("run_agent.AIAgent", _RecordingAgent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "openai-codex", "api_key": "healthy", "base_url": "u"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "gpt-6-sol")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda model="": {}))
    monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda *_: None)
    assert adapter._stored_session_model(row) is None
    next_agent = adapter._create_agent(session_id="sid", session_model=adapter._stored_session_model(row))
    assert next_agent.model == "gpt-6-sol"
