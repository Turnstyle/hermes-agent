"""/review consumes ``auxiliary.review`` reasoning effort and speed tier, independent of the primary.

Real ``start_review`` -> ``delegate_task`` -> route/credential resolution; only child construction and the
child run are stubbed, so the assertions read what the reviewer child was actually handed.
"""

import json
import time
from unittest.mock import MagicMock

import pytest

from agent.review_engine import start_review
from hermes_constants import parse_reasoning_effort
from tools import async_delegation as ad
from tools.process_registry import process_registry

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_MSGS = [
    {"role": "user", "content": "ship the fix"},
    {"role": "assistant", "content": "PR #12 opened"},
]


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _high_fast_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "review-aux-parent"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    parent.model, parent.provider = "gpt-6.1-sol", "custom"
    parent.base_url, parent.api_key, parent.api_mode = "http://127.0.0.1:8318/v1", "k", "chat_completions"
    parent.reasoning_config = {"enabled": True, "effort": "high"}
    parent.request_overrides = {"service_tier": "priority"}
    parent.service_tier = "priority"
    return parent


def _review_built(monkeypatch, review_cfg):
    import tools.delegate_tool as dt

    built = {}
    child = MagicMock()
    child._delegate_role = "leaf"
    child.model = "gpt-6.1-sol"  # the dispatch ledger JSON-serializes what the child runs on

    def fake_build(**kw):
        built.update(kw)
        return child

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"auxiliary": {"review": review_cfg}})
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dt, "_build_child_agent", fake_build)
    monkeypatch.setattr(dt, "_run_single_child", lambda *a, **k: {
        "task_index": 0, "status": "completed", "summary": "ok", "api_calls": 1,
        "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"})
    parent = _high_fast_parent()
    result = start_review(parent, _MSGS, "")
    assert result["status"] == "dispatched", json.dumps(result, default=str)
    return built, parent


def _child_reasoning(built, parent):
    from tools.delegate_tool_config import _child_reasoning
    return _child_reasoning(parent, {}, built.get("override_reasoning_effort"))


def _child_tier(built):
    overrides = built.get("override_request_overrides") or {}
    return built.get("override_service_tier"), overrides.get("service_tier"), (overrides.get("extra_body") or {}).get("service_tier")


def test_review_slot_medium_normal_beats_high_fast_primary_without_route_pin(monkeypatch):
    built, parent = _review_built(monkeypatch, {
        "provider": "auto", "model": "", "reasoning_effort": "medium", "extra_body": {"service_tier": "default"}})

    assert _child_reasoning(built, parent) == parse_reasoning_effort("medium")
    assert _child_tier(built) == (None, None, None)
    assert parent.request_overrides == {"service_tier": "priority"}  # the parent is untouched


def test_review_slot_controls_apply_on_a_pinned_route(monkeypatch):
    built, parent = _review_built(monkeypatch, {
        "base_url": "http://127.0.0.1:8318/v1", "api_key": "k", "model": "gpt-6.1-sol",
        "reasoning_effort": "medium", "extra_body": {"service_tier": "default"}})

    assert built["override_base_url"] == "http://127.0.0.1:8318/v1"
    assert _child_reasoning(built, parent) == parse_reasoning_effort("medium")
    assert _child_tier(built) == (None, None, None)


def test_review_slot_explicit_disabled_reasoning_is_kept(monkeypatch):
    built, parent = _review_built(monkeypatch, {"provider": "auto", "reasoning_effort": False})

    assert _child_reasoning(built, parent) == {"enabled": False}


def test_review_slot_unset_controls_keep_inheriting_the_parent(monkeypatch):
    built, parent = _review_built(monkeypatch, {"provider": "auto", "model": "", "reasoning_effort": ""})

    assert built.get("override_reasoning_effort") is None
    assert _child_reasoning(built, parent) == {"enabled": True, "effort": "high"}
    assert (built.get("override_request_overrides") or parent.request_overrides).get("service_tier") == "priority"
