"""A config refresh never bypasses the primary cooldown and never keeps a removed route."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from agent.fallback_chain_refresh import FallbackRouteUnavailableError, apply_fallback_chain_to_agent
from tests.agent.test_live_fallback_chain_refresh import _make_agent, _mock_resolve


def active_agent(entry):
    """A real agent activated onto ``entry`` and held there by a long primary cooldown."""
    agent = _make_agent(fallback_model=[entry])
    client = _mock_resolve(base_url=entry.get("base_url", "https://offline.example/v1"))
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, entry["model"])):
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    agent._rate_limited_until = time.monotonic() + 1000
    agent._rate_limit_backoff_count = 4
    return agent


@pytest.mark.parametrize("new_url", ["https://different.example/v1", "https://offline.example/v2"])
def test_explicit_endpoint_change_cannot_keep_removed_client(new_url):
    old = {"provider": "openrouter", "model": "offline-model", "base_url": "https://offline.example/v1"}
    agent = active_agent(old)
    deadline = agent._rate_limited_until
    new = {**old, "base_url": new_url}
    with patch("agent.auxiliary_client.resolve_provider_client",
               return_value=(_mock_resolve(base_url=new_url), new["model"])):
        apply_fallback_chain_to_agent(agent, [new])
    assert agent.base_url == new_url
    assert agent._rate_limited_until == deadline
    assert agent._rate_limit_backoff_count == 4
    assert agent._fallback_index == 1


def test_explicit_endpoint_no_eligible_replacement_refuses_before_send():
    old = {"provider": "openrouter", "model": "offline-model", "base_url": "https://offline.example/v1"}
    agent = active_agent(old)
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, "offline-model")):
        with pytest.raises(FallbackRouteUnavailableError, match="no configured fallback is eligible"):
            apply_fallback_chain_to_agent(agent, [{**old, "base_url": "https://different.example/v1"}])
    agent.client.chat.completions.create.assert_not_called()
    assert agent._rate_limit_backoff_count == 4


def test_removed_route_is_refused_even_during_cooldown():
    """Upstream skipped every refresh while a cooldown held the fallback, so a removed route kept serving."""
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    deadline = agent._rate_limited_until
    with pytest.raises(FallbackRouteUnavailableError):
        apply_fallback_chain_to_agent(agent, [])
    assert agent._fallback_chain == []
    assert agent._rate_limited_until == deadline and agent._rate_limit_backoff_count == 4
    agent.client.chat.completions.create.assert_not_called()


def test_pinned_route_and_unavailable_memo_are_not_mutated():
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    agent._fallback_chain_pinned = True
    agent._unavailable_fallback_keys = {"memo"}
    original = agent._fallback_chain[:]
    assert not apply_fallback_chain_to_agent(agent, [])
    assert agent._fallback_chain == original
    assert agent._unavailable_fallback_keys == {"memo"}


def test_invalid_config_keeps_last_good_route_and_cooldown():
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    original, deadline = agent._fallback_chain[:], agent._rate_limited_until
    with patch("hermes_cli.config_effective.load_user_config_effective",
               side_effect=ValueError("malformed offline config")):
        assert not apply_fallback_chain_to_agent(agent)
    assert agent._fallback_chain == original
    assert agent._rate_limited_until == deadline


def test_once_override_route_is_not_treated_as_removed_fallback():
    """``_fallback_activated`` also marks a `/model --once` route; only a former chain entry is "removed"."""
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    agent.provider, agent.model = "anthropic", "one-turn-model"
    agent._try_activate_fallback = MagicMock(side_effect=AssertionError("walked the chain"))
    apply_fallback_chain_to_agent(agent, [{"provider": "openrouter", "model": "other"}])
    assert (agent.provider, agent.model) == ("anthropic", "one-turn-model")


def test_cached_gateway_refuses_before_building_or_wiring_a_turn():
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    runner = SimpleNamespace(
        _agent_config_signature=MagicMock(return_value="sig"),
        _extract_cache_busting_config=MagicMock(return_value={}),
        _refresh_fallback_model=MagicMock(return_value=[]),
        _apply_fallback_chain_to_agent=GatewayRunner._apply_fallback_chain_to_agent)
    fake = SimpleNamespace(
        _runner=runner,
        _ctx=SimpleNamespace(enabled_toolsets=[], user_config={}, source=SimpleNamespace()),
        _skip_context_files=MagicMock(return_value=True),
        _cached_sid_is_dead=MagicMock(return_value=(None, False)),
        _current_message_count=MagicMock(return_value=0),
        _lookup_cached_agent=MagicMock(return_value=SimpleNamespace(agent=agent, reused=True, evicted=None)),
        _build_fresh_agent=MagicMock(), _release_evicted_agent=MagicMock())
    with pytest.raises(FallbackRouteUnavailableError, match="Active fallback removed"):
        TurnRunner._resolve_turn_agent(fake, {"model": "primary-model", "runtime": {}}, "test", None, 1, None, {})
    fake._build_fresh_agent.assert_not_called()
    agent.client.chat.completions.create.assert_not_called()
