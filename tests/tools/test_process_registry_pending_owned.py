"""A Bot Chat follow-up only takes the turn locks for an owned pending notification."""

from tools.process_registry import ProcessRegistry


def test_pending_owned_notifications_matches_drain_ownership_without_popping():
    registry = ProcessRegistry()
    owned = {"type": "completion", "session_id": "proc-owned", "session_key": "s-1", "command": "echo ok",
             "exit_code": 0, "output": "done"}
    foreign = {**owned, "session_id": "proc-foreign", "session_key": "s-2"}
    observed = {**owned, "session_id": "proc-observed"}
    registry._poll_observed.add("proc-observed")
    for event in (owned, foreign, observed):
        registry.completion_queue.put(event)

    assert registry.pending_owned_notifications(session_key="s-1") == [owned]
    assert registry.completion_queue.qsize() == 3
    assert [event for event, _text in registry.drain_notifications(session_key="s-1")] == [owned]
    assert registry.pending_owned_notifications(session_key="s-2") == [foreign]


def test_pending_owned_notifications_uses_callback_and_fails_closed():
    registry = ProcessRegistry()
    event = {"type": "completion", "session_id": "proc-1", "session_key": "s-1"}
    registry.completion_queue.put(event)
    assert registry.pending_owned_notifications(session_key="s-1", owns_event=lambda _event: False) == []
    assert registry.pending_owned_notifications(session_key="s-1", owns_event=lambda _event: 1 / 0) == []
    assert registry.pending_owned_notifications(session_key="s-1", owns_event=lambda _event: True) == [event]
    assert registry.completion_queue.qsize() == 1
