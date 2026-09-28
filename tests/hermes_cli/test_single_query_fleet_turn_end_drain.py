"""``-Q`` turn exit runs the fleet turn-end query when drain is enabled."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hermes_cli.cli_single_query as csq


def test_single_query_turn_end_invokes_fleet_drain_when_enabled(monkeypatch):
    cli = MagicMock(session_id="sid", agent=MagicMock(), conversation_history=[])
    queries: list = []
    mark_calls: list = []

    monkeypatch.setattr(
        "tools.fleet_message_drain.turn_end_drain_query",
        lambda home, cfg=None: queries.append(home) or None,
    )
    monkeypatch.setattr("tools.fleet_message_drain.mark_read", lambda *a, **k: mark_calls.append(True))

    csq._run_single_query_fleet_turn_end_drain(cli)
    assert len(queries) == 1
    assert not mark_calls

    queries.clear()
    claimed = MagicMock()
    monkeypatch.setattr(
        "tools.fleet_message_drain.turn_end_drain_query",
        lambda home, cfg=None: queries.append(home) or (MagicMock(max_attempts=5), MagicMock(), claimed),
    )
    monkeypatch.setattr("tools.fleet_message_drain.mark_read", lambda *a, **k: mark_calls.append(True) or False)

    csq._run_single_query_fleet_turn_end_drain(cli)
    assert len(queries) == 1
    assert mark_calls


def _quiet_run_drains(monkeypatch, *, reclaim_ok: bool) -> list:
    """One quiet Bot Chat turn whose post-linger lease re-claim succeeds or fails; returns drain calls."""
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.setattr("hermes_cli.quiet_single_query.continue_quiet_notify_completions",
                        lambda *a, **k: None)
    drains = []
    monkeypatch.setattr(csq, "_drain_quiet_bot_chat", lambda cli, history: drains.append(history))
    agent = SimpleNamespace(session_id="s-1",
                            run_conversation=lambda **kw: {"final_response": "ok", "messages": []})
    released = []
    fake_cli = SimpleNamespace(
        agent=agent, conversation_history=[], session_id="s-1",
        _release_active_session=lambda: released.append(True),
        _claim_active_session=lambda *a, **k: reclaim_ok,
    )
    with pytest.raises(SystemExit):
        csq._run_quiet_single_query(fake_cli, "hello")
    assert released, "the idle linger must release the lease first"
    return drains


def test_quiet_turn_end_drain_skipped_when_the_lease_is_not_reclaimed(monkeypatch):
    """Another writer took the chat during the linger: this process's history is stale, so the
    queued fleet messages stay for that owner's own turn-end drain."""
    assert _quiet_run_drains(monkeypatch, reclaim_ok=False) == []
    assert len(_quiet_run_drains(monkeypatch, reclaim_ok=True)) == 1
