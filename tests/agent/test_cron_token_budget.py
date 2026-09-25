"""Per-wake token-budget regression tests for scheduled agents."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_cron_budget import (
    apply_cron_token_budget,
    cron_token_budget_stop_message,
    cron_turn_limit_stop,
    mark_cron_token_budget_after_usage,
)


def _agent(*, cap=None, used=0, default_output=8192):
    return SimpleNamespace(
        _cron_max_tokens=cap,
        _cron_default_response_max_tokens=default_output,
        _cron_budget_stopped=False,
        _cron_budget_stop_reason=None,
        session_total_tokens=used,
        max_tokens=default_output,
    )


def test_missing_cron_cap_leaves_output_limit_unchanged():
    agent = _agent(cap=None)

    assert apply_cron_token_budget(agent, 49_000) is False
    assert agent.max_tokens == 8192


def test_interactive_agent_without_cron_attributes_is_untouched():
    agent = SimpleNamespace(max_tokens=4096, session_total_tokens=10**9)

    assert apply_cron_token_budget(agent, 10**9) is False
    mark_cron_token_budget_after_usage(agent)
    assert cron_turn_limit_stop(agent, 99) is None
    assert agent.max_tokens == 4096
    assert not hasattr(agent, "_cron_budget_stopped")


def test_mock_doubles_never_arm_a_budget():
    agent = MagicMock()

    assert apply_cron_token_budget(agent, 1) is False
    assert cron_turn_limit_stop(agent, 1) is None


def test_next_output_is_clamped_to_remaining_per_wake_headroom():
    agent = _agent(cap=50_000, used=20_000, default_output=16_000)

    assert apply_cron_token_budget(agent, 29_000) is False
    assert agent.max_tokens == 1_000
    assert agent._cron_budget_stopped is False


def test_smaller_existing_output_limit_is_preserved():
    agent = _agent(cap=50_000, used=5_000, default_output=8192)

    assert apply_cron_token_budget(agent, 10_000) is False
    assert agent.max_tokens == 8192


def test_unset_default_output_uses_the_headroom():
    agent = _agent(cap=50_000, used=5_000, default_output=None)

    assert apply_cron_token_budget(agent, 10_000) is False
    assert agent.max_tokens == 35_000


def test_no_request_fits_stops_before_provider():
    agent = _agent(cap=50_000, used=20_000, default_output=16_000)

    assert apply_cron_token_budget(agent, 30_000) is True
    assert agent._cron_budget_stopped is True
    assert agent._cron_budget_stop_reason == "max_tokens"
    message = cron_token_budget_stop_message(agent, 30_000)
    assert "20000/50000" in message and "~30000" in message


def test_provider_reported_crossing_marks_wake_for_next_preflight():
    agent = _agent(cap=50_000, used=50_001)

    mark_cron_token_budget_after_usage(agent)

    assert agent._cron_budget_stopped is True
    assert agent._cron_budget_stop_reason == "max_tokens"


def test_usage_below_cap_does_not_mark():
    agent = _agent(cap=50_000, used=49_999)

    mark_cron_token_budget_after_usage(agent)

    assert agent._cron_budget_stopped is False
