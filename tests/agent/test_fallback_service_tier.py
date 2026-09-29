"""Fallback tier belongs to the active entry, including the physical request route."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.fast_mode import effective_request_overrides, effective_service_tier, parse_fallback_service_tier
from hermes_cli.config import validate_config_structure
from run_agent import AIAgent


def _agent(entry, *, primary_tier=None, pinned=False, model="gpt-6-sol", api_mode=None):
    overrides = {"service_tier": "priority"} if pinned else {}
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.get_model_context_length", return_value=200_000),
    ):
        agent = AIAgent(
            api_key="test-key", base_url="https://api.openai.com/v1", provider="openai",
            model=model, api_mode=api_mode, service_tier=primary_tier, request_overrides=overrides,
            fallback_model=[entry], quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    agent.client = MagicMock()
    return agent


def _activate(agent, *, url="https://chatgpt.com/backend-api/codex"):
    client = MagicMock()
    client.base_url = url
    client.api_key = "fallback-test-key"
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, agent._fallback_chain[0]["model"])):
        assert agent._try_activate_fallback() is True


@pytest.mark.parametrize("raw,expected", [
    ("normal", None), ("default", None), ("standard", None), ("off", None),
    ("none", None), ("", None), (" FAST ", "priority"), ("priority", "priority"),
    ("on", "priority"), ("auto", "auto"), ("cold", "cold"),
])
def test_fallback_tier_vocabulary(raw, expected):
    assert parse_fallback_service_tier(raw) == (expected, True)
    assert parse_fallback_service_tier(0) == (None, False)


def test_priority_entry_is_active_only_on_fallback():
    entry = {"provider": "openai-codex", "model": "gpt-6-sol", "service_tier": "priority"}
    agent = _agent(entry)
    assert agent._fallback_chain[0]["service_tier"] == "priority"
    from hermes_cli.fallback_config import get_fallback_chain
    assert get_fallback_chain({"fallback_providers": [entry]})[0]["service_tier"] == "priority"
    assert "service_tier" not in effective_request_overrides(agent)
    _activate(agent)
    assert effective_service_tier(agent) == "priority"
    assert effective_request_overrides(agent)["service_tier"] == "priority"


def test_init_time_fallback_uses_selected_entry_tier():
    from agent.agent_init import _routed_client_kwargs

    entry = {"provider": "openai-codex", "model": "gpt-6-sol", "service_tier": "priority"}
    agent = SimpleNamespace(
        provider="openai", model="gpt-5.4", base_url=None, api_mode="chat_completions",
        service_tier=None, request_overrides={}, _fallback_activated=False,
    )
    client = SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex", api_key="test-key")

    def resolve(provider, **kwargs):
        return (client, entry["model"]) if provider == "openai-codex" else (None, None)

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("agent.agent_init._client_kwargs_from_routed", return_value={"api_key": "test-key"}),
    ):
        assert _routed_client_kwargs(agent, [entry], 60) == {"api_key": "test-key"}
    assert agent._initial_fallback_entry is entry
    assert effective_request_overrides(agent)["service_tier"] == "priority"


@pytest.mark.parametrize("primary_tier,pinned", [(None, False), ("priority", True), ("auto", False)])
def test_primary_and_fallback_tiers_are_isolated(primary_tier, pinned):
    agent = _agent({"provider": "openai-codex", "model": "gpt-6-sol"},
                   primary_tier=primary_tier, pinned=pinned)
    before = effective_request_overrides(agent)
    _activate(agent)
    assert not {"service_tier", "speed"} & effective_request_overrides(agent).keys()
    with patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()):
        assert agent._restore_primary_runtime() is True
    assert effective_service_tier(agent) == primary_tier
    assert effective_request_overrides(agent) == before


def test_unsupported_xai_route_warns_once_and_sends_no_tier(caplog):
    entry = {"provider": "xai-oauth", "model": "grok-4.7", "service_tier": "priority"}
    agent = _agent(entry, primary_tier="priority", pinned=True)
    with caplog.at_level(logging.WARNING, logger="agent.fast_mode"):
        _activate(agent, url="https://api.x.ai/v1")
        assert "service_tier" not in effective_request_overrides(agent)
        from agent.transports.codex import ResponsesApiTransport
        wire = ResponsesApiTransport().build_kwargs(
            model=agent.model, messages=[{"role": "user", "content": "hi"}], tools=[],
            is_xai_responses=True, base_url=agent.base_url,
            request_overrides=effective_request_overrides(agent),
        )
        assert "service_tier" not in wire
        assert "service_tier" not in effective_request_overrides(agent)
        with patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()):
            agent._restore_primary_runtime()
        _activate(agent, url="https://api.x.ai/v1")
        assert "service_tier" not in effective_request_overrides(agent)
    warnings = [r for r in caplog.records if "does not support fast mode" in r.message]
    assert len(warnings) == 1


def test_invalid_tier_is_config_error_and_runtime_normal(caplog):
    entry = {"provider": "openai-codex", "model": "gpt-6-sol", "service_tier": "turbo"}
    for key, label in (("fallback_providers", "fallback_providers[0]"),
                       ("fallback_model", "fallback_model")):
        value = [entry] if key == "fallback_providers" else entry
        issues = validate_config_structure({key: value})
        assert any(i.severity == "error" and f"{label}.service_tier" in i.message
                   and "Allowed values" in i.hint for i in issues)
    agent = _agent(entry)
    with caplog.at_level(logging.ERROR, logger="agent.fast_mode"):
        _activate(agent)
        assert effective_service_tier(agent) is None
        assert "service_tier" not in effective_request_overrides(agent)
        with patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True
        _activate(agent)
    assert len([r for r in caplog.records if "Invalid fallback service_tier" in r.message]) == 1


def test_turn_result_reports_last_sent_tier():
    entry = {"provider": "openai", "model": "gpt-5.4", "service_tier": "priority"}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
        model="gpt-5.4", usage=None,
    )

    def run_turn(agent):
        sent = []
        agent._disable_streaming = True
        agent._interruptible_api_call = lambda kwargs: (sent.append(dict(kwargs)), response)[1]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.relay_llm.execute", side_effect=lambda request, callback, **kw: callback(request)),
        ):
            result = agent.run_conversation("hello")
        return result, sent

    primary = _agent(entry, model="gpt-5.4", api_mode="chat_completions")
    primary_result, primary_sent = run_turn(primary)
    assert primary_sent and "service_tier" not in primary_sent[-1]
    assert primary_result["service_tier"] is None

    fallback = _agent(entry, model="gpt-5.4", api_mode="chat_completions")
    from agent.fast_mode import activate_initial_fallback
    activate_initial_fallback(fallback, entry)
    fallback_result, fallback_sent = run_turn(fallback)
    assert fallback_sent and fallback_sent[-1]["service_tier"] == "priority"
    assert fallback_result["service_tier"] == "priority"
