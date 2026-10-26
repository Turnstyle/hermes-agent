"""Quiet one-shot completion pump (X06): a completion's delivery is booked only once its follow-up
turn was admitted. Upstream acknowledged every drained completion BEFORE running the follow-up, so
a follow-up the session owner refused lost the completion and replaced the run's answer."""
import pytest

import tools.async_delegation as async_delegation
from hermes_cli import quiet_single_query
from tools.process_registry import process_registry

EVENT = {"type": "async_delegation", "delegation_id": "d1"}


@pytest.fixture
def ledger(monkeypatch):
    calls = []
    queue = [[(EVENT, "child finished")]]
    monkeypatch.setattr(process_registry, "wait_for_pending_completions", lambda *_a, **_k: {"timed_out": False})
    monkeypatch.setattr(process_registry, "drain_notifications",
                        lambda **_k: queue.pop(0) if queue else [])
    monkeypatch.setattr(async_delegation, "claim_event_delivery", lambda evt, consumer: "claim-1")
    for verb in ("complete", "defer"):
        monkeypatch.setattr(async_delegation, f"{verb}_event_delivery",
                            lambda evt, claim, verb=verb: calls.append((verb, claim, list(order))))
    order = []
    return calls, order


def _run(run_turn):
    return quiet_single_query.continue_quiet_notify_completions("sid", run_turn, linger_budget=0.0)


def test_refused_follow_up_defers_completion_and_keeps_answer(ledger):
    calls, order = ledger

    def refused(text):
        order.append("turn")
        return {"failed": True, "failure_reason": "session_busy", "error": "session_busy"}

    assert _run(refused) is None  # the refused wake never becomes the run's answer
    assert calls == [("defer", "claim-1", ["turn"])]


def test_admitted_follow_up_completes_after_the_turn(ledger):
    calls, order = ledger

    def admitted(text):
        order.append("turn")
        return {"final_response": "teammate reply", "failed": False}

    assert _run(admitted)["final_response"] == "teammate reply"
    assert calls == [("complete", "claim-1", ["turn"])]


@pytest.mark.parametrize("outcome", [
    {"failed": True, "failure_reason": "gateway_connection", "error": "lost"},  # may have been admitted
    RuntimeError("controlled"),
])
def test_ambiguous_follow_up_never_reinjects(ledger, outcome):
    calls, _order = ledger

    def ambiguous(text):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    if isinstance(outcome, Exception):
        with pytest.raises(RuntimeError):
            _run(ambiguous)
    else:
        _run(ambiguous)
    assert [verb for verb, *_ in calls] == ["complete"]
