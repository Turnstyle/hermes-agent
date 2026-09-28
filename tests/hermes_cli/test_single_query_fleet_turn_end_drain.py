"""``-Q`` turn exit runs the fleet turn-end query when drain is enabled."""

from __future__ import annotations

from unittest.mock import MagicMock

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
