"""MoA consumes ``auxiliary.moa_reference`` / ``auxiliary.moa_aggregator`` reasoning effort as the
task-level default; an explicit per-slot ``reasoning_effort`` in the preset always wins.

Precedence per call: preset slot > task slot (``auxiliary.moa_*``) > today's fallback (none for
reference advisors; per-model > global for the aggregator). An explicit task
``extra_body.service_tier`` replaces the tier the slot's endpoint (or the main request) carried.
Unset task policy keeps today's behaviour exactly, so a global ``xhigh`` still never leaks into every
advisor and the endpoint's own tier still applies.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_constants import parse_reasoning_effort

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_MEDIUM_NORMAL_TASK = {"reasoning_effort": "medium", "extra_body": {"service_tier": "default"}}


def _response(content="advice"):
    message = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None, model="fake")


@pytest.fixture
def moa_config(monkeypatch):
    from agent import moa_loop

    monkeypatch.setattr(moa_loop, "_runtime_cache", {})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {"provider": "custom", "base_url": "http://127.0.0.1:8318/v1", "api_key": "k",
                          "api_mode": "chat_completions"},
    )

    def apply(auxiliary, agent_cfg=None):
        cfg = {"model": {"provider": "custom", "default": "primary-model"},
               "agent": agent_cfg or {"reasoning_effort": "high"}, "auxiliary": auxiliary}
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
        return cfg

    return apply


def _reference_reasoning(monkeypatch, slot):
    from agent import moa_loop

    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response()

    monkeypatch.setattr(moa_loop, "call_llm", fake_call_llm)
    moa_loop._run_reference(slot, [{"role": "user", "content": "judge this"}])
    assert captured, "reference call never reached call_llm"
    return captured["reasoning_config"]


def test_reference_task_effort_fills_a_slot_without_one(moa_config, monkeypatch):
    moa_config({"moa_reference": {"reasoning_effort": "medium"}})

    assert _reference_reasoning(monkeypatch, {"provider": "custom", "model": "advisor"}) == \
        parse_reasoning_effort("medium")


def test_reference_slot_effort_beats_the_task_default(moa_config, monkeypatch):
    moa_config({"moa_reference": {"reasoning_effort": "medium"}})

    slot = {"provider": "custom", "model": "advisor", "reasoning_effort": "low"}
    assert _reference_reasoning(monkeypatch, slot) == parse_reasoning_effort("low")


def test_reference_task_none_disables_reasoning(moa_config, monkeypatch):
    moa_config({"moa_reference": {"reasoning_effort": "none"}})

    assert _reference_reasoning(monkeypatch, {"provider": "custom", "model": "advisor"}) == {"enabled": False}


def test_reference_unset_task_keeps_provider_default(moa_config, monkeypatch):
    moa_config({"moa_reference": {}}, agent_cfg={"reasoning_effort": "xhigh"})

    assert _reference_reasoning(monkeypatch, {"provider": "custom", "model": "advisor"}) is None


def test_aggregator_task_effort_beats_primary_global(moa_config):
    from agent.moa_loop import _aggregator_reasoning_config

    moa_config({"moa_aggregator": {"reasoning_effort": "medium"}}, agent_cfg={"reasoning_effort": "high"})

    assert _aggregator_reasoning_config({"provider": "custom", "model": "agg"}) == parse_reasoning_effort("medium")


def test_aggregator_slot_effort_beats_the_task_default(moa_config):
    from agent.moa_loop import _aggregator_reasoning_config

    moa_config({"moa_aggregator": {"reasoning_effort": "medium"}})

    slot = {"provider": "custom", "model": "agg", "reasoning_effort": "low"}
    assert _aggregator_reasoning_config(slot) == parse_reasoning_effort("low")


def test_aggregator_unset_task_keeps_global_fallback(moa_config):
    from agent.moa_loop import _aggregator_reasoning_config

    moa_config({"moa_aggregator": {}}, agent_cfg={"reasoning_effort": "high"})

    assert _aggregator_reasoning_config({"provider": "custom", "model": "agg"}) == parse_reasoning_effort("high")


# ── Request boundary: real call_llm, only the HTTP client stubbed ───────────────────────────────

_ENDPOINT_FAST = {"provider": "custom", "base_url": "http://127.0.0.1:8318/v1", "api_key": "k",
                  "api_mode": "chat_completions", "request_overrides": {"extra_body": {"service_tier": "priority"}}}


def _wire_requests(monkeypatch, auxiliary):
    """Run a one-advisor MoA turn; return ``{model: (effort, tier)}`` as sent on the wire."""
    from unittest.mock import MagicMock

    from agent import moa_loop

    cfg = {"model": {"provider": "custom", "default": "primary-model"},
           "agent": {"reasoning_effort": "high"}, "auxiliary": auxiliary}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    monkeypatch.setattr(moa_loop, "_runtime_cache", {})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: dict(_ENDPOINT_FAST))
    client = MagicMock()
    client.base_url = _ENDPOINT_FAST["base_url"]
    client.chat.completions.create.side_effect = lambda **kw: _response(f"from {kw['model']}")
    monkeypatch.setattr("agent.auxiliary_client._get_cached_client",
                        lambda provider, model, *a, **k: (client, model))

    moa_loop.aggregate_moa_context(
        user_prompt="judge this", api_messages=[{"role": "user", "content": "judge this"}],
        reference_models=[{"provider": "custom", "model": "advisor"}],
        aggregator={"provider": "custom", "model": "agg"},
    )

    sent = {}
    for call in client.chat.completions.create.call_args_list:
        kwargs = call.kwargs
        body = kwargs.get("extra_body") or {}
        reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
        effort = kwargs.get("reasoning_effort") or body.get("reasoning_effort") or reasoning.get("effort")
        sent[kwargs["model"]] = (effort, kwargs.get("service_tier") or body.get("service_tier"))
    assert set(sent) == {"advisor", "agg"}, sent
    return sent


def test_explicit_task_medium_normal_reaches_the_wire_over_endpoint_fast_and_primary_high(monkeypatch):
    sent = _wire_requests(monkeypatch, {"moa_reference": _MEDIUM_NORMAL_TASK, "moa_aggregator": _MEDIUM_NORMAL_TASK})

    assert sent == {"advisor": ("medium", "default"), "agg": ("medium", "default")}


def test_unset_task_policy_keeps_todays_wire_request(monkeypatch):
    sent = _wire_requests(monkeypatch, {"moa_reference": {}, "moa_aggregator": {}})

    assert sent["advisor"] == (None, "priority")  # no global leak into advisors; the endpoint's own tier
    assert sent["agg"] == ("high", "priority")    # the aggregator keeps the global fallback
