"""An owner-scoped fallback chain (delegated child, cron job) survives agent init as pinned.

``scoped_fallback_chain`` marks an owner's own chain; ``_init_fallback_chain`` normalizes it into
a plain list, so it must record the marker for ``apply_fallback_chain_to_agent`` to refuse a live
profile-config replacement (fleet unit X05).
"""

from types import SimpleNamespace

import pytest

from agent import agent_init_fallback
from agent.fallback_chain_refresh import apply_fallback_chain_to_agent
from hermes_cli.fallback_config import scoped_fallback_chain

OWN = [{"provider": "anthropic", "model": "pinned-model"}]
PROFILE = [{"provider": "openrouter", "model": "profile-model"}]


@pytest.fixture
def init_agent(monkeypatch):
    import agent.agent_runtime_helpers as helpers
    monkeypatch.setattr(helpers, "sync_credential_pool_entry_id", lambda agent: None)

    def make(fallback_model):
        agent = SimpleNamespace(quiet_mode=True)
        agent_init_fallback._init_fallback_chain(agent, fallback_model)
        return agent
    return make


def test_owner_pinned_chain_is_never_replaced_by_profile_refresh(init_agent):
    agent = init_agent(scoped_fallback_chain(inherited=PROFILE, declared=OWN, pinned=True, owner="delegation"))
    assert agent._fallback_chain_pinned is True
    assert apply_fallback_chain_to_agent(agent, PROFILE) is False
    assert agent._fallback_chain == OWN


def test_declared_owner_chain_is_also_owned(init_agent):
    agent = init_agent(scoped_fallback_chain(inherited=PROFILE, declared=OWN, pinned=False, owner="cron"))
    assert agent._fallback_chain_pinned is True
    assert apply_fallback_chain_to_agent(agent, PROFILE) is False


def test_plain_profile_chain_still_follows_live_config(init_agent):
    agent = init_agent(list(OWN))
    assert agent._fallback_chain_pinned is False
    assert apply_fallback_chain_to_agent(agent, PROFILE) is True
    assert agent._fallback_chain == PROFILE
