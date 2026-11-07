"""Pre-spawn launch admission and the durable worker-handoff intent.

The owner-managed path (``kanban_worker_client`` submitting the claim to its profile owner's
``kanban.run``) fences execution with owner receipts: ``owner_admitted`` under the exact
run/claim, then ``worker_bound``. Two windows sit before any receipt can exist:

* ESTOP / external drain. ``hermes pause`` and a begin-drain marker hold NEW work. The
  gateway-embedded tick checks ESTOP before it starts, but a standalone dispatcher does not, and
  nothing re-checks between the claim and ``Popen``. :func:`pause_reason` is that last check;
  a refusal is :class:`LaunchDeferred`, host-level, so it never spends the card's retry budget.
* ``Popen`` -> pid persistence. A waiter that started but whose pid never reached the board
  must keep its claim: releasing it as a spawn failure lets the next tick launch a second
  attempt beside one the owner may already be admitting. The ``worker_handoff_pending`` intent
  is committed BEFORE ``Popen`` and scoped to that run. The first post-spawn evidence
  (``spawned``, ``worker_registered``) or owner receipt (``owner_admitted``, ``worker_bound``)
  settles it, and it lapses with the claim: once the claim expires, a late admission of that
  run is refused by the owner's claim check or, if it landed first, held by
  ``kanban_owner_recovery.owner_reclaim_blocked``. It is never a second permanent authority
  beside the owner receipts.

No board lock is taken here: the dispatcher already holds the board's tick lock around the
spawn, and owner admission (``session_kanban.run_task``) must never wait on it.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Callable, Optional

from tools.process_registry import RestartSafeScopeUnavailable

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

_log = logging.getLogger(__name__)


class LaunchDeferred(RestartSafeScopeUnavailable):
    """Launch refused before a worker started (ESTOP, drain): a host condition, never the
    card's, so the dispatcher books it like an unplaceable restart-safe scope."""


class WorkerHandoffUncertain(RuntimeError):
    """A worker may have started for the claim: keep the claim and charge the card nothing."""


HANDOFF_HOLD_REASON = (
    "Worker handoff unresolved: a worker may have started without a recorded pid. Recovery "
    "waits for its owner receipt (owner_admitted / worker_bound) or for the claim to lapse."
)

# Run-scoped evidence that settles an intent: the pid reached the board, or the owner answered.
_SETTLING_KINDS = (
    "spawned", "worker_registered", "owner_admitted", "worker_bound",
    "worker_handoff_cancelled", "worker_handoff_resolved",
)
_PENDING_SQL = (
    "SELECT t.claim_expires, "
    "(SELECT MAX(e.id) FROM task_events e WHERE e.task_id = t.id AND e.run_id = t.current_run_id "
    " AND e.kind = 'worker_handoff_pending'), "
    "(SELECT MAX(e.id) FROM task_events e WHERE e.task_id = t.id AND e.run_id = t.current_run_id "
    f" AND e.kind IN ({', '.join('?' for _ in _SETTLING_KINDS)})) "
    "FROM tasks t WHERE t.id = ? AND t.status = 'running' AND t.current_run_id IS NOT NULL"
)


def handoff_pending(
    conn: sqlite3.Connection, task_id: str, *, errors_out: Optional[list] = None, stage: str = "launch",
) -> bool:
    """True while the task's CURRENT run has an unsettled pre-spawn intent and a live claim."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(_PENDING_SQL, (*_SETTLING_KINDS, task_id)).fetchone()
    if row is None or row[1] is None or (row[2] or 0) > row[1]:
        return False
    expires = kb._to_epoch(row[0])
    if expires is None or expires < int(time.time()):
        return False
    _log.warning("kanban %s: %s: %s", stage, task_id, HANDOFF_HOLD_REASON)
    if errors_out is not None:
        errors_out.append((task_id, f"{stage}: {HANDOFF_HOLD_REASON}"))
    return True


def begin_worker_handoff(conn: sqlite3.Connection, task: "Task") -> None:
    """Commit the intent before ``Popen``, so a failed pid write needs no further write to be seen."""
    from hermes_cli import kanban_db as kb

    if conn.in_transaction:
        raise LaunchDeferred("worker handoff intent must commit outside an open transaction")
    with kb.write_txn(conn):
        if handoff_pending(conn, task.id):
            raise WorkerHandoffUncertain(HANDOFF_HOLD_REASON)
        row = conn.execute(
            "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?", (task.id,),
        ).fetchone()
        if (row is None or row[0] != "running" or not task.claim_lock or row[1] != task.claim_lock
                or row[2] != task.current_run_id or row[3] is not None):
            # The claim moved on (or already has a worker): nothing starts, and nothing here may
            # release a claim that is no longer this launch's.
            raise WorkerHandoffUncertain(f"task {task.id}: launch claim is no longer current; not spawning")
        kb._append_event(conn, task.id, "worker_handoff_pending", {"claim_lock": task.claim_lock},
                         run_id=task.current_run_id)


def cancel_worker_handoff(conn: sqlite3.Connection, task: "Task") -> None:
    """Only for a spawn known to have failed before returning a process."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        kb._append_event(conn, task.id, "worker_handoff_cancelled",
                         {"reason": "spawn failed before returning a worker"}, run_id=task.current_run_id)


def pause_reason() -> Optional[str]:
    """Why launches are paused right now, or None. Unreadable state pauses (fail closed)."""
    try:
        from agent.estop import is_engaged
        if is_engaged():
            return "launch paused: ESTOP is engaged (hermes pause)"
        from gateway.drain_control import drain_requested
        from hermes_constants import get_default_hermes_root, get_hermes_home
        home = get_hermes_home()
        for parent in dict.fromkeys((home, get_default_hermes_root(home=home.resolve()))):
            if drain_requested(home=parent):
                return f"launch paused: drain requested in {parent}"
    except Exception as exc:
        return f"launch pause check failed ({type(exc).__name__})"
    return None


def require_launch() -> None:
    """Raise :class:`LaunchDeferred` while launches are paused."""
    reason = pause_reason()
    if reason:
        raise LaunchDeferred(reason)


def spawn_with_handoff(conn: sqlite3.Connection, claimed: "Task", spawn: Callable[[], Optional[int]],
                       *, durable: bool) -> Optional[int]:
    """Run ``spawn`` (returns the worker pid) and publish the pid; the lane's spawn step.

    ``durable`` (the dispatcher's own ``_default_spawn``) brackets ``spawn`` with the pre-spawn
    intent: a spawn that raised started nothing and cancels it; a pid that cannot be published
    raises :class:`WorkerHandoffUncertain` and leaves the intent standing, so the caller keeps
    the claim instead of releasing it beside a live waiter. Test spawners that start no process
    keep the plain publish-after-spawn order.
    """
    from hermes_cli import kanban_db_dispatch as kd

    if durable:
        begin_worker_handoff(conn, claimed)
    try:
        pid = spawn()
    except BaseException:
        if durable:
            try:
                cancel_worker_handoff(conn, claimed)
            except sqlite3.Error:
                # Still settles: the caller releases the claim, closing the intent's run.
                _log.warning("kanban launch: could not cancel handoff for %s", claimed.id, exc_info=True)
        raise
    if not pid:
        return pid
    try:
        kd._set_worker_pid(conn, claimed.id, int(pid), run_id=claimed.current_run_id,
                           claim_lock=claimed.claim_lock)
    except Exception as exc:
        if not durable:
            raise
        raise WorkerHandoffUncertain(
            f"worker {pid} started for {claimed.id} but its pid did not persist ({exc}); "
            "claim kept, handoff unresolved until the owner receipt or claim expiry"
        ) from exc
    return pid
