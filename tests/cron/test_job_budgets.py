"""Scheduler-level coverage for per-job wake budgets and audit values."""

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
    assert (
        scheduler._positive_job_budget({"max_tokens": 50_000}, "max_tokens")
        == 50_000
    )
    assert scheduler._positive_job_budget({"max_turns": 4}, "max_turns") == 4


def test_usage_audit_value_falls_back_to_live_agent_tally():
    agent = SimpleNamespace(session_total_tokens=12_600)

    assert (
        scheduler._usage_audit_value(
            {}, "total_tokens", agent, "session_total_tokens"
        )
        == 12_600
    )
    assert (
        scheduler._usage_audit_value(
            {"total_tokens": 12_601},
            "total_tokens",
            agent,
            "session_total_tokens",
        )
        == 12_601
    )
