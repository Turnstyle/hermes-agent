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


def test_apply_fallback_chain_keeps_live_activation_during_cooldown():
    """A cooldown-time edit keeps a still-configured live fallback and its cooldown, and re-points
    the walk past it, instead of skipping the edit entirely."""
    from gateway.run import GatewayRunner

    live = {"provider": "deepseek", "model": "deepseek-v4-flash"}
    deadline = time.monotonic() + 30
    agent = SimpleNamespace(
        provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1",
        _fallback_chain=[live], _fallback_model=live, _fallback_index=1,
        _fallback_activated=True, _rate_limited_until=deadline,
    )
    added = {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}
    GatewayRunner._apply_fallback_chain_to_agent(agent, [added, live])

    assert agent._fallback_chain == [added, live]
    assert agent._fallback_index == 2
    assert agent._fallback_activated is True
    assert agent._rate_limited_until == deadline


def test_apply_fallback_chain_refuses_removed_live_route_during_cooldown():
    """A removed live fallback is never kept just because the primary is cooling down."""
    import pytest

    from agent.fallback_chain_refresh import FallbackRouteUnavailableError
    from gateway.run import GatewayRunner

    live = {"provider": "deepseek", "model": "deepseek-v4-flash"}
    deadline = time.monotonic() + 30
    calls = []
    agent = SimpleNamespace(
        provider="deepseek", model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1",
        _fallback_chain=[live], _fallback_model=live, _fallback_index=1,
        _fallback_activated=True, _rate_limited_until=deadline,
        _restore_primary_runtime=lambda: calls.append("restore") or False,
        _try_activate_fallback=lambda: calls.append("walk") or False,
    )
    with pytest.raises(FallbackRouteUnavailableError):
        GatewayRunner._apply_fallback_chain_to_agent(agent, None)

    assert calls == ["restore", "walk"]
    assert agent._fallback_chain == []
    assert agent._rate_limited_until == deadline


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
