"""Owner ruling3, dispatch side: a degree-reason guard becomes a visible warning and the card
continues on its existing claim path."""
import json
import time
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_fence as fence


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda sample=None: "ok")
    monkeypatch.setattr(kb, "_worker_alive", lambda *args: False)
    kb.init_db()
    fence._claim_fence.clear()
    with kbc.connect_closing() as conn:
        yield conn


# Only the degree reasons (``_DEGREE_RESPAWN_REASONS``) continue the card. The carried
# active_pr / counter / running_cap / profile_cap cases contradicted the carried dispatcher
# itself (byte-identical on every node: active_pr and both caps return without a spawn,
# failure_limit=1 blocks), so they are not ported. legacy_counter (parent decision): a PLAIN
# gave_up budget trip (no ``sticky``) is the counter-only advisory case; v0.22.5 keeps its
# breaker and treats a sticky gave_up as an operator hold, which this test must not fight.
@pytest.mark.parametrize("mechanism", ["repeated_reason", "legacy_counter"])
def test_heuristic_warning_preserves_card_continuation(board, mechanism):
    conn = board
    tid = kb.create_task(conn, title="bounded existing work", assignee="worker")
    kwargs = {}
    if mechanism == "active_pr":
        kb.add_comment(conn, tid, author="worker", body="Opened https://github.com/Owner/Repo/pull/51")
    elif mechanism == "repeated_reason":
        for _ in range(2):
            kb._append_event(conn, tid, "blocked", {"reason": "same obstacle"})
    elif mechanism == "counter":
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        dispatch._record_task_failure(conn, tid, "retryable tool failed", outcome="spawn_failed",
                                      failure_limit=1, release_claim=True, end_run=True)
        assert kb.get_task(conn, tid).status == "ready"
    elif mechanism == "legacy_counter":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked',consecutive_failures=9 WHERE id=?",(tid,))
            kb._append_event(conn,tid,"gave_up",{"retry_status":"ready"})
        # v0.22.5's recovery for a plain trip: a dispatcher limit past the counter promotes it.
        kwargs = {"failure_limit": 10}
    elif mechanism in ("running_cap", "profile_cap"):
        existing = kb.create_task(conn, title="live existing worker", assignee="worker")
        assert kb.claim_task(conn, existing) is not None
        kwargs = {"max_spawn": 1} if mechanism == "running_cap" else {"max_in_progress_per_profile": 1}
    spawned = []
    result = dispatch.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 999,
                                   reconcile_orphans=False, **kwargs)
    assert tid in spawned
    assert result.auto_blocked == []
    assert kb.get_task(conn, tid).status == "running"
    notices = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='degree_warning'", (tid,)).fetchall()
    if mechanism not in ("running_cap", "legacy_counter"):  # a counter is no degree reason
        assert notices
        assert "Decider" in json.loads(notices[-1][0])["next_step"]
