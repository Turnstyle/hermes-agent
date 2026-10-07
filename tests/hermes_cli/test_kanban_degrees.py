"""Owner ruling3: warnings carry recoverable work while native writer ownership remains authoritative."""
import json
import time
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


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
    dispatch._claim_fence.clear()
    with kbc.connect_closing() as conn:
        yield conn


@pytest.mark.parametrize("mechanism", ["active_pr", "repeated_reason", "counter", "legacy_counter", "running_cap", "profile_cap"])
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
            kb._append_event(conn,tid,"gave_up",{"sticky":True,"retry_status":"ready"})
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
    if mechanism != "running_cap":
        assert notices
        assert "Decider" in json.loads(notices[-1][0])["next_step"]


@pytest.mark.parametrize("mechanism", ["lease", "lease_recovered", "stale_live", "stale_dead", "cooldown"])
def test_native_ownership_recovery_is_bounded_and_visible(board, mechanism, monkeypatch):
    conn = board
    tid = kb.create_task(conn, title="ownership bounded work", assignee="worker")
    if mechanism in ("lease", "lease_recovered"):
        conn.execute("CREATE TRIGGER fixture_lease BEFORE UPDATE OF status ON tasks WHEN NEW.status='running' "
                     "BEGIN SELECT RAISE(ABORT,'verified execution lease required before running'); END")
        conn.commit()
        for _ in range(4):
            result = dispatch.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: pytest.fail("lease bypassed"),
                                            reconcile_orphans=False)
        assert kb.get_task(conn, tid).status == "ready"
        warnings = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='degree_warning'", (tid,)).fetchall()
        assert len(warnings) == 2  # execution lease, then its bounded retry notice
        assert "sync/acquire" in json.loads(warnings[0][0])["next_step"]
        assert dispatch.check_respawn_guard(conn, tid) == "execution_lease_retry"
        if mechanism == "lease_recovered":
            conn.execute("CREATE TABLE fleet_kanban_issue_map(local_task_id TEXT,issue_id TEXT,current_node TEXT,source_node TEXT)")
            conn.execute("CREATE TABLE fleet_kanban_verified_leases(issue_id TEXT,holder_profile TEXT,holder_node TEXT,expires_at INTEGER)")
            conn.execute("INSERT INTO fleet_kanban_issue_map VALUES (?,'issue','local','local')", (tid,))
            conn.execute("INSERT INTO fleet_kanban_verified_leases VALUES ('issue','worker','local',?)", (int(time.time())+60,))
            conn.commit()
            assert dispatch.check_respawn_guard(conn, tid) is None
    elif mechanism in ("stale_live", "stale_dead"):
        assert kb.claim_task(conn, tid) is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=123, worker_started_at=1, claim_expires=0,last_heartbeat_at=0 WHERE id=?", (tid,))
        monkeypatch.setattr(kb, "_worker_alive", lambda *args: mechanism == "stale_live")
        signals=[]
        reclaimed=kb.release_stale_claims(conn, signal_fn=lambda *args: signals.append(args))
        assert reclaimed == (0 if mechanism == "stale_live" else 1)
        if mechanism == "stale_live":
            assert not signals
            assert kb.get_task(conn, tid).status == "running"
            notice=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='degree_warning'", (tid,)).fetchone()
            assert "Decider" in json.loads(notice[0])["next_step"]
        else:
            assert kb.get_task(conn, tid).status == "ready"
    else:
        assert kb.claim_task(conn, tid) is not None
        kb._end_run(conn, tid, outcome="rate_limited",status="rate_limited",error="quota",metadata={})
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL,claim_expires=NULL WHERE id=?", (tid,))
            conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=?", (int(time.time())-601,tid))
        monkeypatch.setattr(kb,"_resolve_rate_limit_cooldown_seconds",lambda:600)
        assert dispatch.check_respawn_guard(conn,tid) is None
