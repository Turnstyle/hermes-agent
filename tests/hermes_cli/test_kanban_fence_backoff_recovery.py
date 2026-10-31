"""Reclaim-path fleet invariants on the owner-runtime dispatcher.

A Fleet lifecycle fence refusing one row's release skips only that row (recorded in
``errors_out``) and never lets a reclaim path signal the worker it may not release; a worker
that survives termination keeps its claim; a live worker with a stale heartbeat is an
advisory, not a kill; another node's claim is out of scope. Heartbeats never let a stale run
touch its successor, and only a renewing worker heartbeat extends the lease.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_fence as fence

_FENCE = "verified run generation no longer owns lifecycle write"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    fence._fence_logged.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _running(conn, title, *, pid=4242, lock=None, max_runtime=None, age=100):
    tid = kb.create_task(conn, title=title, assignee="worker")
    lock = lock or f"{kb._host_prefix()}:w{pid}"
    assert kb.claim_task(conn, tid, claimer=lock) is not None
    run_id = kb.get_task(conn, tid).current_run_id
    past = int(time.time()) - age
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?, worker_started_at='fp', started_at=?, "
            "max_runtime_seconds=?, last_heartbeat_at=NULL WHERE id=?",
            (pid, past, max_runtime, tid))
        conn.execute("UPDATE task_runs SET started_at=?, worker_pid=?, worker_started_at='fp' WHERE id=?",
                     (past, pid, run_id))
    return tid


def _fence_row(conn, tid, message=_FENCE):
    conn.execute(
        f"CREATE TRIGGER fence_{tid} BEFORE UPDATE OF status ON tasks "
        f"WHEN OLD.id = '{tid}' AND OLD.status = 'running' "
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END")
    conn.commit()


def _status(conn, tid):
    return kb.get_task(conn, tid).status


def _events(conn, tid, kind):
    return [json.loads(r[0] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _live_unkillable(monkeypatch, signals):
    """A host-local worker that is alive, verified and ignores every signal."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started_at: False)
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: True)
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at=None: False)
    return lambda pid, sig: signals.append((pid, sig))


def test_timeout_fence_refusal_never_signals_and_spares_siblings(board, monkeypatch):
    fenced = _running(board, "fenced", pid=4242, max_runtime=1)
    sibling = _running(board, "sibling", pid=4343, max_runtime=1)
    _fence_row(board, fenced)
    signals: list = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started_at: False)
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: pid == 4242)
    errors: list = []

    timed_out = kbd.enforce_max_runtime(board, signal_fn=lambda pid, sig: signals.append((pid, sig)),
                                        errors_out=errors)

    assert timed_out == [sibling]
    assert [pid for pid, _sig in signals] == [4343]  # the fenced worker was never signalled
    assert _status(board, fenced) == "running"
    assert errors == [(fenced, f"enforce_max_runtime: {_FENCE}")]


def test_timeout_worker_surviving_sigkill_keeps_its_claim(board, monkeypatch):
    tid = _running(board, "unkillable", max_runtime=1)
    before = kb.get_task(board, tid)
    signals: list = []

    assert kbd.enforce_max_runtime(board, signal_fn=_live_unkillable(monkeypatch, signals)) == []

    after = kb.get_task(board, tid)
    assert after.status == "running" and after.claim_lock == before.claim_lock
    assert len(signals) == 2  # SIGTERM then SIGKILL, and still alive
    deferred = _events(board, tid, "reclaim_deferred")
    assert deferred and deferred[-1]["reason"] == "max_runtime_worker_alive"
    assert not _events(board, tid, "timed_out")


def test_stale_live_worker_gets_a_degree_warning_not_a_kill(board, monkeypatch):
    tid = _running(board, "slow but alive", age=7200)
    signals: list = []
    signal_fn = _live_unkillable(monkeypatch, signals)

    assert kbd.detect_stale_running(board, stale_timeout_seconds=60, signal_fn=signal_fn) == []

    assert signals == []
    assert _status(board, tid) == "running"
    assert [p["reason"] for p in _events(board, tid, "degree_warning")] == ["stale_heartbeat_live_owner"]


def test_stale_claim_from_another_node_is_out_of_scope(board, monkeypatch):
    tid = _running(board, "foreign claim", lock="other-node:w1", age=7200)
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: False)
    errors: list = []

    assert kbd.detect_stale_running(board, stale_timeout_seconds=60, errors_out=errors) == []
    assert _status(board, tid) == "running" and errors == []


def test_stale_dead_worker_fence_refusal_is_isolated(board, monkeypatch):
    fenced = _running(board, "fenced stale", pid=4242, age=7200)
    sibling = _running(board, "stale sibling", pid=4343, age=7200)
    _fence_row(board, fenced)
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: False)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    errors: list = []

    reclaimed = kbd.detect_stale_running(board, stale_timeout_seconds=60, signal_fn=lambda *a: None,
                                         errors_out=errors)

    assert reclaimed == [sibling]
    assert _status(board, fenced) == "running"
    assert [tid for tid, _msg in errors] == [fenced]


def test_crash_sweep_isolates_a_fenced_row_and_releases_its_sibling(board, monkeypatch):
    fenced = _running(board, "fenced crash", pid=4242)
    sibling = _running(board, "crashed sibling", pid=4343)
    _fence_row(board, fenced)
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: False)
    errors: list = []

    crashed = kbd.detect_crashed_workers(board, errors_out=errors)

    assert crashed == [sibling]
    assert _status(board, fenced) == "running"
    assert _status(board, sibling) != "running"
    assert errors == [(fenced, f"detect_crashed_workers: {_FENCE}")]


def test_unknown_constraint_still_aborts_the_crash_sweep(board, monkeypatch):
    tid = _running(board, "corrupt", pid=4242)
    _fence_row(board, tid, message="something else entirely")
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: False)

    with pytest.raises(sqlite3.IntegrityError, match="something else entirely"):
        kbd.detect_crashed_workers(board, errors_out=[])
    assert _status(board, tid) == "running"


def test_reconcile_orphan_fence_refusal_is_isolated(board):
    fenced = kb.create_task(board, title="fenced orphan", assignee="worker")
    sibling = kb.create_task(board, title="orphan sibling", assignee="worker")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='running', claim_lock=NULL, claim_expires=NULL "
                      "WHERE id IN (?, ?)", (fenced, sibling))
    _fence_row(board, fenced)
    errors: list = []

    assert kbd.reconcile_orphaned_running(board, errors_out=errors) == [sibling]
    assert _status(board, fenced) == "running"
    assert [tid for tid, _msg in errors] == [fenced]


def _heartbeat_card(conn):
    tid = kb.create_task(conn, title="heartbeat", assignee="worker")
    lock = f"{kb._host_prefix()}:w1"
    kb.claim_task(conn, tid, claimer=lock)
    return tid, lock, kb.get_task(conn, tid).current_run_id


def _lease(conn, tid):
    return conn.execute("SELECT claim_expires, last_heartbeat_at FROM tasks WHERE id=?", (tid,)).fetchone()


def test_stale_run_heartbeat_never_touches_its_successor(board):
    tid, lock, stale_run = _heartbeat_card(board)
    kb.reclaim_task(board, tid)
    kb.claim_task(board, tid, claimer=lock)
    before = _lease(board, tid)

    assert kbd.heartbeat_worker(board, tid, expected_run_id=stale_run, extend_claim=True, claimer=lock) is False
    assert tuple(_lease(board, tid)) == tuple(before)


def test_renewing_heartbeat_extends_the_claim_with_the_heartbeat(board):
    tid, lock, run_id = _heartbeat_card(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
    warnings: list = []

    assert kbd.heartbeat_worker(board, tid, expected_run_id=run_id, extend_claim=True, claimer=lock,
                                warnings_out=warnings) is True
    assert _lease(board, tid)["claim_expires"] > int(time.time())
    assert warnings == []


def test_heartbeat_from_another_claimer_is_refused_before_any_write(board):
    tid, _lock, run_id = _heartbeat_card(board)
    before = _lease(board, tid)
    events = len(_events(board, tid, "heartbeat"))

    assert kbd.heartbeat_worker(board, tid, expected_run_id=run_id, extend_claim=True,
                                claimer="someone-else:w9") is False
    assert tuple(_lease(board, tid)) == tuple(before)
    assert len(_events(board, tid, "heartbeat")) == events


def test_plain_heartbeat_records_liveness_but_never_renews_the_lease(board):
    tid, _lock, run_id = _heartbeat_card(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))

    assert kbd.heartbeat_worker(board, tid, expected_run_id=run_id, note="cli") is True
    row = _lease(board, tid)
    assert row["claim_expires"] == 1 and row["last_heartbeat_at"] is not None
