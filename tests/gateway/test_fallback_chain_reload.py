"""Regression tests for #60955: gateway must not freeze fallback_providers.

Cron reloads ``fallback_providers`` from disk on every job. The gateway used to
freeze ``self._fallback_model`` at process start, so a chain configured (or
edited) after ``hermes gateway`` was already running never reached messaging
sessions — even though cron in the same process fell back correctly.

These tests pin the reload + cached-agent apply helpers without driving the
full Feishu session path.
"""

from __future__ import annotations

import time
from types import SimpleNamespace


def test_refresh_fallback_model_rereads_config(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
    )

    runner = SimpleNamespace(
        _fallback_model=None,
    )
    runner._load_fallback_model = GatewayRunner._load_fallback_model
    bound = GatewayRunner._refresh_fallback_model.__get__(runner)
    chain = bound()

    assert chain == [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    assert runner._fallback_model == chain

    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-sonnet-4.6\n"
    )
    updated = bound()
    assert updated == [
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}
    ]
    assert runner._fallback_model == updated


def test_apply_fallback_chain_skips_while_cooldown_holds_fallback():
    """Under H20, chain updates live even during active fallback; if active fallback is removed, switches off it."""
    from unittest.mock import MagicMock
    from gateway.run import GatewayRunner
    from agent.error_classifier import FailoverReason

    live = [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    new_chain = [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}]
    agent = SimpleNamespace(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="",
        _fallback_chain=live,
        _fallback_model=live[0],
        _fallback_index=1,
        _fallback_activated=True,
        _rate_limited_until=time.monotonic() + 30,
        _restore_primary_runtime=MagicMock(),
        _try_activate_fallback=MagicMock(),
    )
    GatewayRunner._apply_fallback_chain_to_agent(
        agent,
        new_chain,
    )

    # Chain is updated to new config
    assert agent._fallback_chain == new_chain
    assert agent._fallback_model == new_chain[0]
    # Because deepseek was removed from config, agent switches off it immediately
    agent._restore_primary_runtime.assert_called_once_with(force=True)
    # And falls back to the new chain because primary is still rate limited
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.rate_limit)




def test_load_fallback_model_static_unchanged_contract(tmp_path, monkeypatch):
    """_load_fallback_model remains a pure static reader used by refresh."""
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
        "fallback_model:\n"
        "  provider: nous\n"
        "  model: Hermes-4\n"
    )

    chain = GatewayRunner._load_fallback_model()
    assert chain == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "nous", "model": "Hermes-4"},
    ]
