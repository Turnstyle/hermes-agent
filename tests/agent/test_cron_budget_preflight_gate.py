"""The pre-API gate stops a budgeted cron fire before its provider call."""

from types import SimpleNamespace

from agent.turn_preflight_gate import run_preflight_gate


class _Budget:
    def __init__(self):
        self.refunds = 0

    def refund(self):
        self.refunds += 1


def _gate(agent, messages, pressure, *, failed=False):
    return run_preflight_gate(
        agent, request_pressure_tokens=pressure, _moa_prepared_request=None,
        pending_moa_prepared_request=None, messages=messages, system_message=None,
        user_message="task", active_system_prompt="sys", conversation_history=[],
        api_call_count=3, compression_attempts=0, max_compression_attempts=3,
        effective_task_id="t", final_response=None, failed=failed,
        _turn_exit_reason="unknown", _compression_timeout_exhausted=False,
        _preflight_compression_blocked=False, _provider_overflow_recovery_pending=False,
        _last_preflight_pressure=None,
    )


def _over_budget_agent():
    return SimpleNamespace(
        _cron_max_tokens=50_000, _cron_default_response_max_tokens=16_000,
        _cron_budget_stopped=False, _cron_budget_stop_reason=None,
        session_total_tokens=45_000, max_tokens=16_000,
        iteration_budget=_Budget(), _api_call_count=3,
    )


def test_cron_fire_over_budget_breaks_before_provider_and_refunds_the_call():
    agent = _over_budget_agent()
    messages = [{"role": "user", "content": "task"}]

    v = _gate(agent, messages, 6_000)

    assert v.action == "break"
    assert v.failed is False
    assert v._turn_exit_reason == "cron_max_tokens_reached"
    assert v.final_response.startswith("Cron wake stopped at its configured max_tokens budget")
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", v.final_response)
    # The request that never went out is not counted against the turn.
    assert v.api_call_count == 2 and agent._api_call_count == 2
    assert agent.iteration_budget.refunds == 1
    assert agent._cron_budget_stop_reason == "max_tokens"


def test_budget_stop_is_a_deliverable_stop_even_if_the_turn_entered_failed():
    v = _gate(_over_budget_agent(), [{"role": "user", "content": "task"}], 6_000, failed=True)

    assert v.action == "break"
    assert v.failed is False
