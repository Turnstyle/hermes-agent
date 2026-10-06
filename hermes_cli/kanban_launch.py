"""Fail-closed admission for card claims and worker handoffs."""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

from hermes_constants import get_default_hermes_root, get_hermes_home

_log = logging.getLogger(__name__)
_held = threading.local()


class LaunchDeferred(RuntimeError):
    """Admission was refused before a worker started."""


class WorkerHandoffUncertain(RuntimeError):
    """A spawned worker must retain its claim when PID persistence fails."""


HANDOFF_HOLD_REASON = (
    "Worker handoff unresolved; automatic recovery blocked because a worker may "
    "have started without a recorded PID. Decider must verify the worker's identity "
    "or exit before retry; escalate to Coordinator or Tasker, then King, then Conductor."
)


def handoff_pending(
    conn: sqlite3.Connection, task_id: str, *, errors_out: list | None = None, stage: str = "launch",
) -> bool:
    """An intent survives expiry and lost claim/run metadata until explicitly resolved."""
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? "
        "AND kind IN ('worker_handoff_pending', 'worker_handoff_cancelled', 'worker_handoff_resolved') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None or row["kind"] != "worker_handoff_pending":
        return False
    _log.warning("kanban %s: %s: %s", stage, task_id, HANDOFF_HOLD_REASON)
    if errors_out is not None:
        errors_out.append((task_id, f"{stage}: {HANDOFF_HOLD_REASON}"))
    return True


def begin_worker_handoff(conn: sqlite3.Connection, task: Task) -> None:
    """Commit before spawn, so a failed PID write needs no further successful write."""
    from hermes_cli import kanban_db as kb

    if conn.in_transaction:
        raise LaunchDeferred("Worker handoff requires a committed intent outside an existing transaction")
    with kb.write_txn(conn):
        if handoff_pending(conn, task.id):
            raise WorkerHandoffUncertain(HANDOFF_HOLD_REASON)
        current = kb.get_task(conn, task.id)
        if (current is None or current.status != "running" or not current.claim_lock
                or current.current_run_id != task.current_run_id
                or current.claim_lock != task.claim_lock or current.worker_pid):
            raise RuntimeError("Worker launch requires an unstarted, current Kanban claim")
        kb._append_event(conn, task.id, "worker_handoff_pending", {
            "reason": HANDOFF_HOLD_REASON, "claim_lock": task.claim_lock,
        }, run_id=task.current_run_id)


def cancel_worker_handoff(conn: sqlite3.Connection, task: Task) -> None:
    """Only for a spawn call known to have failed before returning a process."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        kb._append_event(conn, task.id, "worker_handoff_cancelled", {
            "reason": "Spawn failed before returning a worker",
        }, run_id=task.current_run_id)


def pause_reason() -> str | None:
    """Presence alone pauses launches, including unreadable or partial markers."""
    try:
        home = get_hermes_home()
        root = get_default_hermes_root(home=home.resolve())
        for parent in dict.fromkeys((home, root)):
            for name in ("ESTOP", ".drain_request.json"):
                path = parent / name
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                return f"launch paused by {path}"
    except Exception as exc:
        return f"launch pause check failed ({type(exc).__name__})"
    return None


@contextlib.contextmanager
def launch_guard(conn=None, *, board=None, db_path=None, wait_seconds=0):
    """Hold the board lock until the caller records its claim/worker.

    Nested claims in a dispatcher reuse only this thread's admission. Copied
    contexts and forked children must acquire their own lock. The open SQLite
    connection is authoritative for private boards and explicit DB overrides.
    """
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    try:
        if conn is not None:
            rows = conn.execute("PRAGMA database_list").fetchall()
            actual = next(file for _, name, file in rows if name == "main")
            if actual:
                db_path = Path(actual)
        path = Path(db_path or kb.kanban_db_path(board=board)).resolve()
    except Exception as exc:
        _log.warning("launch skipped: board path unavailable (%s)", type(exc).__name__)
        yield False
        return

    key = (os.getpid(), str(path))
    owned = getattr(_held, "boards", {})
    nested = key in owned
    deadline = time.monotonic() + wait_seconds
    with contextlib.ExitStack() as stack:
        while True:
            lock = contextlib.nullcontext(True) if nested else kbc._dispatch_tick_lock(path)
            acquired = stack.enter_context(lock)
            if acquired or time.monotonic() >= deadline or pause_reason():
                break
            stack.close()
            time.sleep(0.01)
        reason = pause_reason() or (None if acquired else "board dispatch lock unavailable")
        if reason:
            _log.warning("launch skipped: %s", reason)
            yield False
            return
        if not nested:
            _held.boards = {**owned, key: True}
        try:
            yield True
        finally:
            if not nested:
                _held.boards = owned


@contextlib.contextmanager
def require_launch(conn=None, *, board=None, wait_seconds=0):
    """Spawn callers cannot turn refused admission into a successful handoff."""
    with launch_guard(conn, board=board, wait_seconds=wait_seconds) as admitted:
        if not admitted:
            raise LaunchDeferred("Worker launch deferred: restart pause or board lock unavailable")
        yield
