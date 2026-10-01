"""Tests for H23: delegated children/helpers default to high when parent is xhigh/max/ultra."""

from types import SimpleNamespace
import pytest

from tools.delegate_tool_config import _resolve_child_runtime


def _make_parent(reasoning_config=None, **kwargs):
    defaults = {
        "model": "test-model",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_mode": "chat_completions",
        "acp_args": [],
        "reasoning_config": reasoning_config,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _call_resolve(parent, delegation_cfg):
    return _resolve_child_runtime(
        parent,
        delegation_cfg,
        "test-key",
        model=None,
        override_provider=None,
        override_base_url=None,
        override_api_key=None,
        override_api_mode=None,
        override_acp_command=None,
        override_acp_args=None,
    )


@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_parent_xhigh_max_ultra_resolves_to_high_when_no_child_setting(effort):
    """Parent on xhigh/max/ultra caps to high when no child effort is configured."""
    parent = _make_parent(reasoning_config={"enabled": True, "effort": effort})
    result = _call_resolve(parent, {})
    assert result["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_parent_high_stays_high():
    """Parent on high stays high when no child effort is configured."""
    parent = _make_parent(reasoning_config={"enabled": True, "effort": "high"})
    result = _call_resolve(parent, {})
    assert result["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_parent_medium_stays_medium():
    """Parent on medium stays medium when no child effort is configured."""
    parent = _make_parent(reasoning_config={"enabled": True, "effort": "medium"})
    result = _call_resolve(parent, {})
    assert result["reasoning_config"] == {"enabled": True, "effort": "medium"}


def test_parent_disabled_stays_disabled():
    """Parent with reasoning disabled stays disabled."""
    parent = _make_parent(reasoning_config={"enabled": False})
    result = _call_resolve(parent, {})
    assert result["reasoning_config"] == {"enabled": False}


@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_explicit_child_effort_wins_over_cap(effort):
    """Explicit child setting preserves xhigh/max/ultra."""
    parent = _make_parent(reasoning_config={"enabled": True, "effort": "medium"})
    result = _call_resolve(parent, {"reasoning_effort": effort})
    assert result["reasoning_config"] == {"enabled": True, "effort": effort}


def test_explicit_child_xhigh_on_xhigh_parent_stays_xhigh():
    """Explicit child xhigh stays xhigh even when parent was xhigh."""
    parent = _make_parent(reasoning_config={"enabled": True, "effort": "xhigh"})
    result = _call_resolve(parent, {"reasoning_effort": "xhigh"})
    assert result["reasoning_config"] == {"enabled": True, "effort": "xhigh"}


def test_explicit_child_low_on_xhigh_parent():
    """Explicit child low applies when parent was xhigh."""
    parent = _make_parent(reasoning_config={"enabled": True, "effort": "xhigh"})
    result = _call_resolve(parent, {"reasoning_effort": "low"})
    assert result["reasoning_config"] == {"enabled": True, "effort": "low"}
