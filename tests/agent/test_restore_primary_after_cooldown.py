"""Tests for primary runtime restoration after rate-limit cooldown (H21)."""

import time
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import _arm_rate_limit_cooldown
try:
    from agent.fallback_cooldown import decay_rate_limit_backoff
except ImportError:
    decay_rate_limit_backoff = None
from agent.turn_finalizer import finalize_turn
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


class TestRestorePrimaryAfterCooldown:
    def test_exponential_backoff_capped_at_30_minutes(self):
        """When provider gives no reset time, exponential fallback caps at 1800s (30 min) for long-lived sessions."""
        agent = _make_agent()
        agent._long_lived_session = True
        agent._rate_limit_backoff_count = 10  # 60 * 2^10 = 61440 >> 1800

        with patch("agent.fallback_cooldown.time.monotonic", return_value=1000.0):
            cooldown = _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit, reset_at=None)

        assert cooldown == 1800
        assert agent._rate_limited_until == 2800.0

    def test_rate_limit_backoff_decays_on_successful_primary_turn(self):
        """_rate_limit_backoff_count must decay after a successful primary turn."""
        agent = _make_agent()
        agent._fallback_activated = False
        agent._rate_limit_backoff_count = 3

        finalize_turn(
            agent,
            final_response="Success",
            api_call_count=1,
            interrupted=False,
            failed=False,
            messages=[],
            conversation_history=[],
            effective_task_id="t1",
            turn_id="turn1",
            user_message="hello",
            original_user_message="hello",
            _should_review_memory=False,
            _turn_exit_reason="text_response(done)",
        )

        assert agent._rate_limit_backoff_count == 2

        # A second successful turn decays it further
        finalize_turn(
            agent,
            final_response="Success 2",
            api_call_count=1,
            interrupted=False,
            failed=False,
            messages=[],
            conversation_history=[],
            effective_task_id="t1",
            turn_id="turn2",
            user_message="hello 2",
            original_user_message="hello 2",
            _should_review_memory=False,
            _turn_exit_reason="text_response(done)",
        )

        assert agent._rate_limit_backoff_count == 1

    def test_failed_probe_rearms_exponential_backoff_without_losing_session(self):
        """When primary cooldown has passed, next turn returns to primary; if probe fails,
        cooldown re-arms without losing session."""
        agent = _make_agent(
            fallback_model={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )
        mock_client = _mock_resolve()
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)

        assert agent._fallback_activated is True
        assert agent._rate_limit_backoff_count == 1

        # Simulate cooldown has passed
        agent._rate_limited_until = time.monotonic() - 1

        # Turn starts: restore primary
        restored = agent._restore_primary_runtime()
        assert restored is True
        assert agent._fallback_activated is False
        assert agent.model == "primary-model"
        assert agent._rate_limit_backoff_count == 0

        # Probe fails with rate limit
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(mock_client, None)):
            fallback_activated = agent._try_activate_fallback(reason=FailoverReason.rate_limit)

        assert fallback_activated is True
        assert agent._fallback_activated is True
        assert agent._rate_limit_backoff_count == 1
        assert agent._rate_limited_until >= time.monotonic() + 55
