"""Unchanged failed routes must not be retried by unrelated chain edits."""
from unittest.mock import patch

import pytest

from agent.chat_completion_helpers import _fallback_entry_key
from agent.fallback_chain_refresh import FallbackRouteUnavailableError, apply_fallback_chain_to_agent
from tests.agent.test_independent_cooldown_review import active_agent


@pytest.mark.parametrize("edit", ["remove_active", "edit_other", "reorder"])
def test_partial_edit_retains_failed_route_memo_without_resolving_it(edit):
    bad = {"provider": "openrouter", "model": "known-bad"}
    active = {"provider": "openrouter", "model": "active"}
    other = {"provider": "openrouter", "model": "other"}
    agent = active_agent(active)
    agent._fallback_chain = [bad, active, other]
    agent._fallback_index = 2
    agent._unavailable_fallback_keys = {_fallback_entry_key(bad), _fallback_entry_key(other)}
    deadline = agent._rate_limited_until
    chains = {"remove_active": [bad], "edit_other": [bad, active, {**other, "model": "changed"}],
              "reorder": [other, active, bad]}
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, "known-bad")) as resolve:
        if edit == "remove_active":
            with pytest.raises(FallbackRouteUnavailableError):
                apply_fallback_chain_to_agent(agent, chains[edit])
        else:
            apply_fallback_chain_to_agent(agent, chains[edit])
        resolve.assert_not_called()
    assert _fallback_entry_key(bad) in agent._unavailable_fallback_keys
    assert (_fallback_entry_key(other) in agent._unavailable_fallback_keys) == (edit == "reorder")
    assert agent._rate_limited_until == deadline
    assert agent._rate_limit_backoff_count == 4
    agent.client.chat.completions.create.assert_not_called()


def test_changed_failed_route_can_be_resolved_again():
    old = {"provider": "openrouter", "model": "known-bad"}
    agent = active_agent({"provider": "openrouter", "model": "active"})
    agent._fallback_chain = [old, {"provider": "openrouter", "model": "active"}]
    agent._unavailable_fallback_keys = {_fallback_entry_key(old)}
    new = {**old, "base_url": "https://new.example/v1"}
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, "known-bad")) as resolve:
        with pytest.raises(FallbackRouteUnavailableError):
            apply_fallback_chain_to_agent(agent, [new])
        assert resolve.call_count == 1
    assert _fallback_entry_key(old) not in agent._unavailable_fallback_keys
    assert _fallback_entry_key(new) in agent._unavailable_fallback_keys
