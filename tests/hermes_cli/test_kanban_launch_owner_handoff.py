"""K07 closing proof: a failure between ``Popen`` and pid persistence, reconciled by owner receipts.

The dispatcher's waiter (``kanban_worker_client``) can start and then lose its pid write. The
pre-spawn ``worker_handoff_pending`` intent keeps the claim (no release beside a live waiter, no
second launch) until the owner's receipt settles it; ``owner_reclaim_blocked`` then governs the
run exactly as upstream wires it. An intent nobody answers lapses with its claim, and a late
admission of that run is refused by the owner's own claim check.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_launch
from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked
from hermes_cli.process_identity import process_birth


class _Waiter:
    pid = os.getpid()  # a live pid: anything that kills by pid would hit the test, so nothing may


@pytest.fixture
def board(tmp_path, monkeypatch):
    kb.init_db()
    launched = []
    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        # Only the worker launch is faked: liveness probes (macOS ``ps``) still run for real.
        if not any("kanban_worker_client" in str(part) for part in cmd):
            return real_popen(cmd, *args, **kwargs)
        launched.append(list(cmd))
        return _Waiter()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with kbc.connect_closing() as conn:
        yield SimpleNamespace(conn=conn, launched=launched, workspace=str(workspace), tmp=tmp_path)


def _claimed(conn, title="handoff"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    return kb.claim_task(conn, tid)


def _fail_pid_write(monkeypatch):
    def fail(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(kbd, "_set_worker_pid", fail)


def _spawn(b, claimed):
    return kanban_launch.spawn_with_handoff(
        b.conn, claimed, lambda: kbd._call_spawn_fn(kbd._default_spawn, claimed, b.workspace, None),
        durable=True)


def _owner_admits(b, claimed, status="queued"):
    """The owner's ``admit_claim`` marker plus its admission ledger row (session_admissions)."""
    ledger = b.tmp / "state.db"
    request_id = "kanban:" + json.dumps([kb.get_current_board(), claimed.id, claimed.current_run_id],
                                        separators=(",", ":"))
    with sqlite3.connect(ledger) as store:
        store.execute("CREATE TABLE IF NOT EXISTS session_admissions "
                      "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
        store.execute("INSERT INTO session_admissions VALUES ('s1', ?, ?, NULL)", (request_id, status))
    with kb.write_txn(b.conn):
        kb._append_event(b.conn, claimed.id, "owner_admitted", {
            "db": str(ledger), "session_id": "s1", "request_id": request_id,
            "pid": os.getpid(), "birth": process_birth(psutil.Process())}, run_id=claimed.current_run_id)


def _expire_claim(conn, tid):
    conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 60, tid))
    conn.commit()


def _row(conn, tid):
    return conn.execute("SELECT status, claim_lock, current_run_id, worker_pid, consecutive_failures "
                        "FROM tasks WHERE id = ?", (tid,)).fetchone()


def test_pid_write_failure_keeps_the_claim_until_the_owner_receipt_settles_it(board, monkeypatch):
    b = board
    claimed = _claimed(b.conn)
    _fail_pid_write(monkeypatch)
    with pytest.raises(kanban_launch.WorkerHandoffUncertain):
        _spawn(b, claimed)
    assert len(b.launched) == 1
    kinds = [r[0] for r in b.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND run_id = ? ORDER BY id", (claimed.id, claimed.current_run_id))]
    assert "worker_handoff_pending" in kinds and "spawned" not in kinds
    assert tuple(_row(b.conn, claimed.id)) == ("running", claimed.claim_lock, claimed.current_run_id, None, 0)
    assert kanban_launch.handoff_pending(b.conn, claimed.id)
    # A second launch of the same claim is refused while the intent stands.
    with pytest.raises(kanban_launch.WorkerHandoffUncertain):
        _spawn(b, claimed)
    assert len(b.launched) == 1
    # Later ticks neither respawn nor reconcile the card: it is running under its claim.
    for _ in range(2):
        kbd.dispatch_once(b.conn)
    assert kbd.reconcile_orphaned_running(b.conn) == []
    assert len(b.launched) == 1
    assert tuple(_row(b.conn, claimed.id))[:3] == ("running", claimed.claim_lock, claimed.current_run_id)

    # The owner admits the attempt: its receipt settles the intent, and the owner fence governs.
    _owner_admits(b, claimed)
    assert not kanban_launch.handoff_pending(b.conn, claimed.id)
    assert owner_reclaim_blocked(b.conn, claimed.id)
    _expire_claim(b.conn, claimed.id)
    kb.release_stale_claims(b.conn)
    assert _row(b.conn, claimed.id)["status"] == "running"  # queued admission: never repeated

    # The owner-managed attempt finishes the card under its own run; nothing launched twice.
    with kb.write_txn(b.conn):
        kb._append_event(b.conn, claimed.id, "worker_bound", {
            "pid": os.getpid(), "claim_lock": claimed.claim_lock, "started_at": "x"}, run_id=claimed.current_run_id)
    assert kb.complete_task(b.conn, claimed.id, result="ok", expected_run_id=claimed.current_run_id)
    kbd.dispatch_once(b.conn)
    assert len(b.launched) == 1
    runs = b.conn.execute("SELECT id, outcome FROM task_runs WHERE task_id = ?", (claimed.id,)).fetchall()
    assert [tuple(r) for r in runs] == [(claimed.current_run_id, "completed")]


def test_unanswered_intent_lapses_with_its_claim_and_a_late_admission_is_refused(board, monkeypatch):
    from gateway import session_kanban
    from hermes_state_runtime import RuntimeStoreError

    b = board
    claimed = _claimed(b.conn)
    publish = kbd._set_worker_pid
    _fail_pid_write(monkeypatch)
    with pytest.raises(kanban_launch.WorkerHandoffUncertain):
        _spawn(b, claimed)
    monkeypatch.setattr(kbd, "_set_worker_pid", publish)
    _expire_claim(b.conn, claimed.id)
    assert not kanban_launch.handoff_pending(b.conn, claimed.id)
    assert not owner_reclaim_blocked(b.conn, claimed.id)
    kb.release_stale_claims(b.conn)
    assert _row(b.conn, claimed.id)["status"] == "ready"

    # The waiter that lost its pid reaches the owner late: its claim is no longer current.
    actor = SimpleNamespace(capabilities={"session:create"}, profile_id="p")
    owner = SimpleNamespace(native_owner=True, authority=SimpleNamespace(profile_id="p"), actor=actor)
    params = {"board": kb.get_current_board(), "task_id": claimed.id, "run_id": claimed.current_run_id,
              "claim_lock": claimed.claim_lock, "db": str(kb.kanban_db_path())}
    with pytest.raises(RuntimeStoreError) as refused:
        session_kanban.build_kanban_policy(owner, params, {})
    # The owner surfaces it as invalid_kanban_claim, chained from its stale-claim check.
    assert "stale_kanban_claim" in repr(refused.value) + repr(refused.value.__cause__)


def test_spawn_that_starts_nothing_cancels_its_intent(board, monkeypatch):
    b = board
    claimed = _claimed(b.conn)

    def no_binary(*_a, **_k):
        raise FileNotFoundError("missing")
    monkeypatch.setattr(subprocess, "Popen", no_binary)
    with pytest.raises(RuntimeError):
        _spawn(b, claimed)
    assert not kanban_launch.handoff_pending(b.conn, claimed.id)
    last = b.conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                          (claimed.id,)).fetchone()
    assert last[0] == "worker_handoff_cancelled"


def test_published_pid_settles_the_intent(board):
    b = board
    claimed = _claimed(b.conn)
    assert _spawn(b, claimed) == os.getpid()
    assert not kanban_launch.handoff_pending(b.conn, claimed.id)
    assert _row(b.conn, claimed.id)["worker_pid"] == os.getpid()


def test_stale_launch_claim_starts_nothing(board):
    b = board
    claimed = _claimed(b.conn)
    kb.reclaim_task(b.conn, claimed.id)
    with pytest.raises(kanban_launch.WorkerHandoffUncertain):
        _spawn(b, claimed)
    assert b.launched == []


def test_dispatch_tick_keeps_the_claim_when_pid_persistence_fails(board, monkeypatch):
    """Through ``dispatch_once``: the lane books an uncertain handoff as neither a failure nor a release."""
    b = board
    tid = kb.create_task(b.conn, title="tick", assignee="worker")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    _fail_pid_write(monkeypatch)
    kbd.dispatch_once(b.conn)
    assert len(b.launched) == 1
    row = _row(b.conn, tid)
    assert (row["status"], row["consecutive_failures"]) == ("running", 0)
    assert kanban_launch.handoff_pending(b.conn, tid)
    kbd.dispatch_once(b.conn)
    assert len(b.launched) == 1
