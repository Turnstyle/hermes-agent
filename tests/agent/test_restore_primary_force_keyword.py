"""``restore_primary_runtime``'s ``force`` is a bound, keyword-only flag that bypasses no gate.

Max, Rosie and Snowdrop carried ``if not force and blocked:`` with no ``force`` binding, so every
turn-start restore that got past the cooldown check raised ``NameError``; Sheldon took ``force``
positionally; TurnerBook made it keyword-only but let it skip the reset-aware pool gate.
"""
from unittest.mock import patch

import pytest

from agent.agent_runtime_helpers import restore_primary_runtime
from tests.agent.test_independent_cooldown_review import active_agent

_GATE = "agent.agent_runtime_helpers._primary_reset_gate_blocks"
_REINSTALL = "agent.route_binding.reinstall_primary_runtime"


def test_restore_past_every_gate_reaches_the_primary():
    """The line that reads ``force`` runs on every eligible restore: it must be bound."""
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    agent._rate_limited_until = 0  # primary cooldown elapsed
    with patch(_GATE, return_value=(False, None, False)), patch(_REINSTALL) as reinstall:
        assert agent._restore_primary_runtime() is True
    reinstall.assert_called_once()


@pytest.mark.parametrize("gate", ["cooldown", "pool_reset"])
def test_force_bypasses_no_cooldown_gate(gate):
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    if gate == "pool_reset":
        agent._rate_limited_until = 0  # only the reset-aware pool gate still holds
    with patch(_GATE, return_value=(gate == "pool_reset", None, False)), patch(_REINSTALL) as reinstall:
        assert restore_primary_runtime(agent, force=True) is False
    reinstall.assert_not_called()
    assert agent._fallback_activated is True


def test_force_is_keyword_only():
    agent = active_agent({"provider": "openrouter", "model": "offline-model"})
    with pytest.raises(TypeError):
        restore_primary_runtime(agent, True)
