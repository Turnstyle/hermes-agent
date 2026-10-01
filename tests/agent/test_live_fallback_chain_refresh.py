"""Tests for live fallback chain re-read from profile config (H20)."""

from unittest.mock import MagicMock, patch

import pytest

try:
    from agent.agent_runtime_helpers import apply_fallback_chain_to_agent
except ImportError:
    apply_fallback_chain_to_agent = None
from agent.error_classifier import FailoverReason
from hermes_cli.fallback_config import (
    DeclaredFallbackChain,
    PinnedFallbackChain,
    scoped_fallback_chain,
)
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(
    fallback_model=None,
    provider="custom",
    base_url="https://my-llm.example.com/v1",
    platform=None,
):
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.get_model_context_length", return_value=200_000),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="unit-test",
            base_url=base_url,
            provider=provider,
            model="primary-model",
            platform=platform,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_resolve(base_url="https://openrouter.ai/api/v1", api_key="unit-test"):
    mock_client = MagicMock()
    mock_client.api_key = api_key
    mock_client.base_url = base_url
    return mock_client


@pytest.fixture(autouse=True)
def _reset_scoped_chain_pin():
    """Other suites call scoped_fallback_chain() without building an agent; keep the pin flag from leaking."""
    from hermes_cli.fallback_config import _SCOPED_CHAIN_PINNED

    _SCOPED_CHAIN_PINNED.set(False)
    yield
    _SCOPED_CHAIN_PINNED.set(False)


class TestLiveFallbackChainRefresh:
    def test_live_config_edit_updates_fallback_chain_between_turns(self):
        """Editing fallback_providers in config must update running agent's chain on next turn."""
        agent = _make_agent(
            fallback_model=[{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}],
        )
        agent._unavailable_fallback_keys = {"stale_key"}

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "anthropic/claude-opus-4"},
                {"provider": "openai-codex", "model": "gpt-6-sol"},
            ]
        }

        assert apply_fallback_chain_to_agent is not None, "apply_fallback_chain_to_agent is not implemented"
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        assert len(agent._fallback_chain) == 2
        assert agent._fallback_chain[0]["model"] == "anthropic/claude-opus-4"
        assert agent._fallback_chain[1]["model"] == "gpt-6-sol"
        # Stale unavailable memo must be cleared on real change
        assert len(agent._unavailable_fallback_keys) == 0

    def test_removing_active_fallback_switches_off_at_once(self):
        """When currently active fallback provider is removed from config, agent switches off it."""
        agent = _make_agent(
            fallback_model=[
                {"provider": "openai-codex", "model": "gpt-6-sol"},
                {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            ],
        )
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)

        assert agent._fallback_activated is True
        assert agent.provider == "openai-codex"
        assert agent.model == "gpt-6-sol"

        # Config changes: openai-codex is removed, only openrouter remains
        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
            ]
        }

        assert apply_fallback_chain_to_agent is not None, "apply_fallback_chain_to_agent is not implemented"
        with (
            patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config),
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)),
            patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
        ):
            apply_fallback_chain_to_agent(agent)

        # Agent must not stay on the removed openai-codex fallback
        assert agent.provider != "openai-codex"

    def test_pinned_chain_not_overridden_by_global_config(self):
        """Delegated children and cron jobs that pin their own route must keep scoped fallback chain."""
        pinned_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=[{"provider": "anthropic", "model": "pinned-model"}],
            pinned=True,
            owner="delegation",
        )
        agent = _make_agent(fallback_model=pinned_chain)
        assert getattr(agent, "_fallback_chain_pinned", False) is True

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "new-global-model"},
            ]
        }

        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        # Pinned chain must NOT be overwritten by the global profile config
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["model"] == "pinned-model"

    def test_pinned_with_none_not_overridden_by_global_config(self):
        """A pinned cron job with NO declared chain (scoped_fallback_chain returns None) must NOT receive global chain."""
        pinned_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=None,
            pinned=True,
            owner="cron job",
        )
        assert pinned_chain is None  # Preserves contract in tests/cron/test_cron_pinned_job_fallback.py

        agent = _make_agent(fallback_model=pinned_chain, platform="cron", provider="anthropic")
        assert getattr(agent, "_fallback_chain_pinned", False) is True
        assert agent._fallback_chain == []

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "new-global-model"},
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        # Pinned agent must NOT receive the global fallback chain
        assert agent._fallback_chain == []

    def test_pinned_with_empty_list_not_overridden_by_global_config(self):
        """A pinned route with an explicit empty chain ([]) must NOT receive the global chain."""
        pinned_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=[],
            pinned=True,
            owner="delegation",
        )
        assert pinned_chain is None

        agent = _make_agent(fallback_model=pinned_chain)
        assert getattr(agent, "_fallback_chain_pinned", False) is True
        assert agent._fallback_chain == []

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "new-global-model"},
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        assert agent._fallback_chain == []

    @pytest.mark.parametrize("pinned", [True, False])
    def test_agent_built_after_scoped_empty_chain_not_given_global_chain_on_first_turn(self, pinned):
        """An agent built after scoped_fallback_chain(declared=[]) (pinned and unpinned) is NOT given the global fallback chain on its first turn."""
        scoped_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=[],
            pinned=pinned,
            owner="delegation",
        )
        assert scoped_chain is None

        agent = _make_agent(fallback_model=scoped_chain)
        assert getattr(agent, "_fallback_chain_pinned", False) is True
        assert agent._fallback_chain == []

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "new-global-model"},
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        assert agent._fallback_chain == []

    def test_unpinned_child_with_declared_chain_keeps_declared_chain(self):
        """An UNpinned child that declared its own chain must keep it too."""
        declared_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=[{"provider": "groq", "model": "llama-3.3-70b"}],
            pinned=False,
            owner="delegation",
        )
        assert isinstance(declared_chain, DeclaredFallbackChain)
        assert getattr(declared_chain, "_pinned", False) is True

        agent = _make_agent(fallback_model=declared_chain)
        assert getattr(agent, "_fallback_chain_pinned", False) is True
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["provider"] == "groq"

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "new-global-model"},
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        # Declared chain must NOT be overwritten by the global profile config
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["provider"] == "groq"

    def test_unpinned_main_agent_global_chain_refreshes(self):
        """An unpinned main agent (CLI/gateway) continues to refresh when global config changes."""
        initial_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "initial-model"}],
            declared=None,
            pinned=False,
            owner="main",
        )
        assert not getattr(initial_chain, "_pinned", False)

        agent = _make_agent(fallback_model=initial_chain, platform="cli")
        assert getattr(agent, "_fallback_chain_pinned", False) is False
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["model"] == "initial-model"

        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "updated-global-model"},
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        # Unpinned main agent's fallback chain refreshes
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["model"] == "updated-global-model"

    def test_torn_config_keeps_last_known_good_chain(self):
        """A torn/failed config read must not wipe the chain or switch off active fallback (MEDIUM 5)."""
        initial_chain = [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]
        agent = _make_agent(fallback_model=initial_chain)
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)

        assert agent._fallback_activated is True
        assert agent._fallback_chain == initial_chain

        # Broken YAML / read exception during torn write causes load_user_config_effective to raise
        import yaml
        with patch("hermes_cli.config_effective.load_user_config_effective", side_effect=yaml.YAMLError("Torn write")):
            refreshed = apply_fallback_chain_to_agent(agent)

        assert refreshed is False
        # Chain must NOT be wiped
        assert agent._fallback_chain == initial_chain
        # Active fallback must NOT be switched off
        assert agent._fallback_activated is True

    def test_entry_metadata_edit_propagates_and_clears_unavailable_keys(self):
        """Whole-entry comparison ensures edits to api_key_env/api_mode propagate and clear unavailable keys (LOW L2)."""
        agent = _make_agent(
            fallback_model=[{
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4",
                "api_key_env": "OLD_KEY",
            }],
        )
        agent._unavailable_fallback_keys = {"stale_key"}

        new_config = {
            "fallback_providers": [
                {
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet-4",
                    "api_key_env": "NEW_KEY",
                }
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)

        # Whole-entry comparison detects the edit
        assert agent._fallback_chain[0]["api_key_env"] == "NEW_KEY"
        assert len(agent._unavailable_fallback_keys) == 0

    def test_build_turn_context_drives_fallback_chain_refresh(self):
        """Wiring verification: build_turn_context calls apply_fallback_chain_to_agent on turn start."""
        from agent.turn_context import build_turn_context

        agent = _make_agent(
            fallback_model=[{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}],
            platform="cli",
        )
        new_config = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "anthropic/claude-opus-4"},
            ]
        }
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            build_turn_context(
                agent,
                user_message="hi",
                system_message=None,
                conversation_history=[],
                task_id="t1",
                stream_callback=None,
                persist_user_message="hi",
                restore_or_build_system_prompt=MagicMock(return_value="sys prompt"),
                install_safe_stdio=MagicMock(),
                sanitize_surrogates=lambda s: s,
                summarize_user_message_for_log=MagicMock(),
                set_session_context=MagicMock(),
                set_current_write_origin=MagicMock(),
                ra=MagicMock(),
            )

        assert agent._fallback_chain[0]["model"] == "anthropic/claude-opus-4"
