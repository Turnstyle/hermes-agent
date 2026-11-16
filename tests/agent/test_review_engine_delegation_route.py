"""A policy-only ``auxiliary.review`` (effort / tier, no route pin) tunes the reviewer on the ``delegation`` route.

Setting ``auxiliary.review.reasoning_effort`` or ``extra_body.service_tier`` alone must not move ``/review`` off the
delegation backend, its ``delegation.*`` model/endpoint pin or its fallback chain (an explicit ``[]`` included) onto
the primary's. An explicit review route pin still owns the route. Real ``start_review`` -> ``delegate_task`` ->
backend routing / credential resolution; only child construction and the child run are stubbed.
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent.delegation_provider import DelegationProvider
from agent.review_engine import start_review
from hermes_constants import parse_reasoning_effort
from tools import async_delegation as ad
from tools.delegate_tool_config import _child_reasoning, _resolve_child_fallback_chain
from tools.process_registry import process_registry

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")

_MSGS = [
    {"role": "user", "content": "ship the fix"},
    {"role": "assistant", "content": "PR #12 opened"},
]
_PRIMARY_URL = "http://127.0.0.1:8318/v1"
_DELEGATION_URL = "http://127.0.0.1:9001/v1"
_PRIMARY_CHAIN = [{"provider": "openrouter", "model": "primary-fallback"}]
_MEDIUM_NORMAL = {"provider": "auto", "model": "", "reasoning_effort": "medium",
                  "extra_body": {"service_tier": "default"}}


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


def _parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "review-route-parent"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.model, parent.provider = "primary-model", "custom"
    parent.base_url, parent.api_key, parent.api_mode = _PRIMARY_URL, "pk", "chat_completions"
    parent.reasoning_config = {"enabled": True, "effort": "high"}
    parent.request_overrides = {"service_tier": "priority"}
    parent.service_tier = "priority"
    parent._fallback_chain = list(_PRIMARY_CHAIN)
    parent._delegation_backend = ""
    return parent


def _review(monkeypatch, review_cfg, delegation_cfg, parent=None):
    """What the reviewer child was handed: ``(build kwargs, parent)``."""
    import tools.delegate_tool as dt

    built = {}
    child = MagicMock()
    child._delegate_role = "leaf"
    child.model = "reviewer"  # the dispatch ledger JSON-serializes what the child runs on

    def fake_build(**kw):
        built.update(kw)
        return child

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"auxiliary": {"review": review_cfg}})
    monkeypatch.setattr(dt, "_load_config", lambda: dict(delegation_cfg))
    monkeypatch.setattr(dt, "_build_child_agent", fake_build)
    monkeypatch.setattr(dt, "_run_single_child", lambda *a, **k: {
        "task_index": 0, "status": "completed", "summary": "ok", "api_calls": 1,
        "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"})
    parent = parent or _parent()
    result = start_review(parent, _MSGS, "")
    assert result["status"] == "dispatched", json.dumps(result, default=str)
    return built, parent


def _fallback_chain(built, parent):
    """The chain the real child builder derives from what it was handed."""
    pinned = bool(built.get("override_provider") or built.get("override_base_url") or built.get("model"))
    return _resolve_child_fallback_chain(parent, built["routing_cfg"], pinned=pinned)


def _tier(built):
    overrides = built.get("override_request_overrides") or {}
    return built.get("override_service_tier"), overrides.get("service_tier")


def test_policy_only_review_keeps_the_delegation_route_and_its_disabled_fallback(monkeypatch):
    delegation = {"base_url": _DELEGATION_URL, "api_key": "dk", "model": "delegation-model",
                  "fallback_providers": []}
    built, parent = _review(monkeypatch, _MEDIUM_NORMAL, delegation)

    assert (built["model"], built["override_base_url"]) == ("delegation-model", _DELEGATION_URL)
    assert _fallback_chain(built, parent) is None  # delegation's explicit [] disables fallback, not the primary's chain
    assert _child_reasoning(parent, delegation, built["override_reasoning_effort"]) == parse_reasoning_effort("medium")
    assert _tier(built) == (None, None)


class _ReviewBackend(DelegationProvider):
    """A non-default delegation backend: records the config it routed on and answers one fixed route."""

    def __init__(self, route):
        self.route, self.configs = route, []

    @property
    def name(self):
        return "review-backend"

    def route_task(self, task, config):
        self.configs.append(dict(config))
        return dict(self.route)


def test_policy_only_review_routes_on_the_pinned_backend_with_disabled_reasoning(monkeypatch):
    backend_chain = [{"provider": "openrouter", "model": "backend-fallback"}]
    backend = _ReviewBackend({"base_url": _DELEGATION_URL, "api_key": "bk", "model": "backend-model",
                              "reasoning_effort": "low", "fallback_providers": backend_chain})
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda config: backend)
    parent = _parent()
    parent._delegation_backend = "review-backend"

    built, parent = _review(monkeypatch, {"provider": "auto", "reasoning_effort": False},
                            {"backend": "default", "model": "delegation-model"}, parent)

    assert [c.get("backend") for c in backend.configs] == ["review-backend"]
    assert (built["model"], built["override_base_url"]) == ("backend-model", _DELEGATION_URL)
    assert [entry["model"] for entry in _fallback_chain(built, parent)] == ["backend-fallback"]
    assert _child_reasoning(parent, {}, built["override_reasoning_effort"]) == {"enabled": False}


def test_explicit_review_route_pin_still_owns_the_route(monkeypatch):
    review = {"base_url": "http://127.0.0.1:9002/v1", "api_key": "rk", "model": "review-model",
              "reasoning_effort": "medium"}
    delegation = {"base_url": _DELEGATION_URL, "api_key": "dk", "model": "delegation-model",
                  "fallback_providers": [{"provider": "openrouter", "model": "delegation-fallback"}]}
    built, parent = _review(monkeypatch, review, delegation)

    assert (built["model"], built["override_base_url"]) == ("review-model", "http://127.0.0.1:9002/v1")
    assert _fallback_chain(built, parent) is None  # a pinned review route borrows no other block's chain
    assert _child_reasoning(parent, delegation, built["override_reasoning_effort"]) == parse_reasoning_effort("medium")


def test_policy_only_review_auto_effort_warns_and_keeps_the_delegation_effort(monkeypatch, caplog):
    delegation = {"model": "delegation-model", "reasoning_effort": "low"}
    with caplog.at_level("WARNING"):
        built, parent = _review(monkeypatch, {"provider": "auto", "reasoning_effort": "auto"}, delegation)

    assert built["model"] == "delegation-model"
    assert _child_reasoning(parent, delegation, built["override_reasoning_effort"]) == parse_reasoning_effort("low")
    assert "'auto' is not a valid level" in caplog.text
