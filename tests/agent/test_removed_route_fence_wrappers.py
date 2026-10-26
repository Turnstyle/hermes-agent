"""A removed fallback that a refused turn fenced stays refused through the production turn-start
syncs of the classic CLI (``HermesCLI.chat``) and Desktop/TUI (``_prepare_turn_input``), including
on a later turn whose config.yaml read fails (X05 wrapper residual).

Both wrappers keep the last known-good chain on a torn read; they also returned before handing it to
``apply_fallback_chain_to_agent``, so the fence never ran and the turn after a refusal served from
the removed route. Real ``AIAgent`` held on a fallback by a primary cooldown, real config.yaml text,
provider client resolution mocked: no network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import cli
from agent.fallback_chain_refresh import FallbackRouteUnavailableError
from hermes_cli.config import get_config_path
from tests.agent.test_independent_cooldown_review import active_agent
from tests.agent.test_live_fallback_chain_refresh import _mock_resolve
from tui_gateway import server

OLD = {"provider": "openrouter", "model": "offline-model", "base_url": "https://offline.example/v1"}
INELIGIBLE = {**OLD, "base_url": "https://different.example/v1"}
REPLACEMENT = {**OLD, "base_url": "https://offline.example/v2"}
TORN = "fallback_providers: [\n  - provider: {{{\n"


def _chain_yaml(*entries) -> str:
    return "fallback_providers:\n" + "".join(
        f"  - provider: {e['provider']}\n    model: {e['model']}\n    base_url: {e['base_url']}\n" for e in entries)


class _StopAfterSync(Exception):
    pass


def _cli_turn(monkeypatch, shell, config_text: str) -> None:
    """One ``HermesCLI.chat`` turn against ``config_text``; a synced turn stops at image routing."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(shell, "_ensure_runtime_credentials", lambda: True)
    monkeypatch.setattr(shell, "_resolve_turn_agent_config",
                        lambda message: {"signature": shell._active_agent_route_signature, "model": None, "runtime": None})
    monkeypatch.setattr(shell, "_init_agent", lambda **kw: True)

    def stop(message, images):
        raise _StopAfterSync()

    monkeypatch.setattr(shell, "_chat_route_images", stop)
    shell.chat("hello")


def _tui_turn(monkeypatch, tmp_path, session: dict, config_text: str) -> None:
    """One Desktop/TUI turn admission against ``config_text``; a synced turn stops at the next sync."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(server, "_active_config_path", lambda: cfg_path)
    monkeypatch.setattr(server, "_profile_runtime_scope_tokens", lambda profile_home: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    for name in ("_apply_pending_model_switch", "_sync_agent_model_with_config", "_sync_agent_compression_with_config"):
        monkeypatch.setattr(server, name, lambda sid, session: None)

    def stop(sid, session):
        raise _StopAfterSync()

    monkeypatch.setattr(server, "_sync_bot_capabilities", stop)
    st = server._TurnRun(agent=None, one_turn_restore=None, terminal_callback=None, receipt_committed=False)
    server._prepare_turn_input("sid", session, st, "hello", [])


@pytest.fixture(params=["cli", "tui"])
def surface(request, monkeypatch, tmp_path):
    """``(agent, turn)``: a real agent active on ``OLD`` behind a held primary cooldown, and one
    production turn start on the CLI or Desktop/TUI surface against a config.yaml text."""
    agent = active_agent(OLD)
    if request.param == "cli":
        shell = cli.HermesCLI(compact=True, max_turns=1)
        shell.agent = agent
        return agent, lambda text: _cli_turn(monkeypatch, shell, text)
    session = {"agent": agent, "session_key": "session-x05-wrapper"}
    return agent, lambda text: _tui_turn(monkeypatch, tmp_path, session, text)


def _chain_urls(agent) -> list:
    return [e.get("base_url") for e in agent._fallback_chain]


def test_fenced_removed_route_is_refused_on_a_torn_config_turn_until_replaced(surface):
    agent, turn = surface
    removed_client = agent.client
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, OLD["model"])):
        # The active fallback is removed; the primary is cooling down and the replacement is ineligible.
        with pytest.raises(FallbackRouteUnavailableError):
            turn(_chain_yaml(INELIGIBLE))
        assert agent._removed_fallback_route is not None
        # config.yaml unreadable on the next turns: the last known-good chain, refused again each time.
        for _ in range(2):
            with pytest.raises(FallbackRouteUnavailableError):
                turn(TORN)
        assert _chain_urls(agent) == [INELIGIBLE["base_url"]]
        assert agent.base_url == OLD["base_url"] and agent._removed_fallback_route is not None
    removed_client.chat.completions.create.assert_not_called()

    with patch("agent.auxiliary_client.resolve_provider_client",
               return_value=(_mock_resolve(base_url=REPLACEMENT["base_url"]), REPLACEMENT["model"])):
        with pytest.raises(_StopAfterSync):
            turn(_chain_yaml(REPLACEMENT))
    assert agent.base_url == REPLACEMENT["base_url"]
    assert agent._removed_fallback_route is None

    # The fence is spent: a later torn read is an ordinary keep-last-good turn again.
    with pytest.raises(_StopAfterSync):
        turn(TORN)
    assert _chain_urls(agent) == [REPLACEMENT["base_url"]] and agent.base_url == REPLACEMENT["base_url"]
    removed_client.chat.completions.create.assert_not_called()


def test_torn_config_without_a_fenced_removal_keeps_the_last_known_good_chain(surface):
    agent, turn = surface
    deadline = agent._rate_limited_until
    with pytest.raises(_StopAfterSync):
        turn(_chain_yaml(OLD))
    with pytest.raises(_StopAfterSync):
        turn(TORN)
    assert _chain_urls(agent) == [OLD["base_url"]]
    assert agent.base_url == OLD["base_url"] and agent._fallback_index == 1
    assert getattr(agent, "_removed_fallback_route", None) is None
    assert agent._rate_limited_until == deadline
