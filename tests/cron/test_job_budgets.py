"""Scheduler-level coverage for per-job wake budgets and their audit receipt."""

from types import SimpleNamespace

import pytest

from cron import scheduler


@pytest.mark.parametrize("key", ["max_tokens", "max_turns"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, "4", 1.5, [], {}])
def test_job_budget_must_be_a_positive_integer(key, bad):
    with pytest.raises(ValueError, match=f"{key} must be a positive integer"):
        scheduler._positive_job_budget({key: bad}, key)


def test_missing_and_positive_job_budgets_parse():
    assert scheduler._positive_job_budget({}, "max_tokens") is None
    assert scheduler._positive_job_budget({"max_tokens": 50_000}, "max_tokens") == 50_000
    assert scheduler._positive_job_budget({"max_turns": 4}, "max_turns") == 4


def test_constructed_agent_carries_scheduler_owned_limits(monkeypatch):
    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.max_tokens = 8192

    monkeypatch.setattr(scheduler, "_resolve_cron_enabled_toolsets", lambda *_a: [])
    monkeypatch.setattr(scheduler, "_resolve_cron_disabled_toolsets", lambda *_a: [])
    setup = scheduler._CronAgentSetup(
        model="main-model",
        runtime={"api_key": "x", "base_url": "https://invalid", "provider": "test"},
        max_iterations=4,
        job_max_turns=4,
        job_max_tokens=50_000,
    )

    agent = scheduler._construct_cron_agent(
        FakeAgent, {"id": "wake"}, {}, setup,
        workdir=None, session_id="cron_wake_1", session_db=None,
    )

    assert agent.kwargs["max_iterations"] == 4
    assert agent._cron_max_turns == 4
    assert agent._cron_max_tokens == 50_000
    assert agent._cron_default_response_max_tokens == 8192
    assert agent._cron_budget_stopped is False


def test_fire_audit_records_budget_and_provider_usage(monkeypatch):
    rows = []
    monkeypatch.setattr(scheduler, "_write_usage_audit", rows.append)
    audit = scheduler._FireAudit(
        {"id": "wake", "max_tokens": 50_000, "max_turns": 4, "deliver": None},
        "wake",
        "main-model",
    )
    agent = SimpleNamespace(
        session_prompt_tokens=12_000,
        session_completion_tokens=600,
        session_total_tokens=12_600,
        _cron_budget_stopped=True,
        _cron_budget_stop_reason="max_turns",
    )

    audit.write({}, None, agent)

    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 12_000
    assert rows[0]["completion_tokens"] == 600
    assert rows[0]["total_tokens"] == 12_600
    assert rows[0]["max_tokens"] == 50_000
    assert rows[0]["max_turns"] == 4
    assert rows[0]["budget_stopped"] is True
    assert rows[0]["budget_stop_reason"] == "max_turns"
