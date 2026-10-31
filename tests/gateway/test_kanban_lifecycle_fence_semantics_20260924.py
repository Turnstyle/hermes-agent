"""Lifecycle-fence semantics through the real dispatcher tick (Snowdrop, 2026-09-20..24).

A Fleet adapter installed on a board refuses local lifecycle writes it holds no verified lease for
with an exact ``RAISE(ABORT, <literal>)``. Live, one refused row aborted every dispatcher tick
("ready queue non-empty for 5246+ consecutive ticks but 0 workers spawned"). The contract:

  1. the refusal rolls back ONLY that row, which stays exactly as it was;
  2. it is surfaced (``reclaim_errors`` / ``claim_errors``), not swallowed or raised;
  3. sibling rows and the later phases of the SAME tick still run;
  4. any other database error (lock contention, a CHECK constraint that merely names a fence
     phrase) still aborts visibly; no trigger is disabled and no lease is fabricated.

The adapter is out of tree, so its fence is emulated with the adapter's exact literals on the real
board schema. Owner admission is untouched by all of this: these rows never reach a submitter.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

_RECLAIM_FENCE = "verified run generation no longer owns lifecycle write"
_CLAIM_FENCE = "verified execution lease required before running"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  dispatch_profiles:\n    - worker\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "worker")
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def _orphan(conn, title):
    """A ``running`` card whose claim bookkeeping was lost (the live zombie shape)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    conn.execute("UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
    conn.commit()
    return tid


def _fence_reclaim(conn, tid):
    conn.execute(
        f"CREATE TRIGGER fence_reclaim_{tid} BEFORE UPDATE OF status ON tasks "
        f"WHEN OLD.id = '{tid}' AND OLD.status = 'running' AND NEW.status != 'running' "
        f"BEGIN SELECT RAISE(ABORT, '{_RECLAIM_FENCE}'); END")
    conn.commit()


def _fence_claim(conn, tid):
    conn.execute(
        f"CREATE TRIGGER fence_claim_{tid} BEFORE UPDATE OF status ON tasks "
        f"WHEN OLD.id = '{tid}' AND NEW.status = 'running' "
        f"BEGIN SELECT RAISE(ABORT, '{_CLAIM_FENCE}'); END")
    conn.commit()


def _row(conn, tid):
    return dict(conn.execute(
        "SELECT status, assignee, claim_lock, claim_expires, worker_pid, current_run_id "
        "FROM tasks WHERE id=?", (tid,)).fetchone())


def test_fenced_orphan_is_isolated_and_its_sibling_reconciled(board):
    fenced, local = _orphan(board, "fenced"), _orphan(board, "local")
    _fence_reclaim(board, fenced)
    before = _row(board, fenced)
    errors: list = []

    assert kbd.reconcile_orphaned_running(board, errors_out=errors) == [local]

    assert [tid for tid, _ in errors] == [fenced]
    assert _RECLAIM_FENCE in errors[0][1]
    assert _row(board, fenced) == before  # no partial write survives the aborted UPDATE
    assert _row(board, local)["status"] == "ready"


def test_whole_tick_continues_past_a_fenced_orphan(board):
    fenced = _orphan(board, "fenced")
    _fence_reclaim(board, fenced)
    before = _row(board, fenced)
    ready = kb.create_task(board, title="plain ready", assignee="worker")
    spawned = []

    result = kbd.dispatch_once(board, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 999999)

    assert fenced in [tid for tid, _ in result.reclaim_errors]
    assert fenced not in result.reconciled_orphans
    assert _row(board, fenced) == before
    assert spawned == [ready], "the claim/spawn phase must still run after the refusal"


def test_fenced_claim_leaves_its_sibling_dispatchable(board):
    fenced = kb.create_task(board, title="fenced ready", assignee="worker")
    sibling = kb.create_task(board, title="sibling ready", assignee="worker")
    _fence_claim(board, fenced)
    spawned = []

    result = kbd.dispatch_once(board, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 999999)

    assert fenced in [tid for tid, _ in result.claim_errors]
    row = _row(board, fenced)
    assert (row["status"], row["claim_lock"], row["current_run_id"]) == ("ready", None, None)
    assert spawned == [sibling]


def test_operational_error_still_aborts_visibly(board, monkeypatch):
    """Real lock contention is not a fence: it must propagate, not be filed per row."""
    _orphan(board, "locked")
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "20")
    path = kb.kanban_db_path(board="default")
    blocker = sqlite3.connect(str(path), timeout=0.02)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with kbc.connect() as conn, pytest.raises(sqlite3.OperationalError):
            kbd.reconcile_orphaned_running(conn, errors_out=[])
    finally:
        blocker.rollback()
        blocker.close()


def test_check_constraint_named_after_a_fence_is_not_swallowed(board):
    """A CHECK constraint that merely NAMES a fence phrase fails as ``CHECK constraint failed``
    (SQLITE_CONSTRAINT_CHECK), not the adapter's RAISE(ABORT): it must abort loudly."""
    tid = _orphan(board, "orphan")
    board.executescript(f"""
        CREATE TABLE unrelated_audit (
            value INTEGER, CONSTRAINT "{_CLAIM_FENCE}" CHECK (value > 0));
        CREATE TRIGGER unrelated_audit_on_task_update AFTER UPDATE ON tasks
        BEGIN INSERT INTO unrelated_audit (value) VALUES (-1); END;
    """)
    errors: list = []

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        kbd.reconcile_orphaned_running(board, errors_out=errors)

    assert errors == []
    assert _row(board, tid)["status"] == "running"
