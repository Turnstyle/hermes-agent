"""Per-wake token-budget regression tests for scheduled agents."""

from types import SimpleNamespace

from agent.conversation_loop import (
    _apply_cron_token_budget,
    _mark_cron_token_budget_after_usage,
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

    assert _apply_cron_token_budget(agent, 49_000) is False
    assert agent.max_tokens == 8192


def test_next_output_is_clamped_to_remaining_per_wake_headroom():
    agent = _agent(cap=50_000, used=20_000, default_output=16_000)

    assert _apply_cron_token_budget(agent, 29_000) is False
    assert agent.max_tokens == 1_000
    assert agent._cron_budget_stopped is False


def test_smaller_existing_output_limit_is_preserved():
    agent = _agent(cap=50_000, used=5_000, default_output=8192)

    assert _apply_cron_token_budget(agent, 10_000) is False
    assert agent.max_tokens == 8192


def test_no_request_fits_stops_before_provider():
    agent = _agent(cap=50_000, used=20_000, default_output=16_000)

    assert _apply_cron_token_budget(agent, 30_000) is True
    assert agent._cron_budget_stopped is True
    assert agent._cron_budget_stop_reason == "max_tokens"


def test_provider_reported_crossing_marks_wake_for_next_preflight():
    agent = _agent(cap=50_000, used=50_001)

    _mark_cron_token_budget_after_usage(agent)

    assert agent._cron_budget_stopped is True
    assert agent._cron_budget_stop_reason == "max_tokens"
