"""The curator review fork consumes ``auxiliary.curator`` reasoning effort and speed tier, independent of the
primary's high/fast settings; an unset slot keeps today's primary-derived behaviour."""

import importlib
from pathlib import Path

import pytest

from hermes_constants import parse_reasoning_effort

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")


@pytest.fixture
def curator_fork(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent import curator
    importlib.reload(curator)
    captured = {}

    class _StubAgent:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self._memory_write_origin = "assistant_tool"
            self._memory_nudge_interval = 0
            self._skill_nudge_interval = 0
            self._session_messages = []

        def run_conversation(self, user_message=None, **kwargs):
            return {"final_response": "ok"}

        def close(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", _StubAgent)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {"provider": "custom", "api_key": "k", "base_url": "http://127.0.0.1:8318/v1",
                          "api_mode": "chat_completions", "request_overrides": {"service_tier": "priority"}},
    )

    def run(curator_slot):
        cfg = {"model": {"provider": "custom", "default": "gpt-6.1-sol"},
               "agent": {"reasoning_effort": "high"}, "auxiliary": {"curator": curator_slot}}
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
        meta = curator._run_llm_review("review prompt")
        assert meta.get("error") is None, meta.get("error")
        return captured

    return run


def _tiers(overrides):
    overrides = overrides or {}
    return overrides.get("service_tier"), (overrides.get("extra_body") or {}).get("service_tier")


@pytest.mark.parametrize("slot", [
    {"provider": "auto", "model": "", "reasoning_effort": "medium", "extra_body": {"service_tier": "default"}},
    {"provider": "custom", "model": "gpt-6.1-sol", "reasoning_effort": "medium",
     "extra_body": {"service_tier": "default"}},
])
def test_curator_slot_medium_normal_beats_high_fast_primary(curator_fork, slot):
    kwargs = curator_fork(slot)

    assert kwargs["reasoning_config"] == parse_reasoning_effort("medium")
    assert _tiers(kwargs["request_overrides"]) == (None, "default")


def test_curator_slot_explicit_disabled_reasoning_is_kept(curator_fork):
    kwargs = curator_fork({"provider": "auto", "reasoning_effort": "none"})

    assert kwargs["reasoning_config"] == {"enabled": False}


def test_curator_unset_slot_keeps_primary_reasoning(curator_fork):
    kwargs = curator_fork({"provider": "auto", "model": "", "reasoning_effort": ""})

    assert kwargs["reasoning_config"] == parse_reasoning_effort("high")
