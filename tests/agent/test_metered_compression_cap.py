"""Tests for H25: metered provider compression cap.

Metered providers (openai-codex, anthropic, xai-oauth) cap compression triggers
at metered_threshold_tokens (default 150,000) rather than the default 256,000
threshold_tokens, preventing 200-245k context resends.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.agent_init import _parse_compression_config
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_metered_provider_triggers_compaction_at_cap():
    """A 1M-window Codex model with 160k tokens triggers compaction at 150k cap."""
    with patch("agent.model_metadata.get_model_context_length", return_value=1_000_000):
        compressor = ContextCompressor(
            model="gpt-5.5",
            threshold_percent=0.50,
            threshold_tokens_cap=256_000,
            provider="openai-codex",
            quiet_mode=True,
            metered_threshold_tokens=150_000,
            metered_providers=["openai-codex", "anthropic", "xai-oauth"],
        )
        compressor.context_length = 1_000_000

        # Trigger threshold must be capped at 150,000 for openai-codex
        assert compressor.threshold_tokens == 150_000
        # 160k tokens must trigger compaction
        assert compressor.should_compress(160_000) is True
        # Under 150k tokens must not trigger
        assert compressor.should_compress(140_000) is False


def test_non_metered_provider_retains_default_cap():
    """A 1M-window model on a non-metered provider (e.g. ollama) keeps the 256k cap."""
    with patch("agent.model_metadata.get_model_context_length", return_value=1_000_000):
        compressor = ContextCompressor(
            model="llama-local",
            threshold_percent=0.50,
            threshold_tokens_cap=256_000,
            provider="ollama",
            quiet_mode=True,
            metered_threshold_tokens=150_000,
            metered_providers=["openai-codex", "anthropic", "xai-oauth"],
        )
        compressor.context_length = 1_000_000

        # Trigger threshold retains 256,000 cap
        assert compressor.threshold_tokens == 256_000
        # 160k tokens must NOT trigger compaction on non-metered provider
        assert compressor.should_compress(160_000) is False
        # 260k tokens triggers compaction
        assert compressor.should_compress(260_000) is True


def test_update_model_switches_metered_cap_dynamically():
    """Fallback and model updates dynamically toggle the metered cap."""
    with patch("agent.model_metadata.get_model_context_length", return_value=1_000_000):
        compressor = ContextCompressor(
            model="gpt-5.5",
            threshold_percent=0.50,
            threshold_tokens_cap=256_000,
            provider="openai-codex",
            quiet_mode=True,
            metered_threshold_tokens=150_000,
            metered_providers=["openai-codex", "anthropic", "xai-oauth"],
        )
        compressor.context_length = 1_000_000
        assert compressor.threshold_tokens == 150_000

        # Switch to ollama (non-metered) -> returns to 256k cap
        compressor.update_model(
            model="llama-local",
            context_length=1_000_000,
            provider="ollama",
        )
        assert compressor.threshold_tokens == 256_000

        # Switch to anthropic (metered) -> drops back to 150k cap
        compressor.update_model(
            model="claude-3-7-sonnet",
            context_length=1_000_000,
            provider="anthropic",
        )
        assert compressor.threshold_tokens == 150_000


def test_parse_compression_config_metered_defaults_and_overrides():
    """_parse_compression_config extracts defaults and custom overrides."""
    agent = SimpleNamespace(model="m", provider="openrouter", api_mode="chat_completions", quiet_mode=True)

    # Defaults
    cs = _parse_compression_config(agent, {})
    assert cs.metered_threshold_tokens == 150_000
    assert cs.metered_providers == ["openai-codex", "anthropic", "xai-oauth"]

    # Custom overrides
    custom_cfg = {
        "compression": {
            "metered_threshold_tokens": 120_000,
            "metered_providers": ["custom-prov"],
        }
    }
    cs_custom = _parse_compression_config(agent, custom_cfg)
    assert cs_custom.metered_threshold_tokens == 120_000
    assert cs_custom.metered_providers == ["custom-prov"]
