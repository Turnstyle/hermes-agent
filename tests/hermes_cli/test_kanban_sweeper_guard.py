"""check_idle: a dead or absent waiter is never proof that an owner-managed attempt is over.

Owner-managed runs keep two handles beside the dispatcher's spawned submitter: the owner's
admission receipt (``owner_admitted`` -> the owner's ``session_admissions`` row) and the
interpreter the owner bound to the run (``worker_bound``). Both outlive the waiter, so the
canonical sweeper's idleness proof must read them.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_sweeper_guard import check_idle
from hermes_cli.process_identity import process_birth


def _absent_pid(base: int = 3_700_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path: Path):
    path = tmp_path / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    ledger = tmp_path / "state.db"
    with closing_store(ledger) as store:
        store.execute("CREATE TABLE session_admissions "
                      "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
    yield conn, ledger
    conn.close()


def closing_store(path: Path):
    from contextlib import closing
    return closing(sqlite3.connect(path, isolation_level=None))


def _finished_card(conn, *, bound_pid=None, bound_lock=None, receipt=None, ledger=None,
                   owner_pid=None, owner_birth=None):
    """A card whose run already closed (status done) after its waiter died."""
    tid = kb.create_task(conn, title="owner-managed card", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id, lock = claimed.current_run_id, claimed.claim_lock
    with kb.write_txn(conn):
        if bound_pid is not None:
            kb._append_event(conn, tid, "worker_bound", {
                "pid": bound_pid, "claim_lock": bound_lock or lock,
                "started_at": kbd._process_fingerprint(bound_pid) or kbd.UNVERIFIED_WORKER_FINGERPRINT,
            }, run_id=run_id)
        if receipt is not None:
            kb._append_event(conn, tid, "owner_admitted", {
                "db": str(ledger), "session_id": f"s-{tid}", "request_id": f"r-{tid}",
                "pid": owner_pid, "birth": owner_birth,
            }, run_id=run_id)
            if receipt != "missing":
                with closing_store(ledger) as store:
                    store.execute("INSERT INTO session_admissions VALUES (?,?,?,?)",
                                  (f"s-{tid}", f"r-{tid}", *receipt))
    # The spawned submitter (the waiter) is gone; only owner-side evidence can speak.
    dead = _absent_pid()
    conn.execute("UPDATE tasks SET worker_pid=?, worker_started_at=NULL WHERE id=?", (dead, tid))
    conn.execute("UPDATE task_runs SET worker_pid=?, worker_started_at=NULL WHERE id=?", (dead, run_id))
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
    assert kb.get_task(conn, tid).status == "done"
    return tid


def _reasons(verdict):
    return {entry["reason"] for entry in verdict["evidence"]}


def test_terminal_task_with_live_bound_interpreter_is_not_idle(board):
    conn, _ledger = board
    tid = _finished_card(conn, bound_pid=os.getpid())
    verdict = check_idle(conn, tid)
    assert verdict["idle"] is False
    assert "bound_worker_running" in _reasons(verdict)


def test_unresolved_owner_receipt_with_live_owner_is_not_idle(board):
    conn, ledger = board
    tid = _finished_card(conn, bound_pid=_absent_pid(), receipt=("started", None), ledger=ledger,
                         owner_pid=os.getpid(), owner_birth=process_birth(psutil.Process()))
    verdict = check_idle(conn, tid)
    assert verdict["idle"] is False
    assert _reasons(verdict) == {"owner_receipt_unresolved"}


@pytest.mark.parametrize("receipt", ["missing", ("queued", None), ("unknown", None)])
def test_unsettled_receipt_of_a_live_owner_is_not_idle(board, receipt):
    conn, ledger = board
    tid = _finished_card(conn, receipt=receipt, ledger=ledger,
                         owner_pid=os.getpid(), owner_birth=process_birth(psutil.Process()))
    assert check_idle(conn, tid)["idle"] is False


def test_unreadable_owner_ledger_with_live_owner_is_not_idle(board, tmp_path):
    conn, _ledger = board
    tid = _finished_card(conn, receipt="missing", ledger=tmp_path / "absent" / "state.db",
                         owner_pid=os.getpid(), owner_birth=process_birth(psutil.Process()))
    verdict = check_idle(conn, tid)
    assert verdict["idle"] is False
    assert [e["receipt_status"] for e in verdict["evidence"]] == ["unreadable"]


def test_closed_run_of_a_dead_owner_with_dead_interpreter_is_idle(board):
    """An unsettled receipt stops mattering once its owner is gone and no worker lives: the
    owner's managed interpreters die with it, and a closed run is never resumed."""
    conn, ledger = board
    tid = _finished_card(conn, bound_pid=_absent_pid(), receipt=("unknown", None), ledger=ledger,
                         owner_pid=_absent_pid(base=3_800_000), owner_birth="gone")
    assert check_idle(conn, tid) == {"idle": True, "evidence": []}


def test_open_run_with_unresolved_receipt_is_not_idle_without_owner_proof(board):
    """A running card whose waiter died keeps its unresolved receipt as evidence until the
    owner-recovery proof (``owner_attempt_provably_over``) says nothing can still execute it."""
    conn, ledger = board
    tid = kb.create_task(conn, title="running card", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {
            "db": str(ledger), "session_id": "s-open", "request_id": "r-open",
            "pid": _absent_pid(base=3_800_000), "birth": "gone"}, run_id=claimed.current_run_id)
    with closing_store(ledger) as store:
        store.execute("INSERT INTO session_admissions VALUES ('s-open','r-open','unknown',NULL)")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (_absent_pid(), tid))
    verdict = check_idle(conn, tid)
    # The live claim alone would not prove activity (no pid recorded is not the case here); the
    # receipt is what keeps it: the claim has not expired, so the owner proof cannot settle it.
    assert verdict["idle"] is False
    assert "owner_receipt_unresolved" in _reasons(verdict)


def test_closed_run_with_dead_pids_and_settled_receipt_is_idle(board):
    conn, ledger = board
    tid = _finished_card(conn, bound_pid=_absent_pid(), receipt=("terminal", "completed"), ledger=ledger,
                         owner_pid=os.getpid(), owner_birth=process_birth(psutil.Process()))
    assert check_idle(conn, tid) == {"idle": True, "evidence": []}


def test_foreign_bound_interpreter_cannot_prove_idleness(board):
    conn, _ledger = board
    tid = _finished_card(conn, bound_pid=_absent_pid(), bound_lock="another-host:4242")
    verdict = check_idle(conn, tid)
    assert verdict["idle"] is False
    assert _reasons(verdict) == {"bound_foreign_worker_unverified"}


def test_closed_run_live_waiter_pid_is_still_evidence(board):
    """The carry's closed-run PID inspection survives the owner-evidence additions."""
    conn, _ledger = board
    tid = _finished_card(conn)
    conn.execute("UPDATE task_runs SET worker_pid=?, worker_started_at=? WHERE task_id=?",
                 (os.getpid(), kbd._process_fingerprint(os.getpid()), tid))
    verdict = check_idle(conn, tid)
    assert verdict["idle"] is False
    assert _reasons(verdict) == {"worker_running"}
