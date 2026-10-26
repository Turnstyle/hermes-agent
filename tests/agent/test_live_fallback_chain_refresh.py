"""Live fallback-chain refresh contract (H20): ``agent.fallback_chain_refresh``.

The CLI, Desktop/TUI and messaging gateway hand each turn's freshly read chain to one helper; these
tests drive it against a real ``AIAgent`` (provider clients mocked, no network).
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from agent.fallback_chain_refresh import apply_fallback_chain_to_agent
from hermes_cli.fallback_config import DeclaredFallbackChain, PinnedFallbackChain, scoped_fallback_chain
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {"type": "function", "function": {
            "name": n, "description": f"{n} tool", "parameters": {"type": "object", "properties": {}}}}
        for n in names
    ]


def _make_agent(fallback_model=None, provider="custom", base_url="https://my-llm.example.com/v1", platform=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.get_model_context_length", return_value=200_000),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="unit-test", base_url=base_url, provider=provider, model="primary-model",
            platform=platform, quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_resolve(base_url="https://openrouter.ai/api/v1", api_key="unit-test"):
    mock_client = MagicMock()
    mock_client.api_key = api_key
    mock_client.base_url = base_url
    return mock_client


_GLOBAL_EDIT = {"fallback_providers": [{"provider": "openrouter", "model": "new-global-model"}]}


class TestLiveFallbackChainRefresh:
    def test_live_config_edit_updates_fallback_chain_between_turns(self):
        agent = _make_agent(fallback_model=[{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}])
        agent._unavailable_fallback_keys = {"stale_key"}
        new_config = {"fallback_providers": [
            {"provider": "openrouter", "model": "anthropic/claude-opus-4"},
            {"provider": "openai-codex", "model": "gpt-6-sol"},
        ]}
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            assert apply_fallback_chain_to_agent(agent) is True

        assert [e["model"] for e in agent._fallback_chain] == ["anthropic/claude-opus-4", "gpt-6-sol"]
        # A key no current entry carries is forgotten on a real change.
        assert agent._unavailable_fallback_keys == set()

    def test_removing_active_fallback_switches_off_at_once(self):
        agent = _make_agent(fallback_model=[
            {"provider": "openai-codex", "model": "gpt-6-sol"},
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        ])
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert (agent._fallback_activated, agent.provider, agent.model) == (True, "openai-codex", "gpt-6-sol")
        deadline = agent._rate_limited_until

        new_config = {"fallback_providers": [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]}
        with (
            patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config),
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)),
            patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
        ):
            apply_fallback_chain_to_agent(agent)

        assert agent.provider == "openrouter"
        # Moving off a removed route is not a new primary failure: the cooldown is untouched.
        assert agent._rate_limited_until == deadline

    def test_unchanged_active_fallback_survives_cooldown_edit(self):
        """An edit elsewhere in the chain keeps the live activation and re-points the walk index."""
        active = {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}
        agent = _make_agent(fallback_model=[active])
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(_mock_resolve(), None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        deadline = agent._rate_limited_until
        new_chain = [{"provider": "openai-codex", "model": "gpt-6-sol"}, active]
        assert apply_fallback_chain_to_agent(agent, new_chain) is True
        assert agent._fallback_activated is True and agent.provider == "openrouter"
        assert agent._fallback_index == 2
        assert agent._rate_limited_until == deadline

    def test_pinned_marker_survives_profile_refresh(self):
        pinned_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=[{"provider": "anthropic", "model": "pinned-model"}], pinned=True, owner="delegation")
        assert isinstance(pinned_chain, PinnedFallbackChain)
        agent = _make_agent(fallback_model=pinned_chain)
        agent._fallback_chain_pinned = True  # what agent init records for a marked chain
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=_GLOBAL_EDIT):
            assert apply_fallback_chain_to_agent(agent) is False
        assert [e["model"] for e in agent._fallback_chain] == ["pinned-model"]

    def test_declared_chain_of_unpinned_owner_is_marked_owned(self):
        declared_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "global-model"}],
            declared=[{"provider": "groq", "model": "llama-3.3-70b"}], pinned=False, owner="delegation")
        assert isinstance(declared_chain, DeclaredFallbackChain)
        assert declared_chain._pinned is True
        assert declared_chain == [{"provider": "groq", "model": "llama-3.3-70b"}]

    @pytest.mark.parametrize("pinned", [True, False])
    def test_scoped_none_and_empty_contracts_are_unchanged(self, pinned):
        """``None`` returns stay None (tests/cron/test_cron_pinned_job_fallback.py contract)."""
        inherited = [{"provider": "openrouter", "model": "global-model"}]
        assert scoped_fallback_chain(inherited=inherited, declared=[], pinned=pinned, owner="cron job") is None
        inherited_result = scoped_fallback_chain(inherited=inherited, declared=None, pinned=pinned, owner="cron job")
        assert inherited_result == (None if pinned else inherited)
        assert not getattr(inherited_result, "_pinned", False)

    def test_unpinned_main_agent_global_chain_refreshes(self):
        initial_chain = scoped_fallback_chain(
            inherited=[{"provider": "openrouter", "model": "initial-model"}], declared=None, pinned=False, owner="main")
        agent = _make_agent(fallback_model=initial_chain, platform="cli")
        assert getattr(agent, "_fallback_chain_pinned", False) is False
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=_GLOBAL_EDIT):
            apply_fallback_chain_to_agent(agent)
        assert [e["model"] for e in agent._fallback_chain] == ["new-global-model"]

    def test_torn_config_keeps_last_known_good_chain(self):
        initial_chain = [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]
        agent = _make_agent(fallback_model=initial_chain)
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(_mock_resolve(), None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert agent._fallback_activated is True

        with patch("hermes_cli.config_effective.load_user_config_effective", side_effect=ValueError("torn write")):
            assert apply_fallback_chain_to_agent(agent) is False
        assert agent._fallback_chain == initial_chain
        assert agent._fallback_activated is True

    def test_entry_metadata_edit_propagates_and_clears_unavailable_keys(self):
        agent = _make_agent(fallback_model=[
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4", "api_key_env": "OLD_KEY"}])
        agent._unavailable_fallback_keys = {"stale_key"}
        new_config = {"fallback_providers": [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4", "api_key_env": "NEW_KEY"}]}
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=new_config):
            apply_fallback_chain_to_agent(agent)
        assert agent._fallback_chain[0]["api_key_env"] == "NEW_KEY"
        assert agent._unavailable_fallback_keys == set()
