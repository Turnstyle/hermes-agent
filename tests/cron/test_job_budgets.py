"""Scheduler-level coverage for per-job wake budgets and audit values."""

from types import SimpleNamespace

import pytest

from cron.scheduler_budget import (
    apply_job_budgets_to_agent,
    budget_audit_fields,
    is_cron_turn_limit_handoff,
    positive_job_budget,
    usage_audit_value,
)


@pytest.mark.parametrize("key", ["max_tokens", "max_turns"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, "4", 1.5, [], {}])
def test_job_budget_must_be_a_positive_integer(key, bad):
    with pytest.raises(ValueError, match=f"{key} must be a positive integer"):
        positive_job_budget({key: bad}, key)


def test_missing_and_positive_job_budgets_parse():
    assert positive_job_budget({}, "max_tokens") is None
    assert positive_job_budget({"max_tokens": 50_000}, "max_tokens") == 50_000
    assert positive_job_budget({"max_turns": 4}, "max_turns") == 4


def test_usage_audit_value_falls_back_to_live_agent_tally():
    agent = SimpleNamespace(session_total_tokens=12_600)

    assert usage_audit_value({}, "total_tokens", agent, "session_total_tokens") == 12_600
    assert usage_audit_value(
        {"total_tokens": 12_601}, "total_tokens", agent, "session_total_tokens"
    ) == 12_601
    assert usage_audit_value({}, "total_tokens", None, "session_total_tokens") is None


def test_apply_job_budgets_arms_agent_and_captures_default_output():
    agent = SimpleNamespace(max_tokens=16_000)

    apply_job_budgets_to_agent(agent, max_tokens=50_000, max_turns=4)

    assert agent._cron_max_tokens == 50_000
    assert agent._cron_max_turns == 4
    assert agent._cron_default_response_max_tokens == 16_000
    assert agent._cron_budget_stopped is False
    assert agent._cron_budget_stop_reason is None


@pytest.mark.parametrize("default_output", [None, 0, True, "8192"])
def test_non_positive_default_output_is_not_captured(default_output):
    agent = SimpleNamespace(max_tokens=default_output)

    apply_job_budgets_to_agent(agent, max_tokens=None, max_turns=None)

    assert agent._cron_default_response_max_tokens is None


def test_budget_audit_fields_record_job_limits_and_stop():
    agent = SimpleNamespace(_cron_budget_stopped=True, _cron_budget_stop_reason="max_tokens")

    assert budget_audit_fields({"max_tokens": 50_000}, agent) == {
        "max_tokens": 50_000,
        "max_turns": None,
        "budget_stopped": True,
        "budget_stop_reason": "max_tokens",
    }
    assert budget_audit_fields({}, None)["budget_stopped"] is False


def _result(**overrides):
    base = {
        "failed": False,
        "completed": False,
        "turn_exit_reason": "cron_max_turns_reached(4/4)",
        "final_response": "Cron wake stopped at its configured max_turns limit (4/4).",
    }
    base.update(overrides)
    return base


def test_cron_turn_limit_result_is_a_deliverable_handoff():
    assert is_cron_turn_limit_handoff(_result()) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"failed": True},
        {"interrupted": True},
        {"completed": True},
        {"turn_exit_reason": "max_iterations_reached(4/4)"},
        {"final_response": "  "},
    ],
)
def test_other_results_are_not_cron_turn_limit_handoffs(overrides):
    assert is_cron_turn_limit_handoff(_result(**overrides)) is False


def test_scheduler_delivers_cron_turn_limit_stop_instead_of_failing():
    from cron.scheduler import _final_response_from_result

    text = _final_response_from_result(_result(messages=[]), "job-1", "job", object)

    assert text == "Cron wake stopped at its configured max_turns limit (4/4)."


def test_scheduler_resolves_per_job_turn_limit_before_profile_default(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_load_prefill_messages", lambda *_a: None)
    monkeypatch.setattr(scheduler, "_guard_job_credential_exfil", lambda *_a: None)
    # Stop right after the budget block: preflight blocking returns early.
    monkeypatch.setattr(scheduler, "_preflight_or_block", lambda *_a: "blocked")
    jc = SimpleNamespace(cfg={"agent": {"max_turns": 90}}, model="m")

    setup = scheduler._resolve_cron_agent_setup(
        {"max_turns": 4, "max_tokens": 50_000}, "j", "j", jc)
    assert (setup.max_iterations, setup.job_max_turns, setup.job_max_tokens) == (4, 4, 50_000)

    setup = scheduler._resolve_cron_agent_setup({}, "j", "j", jc)
    assert (setup.max_iterations, setup.job_max_turns, setup.job_max_tokens) == (90, None, None)

    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        scheduler._resolve_cron_agent_setup({"max_tokens": "50000"}, "j", "j", jc)
