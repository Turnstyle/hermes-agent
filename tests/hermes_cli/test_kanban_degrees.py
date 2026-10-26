"""Owner ruling3: warnings carry recoverable work while native writer ownership remains authoritative."""
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


@pytest.mark.parametrize("mechanism", ["lease", "lease_recovered", pytest.param(
    "stale_live", marks=pytest.mark.xfail(strict=True, reason=(
        "a live owner's expired claim must get a degree warning, not a reclaim: needs the "
        "kanban_db.release_stale_claims live-owner advisory (K04, worker k-db)"))), "stale_dead", pytest.param(
    "cooldown", marks=pytest.mark.xfail(strict=True, reason=(
        "model-chain Decider hand-back after a cooldown needs kanban_db._handoff_to_decider "
        "(K04, worker k-db); not ported in the respawn guard yet")))])
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
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        assert dispatch.check_respawn_guard(conn,tid) == "model_chain_handoff"
        handed = kb.get_task(conn, tid)
        assert handed.status == "triage"
        assert handed.assignee == "worker"  # legacy board retains its registered local owner
        assert handed.model_override is None and handed.provider_override is None  # handoff never selects a provider
        assert dispatch.check_respawn_guard(conn,tid) is None
        assert dispatch.check_respawn_guard(conn,tid) is None
        events = conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='model_chain_handoff'", (tid,)).fetchall()
        assert [event[0] for event in events] == [run_id]
