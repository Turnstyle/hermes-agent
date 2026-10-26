"""Neutral turn-budget hand-back for an owner-managed Kanban run.

A goal-mode card's turn budget is an execution slice, not a verdict on the task: when it runs
out without the worker finishing, the card goes back to its source phase for the next attempt
instead of ``blocked``, and ``consecutive_failures`` is not charged. Only the attempt's own
bound interpreter may hand its run back: the caller passes the owner-receipt identity check
(``session_kanban._bound_worker_matches``: the run's ``worker_bound`` event names this
interpreter under this claim), and it runs inside the same write transaction as the run/claim
compare-and-swap. The run closes through ``kanban_db._end_run`` like every other transition;
the owner's admission receipt is the owner's to settle and is never written here.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

BUDGET_HANDBACK = "budget_handback"


def handback_budget(conn, task_id: str, *, expected_run_id, claim_lock: str, reason: str,
                    summary: str = "", owner_check: Optional[Callable] = None) -> bool:
    """Close the current run as ``budget_handback`` and requeue the card; False touches nothing.

    False when the run is no longer the card's live run under ``claim_lock``, or when
    ``owner_check(conn)`` (evaluated inside the transaction) does not vouch for the caller.
    """
    from hermes_cli import kanban_db as kb

    if isinstance(expected_run_id, bool) or not isinstance(expected_run_id, int) or not claim_lock:
        return False
    message = ("Turn-limit hand-back: " + reason.strip() + "\nDefault: return the card for the next "
               "worker; this slice limit is not a task failure. Choose a smaller task or an explicit "
               "hold if it keeps running out.\n"
               + (summary.strip()[:3000] or "No final summary was available; inspect this run's worker log."))
    with kb.write_txn(conn):
        if owner_check is not None and not owner_check(conn):
            return False
        owned = conn.execute(
            "SELECT t.assignee FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND t.status = 'running' AND t.current_run_id = ? AND t.claim_lock = ? "
            "AND r.ended_at IS NULL", (task_id, expected_run_id, claim_lock)).fetchone()
        if owned is None:
            return False
        retry_status = kb._retry_status_for_run(conn, task_id, expected_run_id)
        conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL WHERE id = ? AND current_run_id = ? AND claim_lock = ?",
            (retry_status, task_id, expected_run_id, claim_lock))
        run_id = kb._end_run(conn, task_id, outcome=BUDGET_HANDBACK, summary=message,
                             metadata={"reason": reason, "retry_status": retry_status})
        kb._insert_comment(conn, task_id, owned[0] or "worker", message, int(time.time()))
        kb._append_event(conn, task_id, BUDGET_HANDBACK, {"reason": reason, "retry_status": retry_status},
                         run_id=run_id)
    kb.notify_task_updated(conn, task_id, ("status", "current_run_id"))
    return True
