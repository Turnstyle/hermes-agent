"""Neutral owned-run budget hand-back with a Decider off-ramp."""
from __future__ import annotations
import time


def handback_budget(conn, task_id: str, *, expected_run_id, claim_lock: str, reason: str, summary: str = "") -> bool:
    """Return this worker's card to ready with evidence; stale or missing ownership is a no-op.

    A budget is an execution slice, so handing it back does not count as a failed task.
    The run closure is a compare-and-swap inside the board's writer transaction.
    """
    from hermes_cli import kanban_db as kb
    if isinstance(expected_run_id, bool) or not isinstance(expected_run_id, int) or expected_run_id < 1 or not claim_lock:
        return False
    message = ("Turn-limit hand-back: " + reason.strip() + "\nDefault: return to ready for the next worker; this slice limit is not a task failure. "
               "A Decider may choose a smaller task or an explicit hold.\n" + (summary.strip()[:3000] or "No final summary was available; inspect this run's worker log."))
    with kb.write_txn(conn):
        owned = conn.execute(
            "SELECT t.assignee FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND t.status = 'running' AND t.current_run_id = ? "
            "AND t.claim_lock = ? AND r.task_id = t.id AND r.ended_at IS NULL",
            (task_id, expected_run_id, claim_lock),
        ).fetchone()
        if owned is None:
            return False
        closed = conn.execute(
            "UPDATE task_runs SET status = 'budget_handback', outcome = 'budget_handback', "
            "summary = ?, ended_at = ?, claim_expires = NULL WHERE id = ? AND task_id = ? AND ended_at IS NULL",
            (message, int(time.time()), expected_run_id, task_id),
        )
        if closed.rowcount != 1:
            return False
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND current_run_id = ? AND claim_lock = ?",
            (task_id, expected_run_id, claim_lock),
        )
        kb.add_comment(conn, task_id, owned["assignee"] or "worker", message)
        kb._append_event(conn, task_id, "budget_handback", {"reason": reason, "retry_status": "ready"}, run_id=expected_run_id)
    kb.notify_task_updated(conn, task_id, ("status", "current_run_id"))
    return True
