"""An EXPLICIT ``auxiliary.background_review`` effort / speed tier is the review fork's policy, even on the
same-model path and even when the primary runs high + fast; an unset slot keeps exact cache parity.

Unset is the default for every user, and there the fork must stay byte-identical to the parent
(reasoning config, request overrides, cached prompt): the #30532 / #104116 parity contract. A user who
pins ``reasoning_effort`` / ``extra_body.service_tier`` on the slot has chosen that cost trade-off
(one cold prefix per review) for THIS slot; the parent's unrelated high/priority must not override it.
/btw (``side_question``) shares the fork builder but not the background_review slot.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

import agent.background_review as bg_review
import run_agent
from agent.background_review import build_cache_parity_fork
from agent.fast_mode import parse_service_tier
from hermes_constants import parse_reasoning_effort

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata
from tests.agent.test_background_review_cache_parity import _make_agent_stub, _make_recorder_class

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_PARENT_REASONING = {"enabled": True, "effort": "high"}
_PARENT_OVERRIDES = {"service_tier": "priority"}
_EXPLICIT_MEDIUM_NORMAL = {"reasoning_effort": "medium", "extra_body": {"service_tier": "default"}}


def _fork(task_cfg, *, write_origin="background_review", routed_runtime=None):
    captured = {}
    agent = _make_agent_stub(run_agent.AIAgent)
    agent.reasoning_config = dict(_PARENT_REASONING)
    agent.request_overrides = dict(_PARENT_OVERRIDES)
    agent.service_tier = "priority"
    agent._emit_warning = lambda _msg: None
    with ExitStack() as stack:
        stack.enter_context(patch.object(run_agent, "AIAgent", _make_recorder_class(captured)))
        if routed_runtime is not None:
            stack.enter_context(patch.object(bg_review, "_resolve_review_runtime", return_value=routed_runtime))
        fork, _rt, routed = build_cache_parity_fork(
            agent, task_cfg, max_iterations=5, write_origin=write_origin)
    return agent, fork, captured["init_kwargs"], routed


def _wire_tiers(kwargs):
    """Every place a fork can carry a speed tier, parsed (None = normal)."""
    overrides = kwargs.get("request_overrides") or {}
    extra_body = overrides.get("extra_body") or {}
    return {parse_service_tier(raw) for raw in (
        kwargs.get("service_tier"), overrides.get("service_tier"), extra_body.get("service_tier"))
        if raw is not None}


def test_explicit_medium_normal_slot_wins_on_the_same_model_fork():
    agent, _fork_agent, kwargs, routed = _fork(_EXPLICIT_MEDIUM_NORMAL)

    assert not routed
    assert kwargs["reasoning_config"] == parse_reasoning_effort("medium")
    assert _wire_tiers(kwargs) <= {None}, kwargs.get("request_overrides")
    # The parent is untouched: its own turns stay high + fast.
    assert agent.reasoning_config == _PARENT_REASONING
    assert agent.request_overrides == _PARENT_OVERRIDES


def test_explicit_effort_alone_keeps_the_parent_tier():
    """Task policy supplies only what it names: an effort pin does not silently reset the tier."""
    _agent, _fork_agent, kwargs, _routed = _fork({"reasoning_effort": "medium"})

    assert kwargs["reasoning_config"] == parse_reasoning_effort("medium")
    assert kwargs["request_overrides"].get("service_tier") == "priority"


def test_explicit_tier_alone_keeps_the_parent_reasoning():
    _agent, _fork_agent, kwargs, _routed = _fork({"extra_body": {"service_tier": "default"}})

    assert kwargs["reasoning_config"] == _PARENT_REASONING
    assert _wire_tiers(kwargs) <= {None}, kwargs.get("request_overrides")


def test_explicit_disabled_reasoning_is_kept():
    _agent, _fork_agent, kwargs, _routed = _fork({"reasoning_effort": "none"})

    assert kwargs["reasoning_config"] == {"enabled": False}


@pytest.mark.parametrize("task_cfg", [{}, {"reasoning_effort": ""}, {"provider": "auto", "model": ""}])
def test_unset_slot_keeps_byte_identical_cache_parity(task_cfg):
    agent, fork, kwargs, routed = _fork(task_cfg)

    assert not routed
    assert kwargs["reasoning_config"] == _PARENT_REASONING
    assert kwargs["request_overrides"] == _PARENT_OVERRIDES
    assert fork._cached_system_prompt == agent._cached_system_prompt
    assert fork.session_start == agent.session_start


def test_btw_fork_ignores_the_background_review_slot():
    """/btw is a one-call prefix extension of the live turn: it keeps the parent's exact request."""
    _agent, _fork_agent, kwargs, _routed = _fork(_EXPLICIT_MEDIUM_NORMAL, write_origin="side_question")

    assert kwargs["reasoning_config"] == _PARENT_REASONING
    assert kwargs["request_overrides"] == _PARENT_OVERRIDES


def test_routed_fork_applies_the_explicit_normal_tier():
    routed_runtime = {
        "provider": "custom", "model": "aux-model", "api_key": "k", "base_url": "http://127.0.0.1:8318/v1",
        "api_mode": "chat_completions", "credential_pool": None,
        "request_overrides": {"service_tier": "priority"},
        "max_tokens": None, "command": None, "args": [], "routed": True,
    }
    _agent, _fork_agent, kwargs, routed = _fork(_EXPLICIT_MEDIUM_NORMAL, routed_runtime=routed_runtime)

    assert routed
    assert kwargs["reasoning_config"] == parse_reasoning_effort("medium")
    assert _wire_tiers(kwargs) <= {None}, kwargs.get("request_overrides")
