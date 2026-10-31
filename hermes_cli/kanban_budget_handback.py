"""Neutral turn-budget hand-back for an owner-managed Kanban run, settled by the owner's receipt.

A goal-mode card's turn budget is an execution slice, not a verdict on the task: when it runs
out without the worker finishing, the card goes back to its source phase for the next attempt
instead of ``blocked``, and ``consecutive_failures`` is not charged.

The turn never writes the card or its run for this. Only the attempt's own bound interpreter
may hand its run back (``session_kanban._bound_worker_matches``: the run's ``worker_bound`` event
names this interpreter under this claim); it records the hand-back on its own ``worker_result``
receipt with :data:`KANBAN_BUDGET_HANDBACK_EXIT_CODE`, the owner settles its admission as for any
finished turn, and the dead-worker sweep (``kanban_db_dispatch_recovery.reclaim_dead_workers``)
closes the run through the same native release as every managed exit, booked ``budget_handback``
and never counted as a crash.
"""
from __future__ import annotations

import json
from typing import Optional

from hermes_cli.kanban_db_dispatch import _DeadWorker

BUDGET_HANDBACK = "budget_handback"
# Outside sysexits (64-78): 75 is the rate-limit requeue and 78 the terminal-provider parking.
KANBAN_BUDGET_HANDBACK_EXIT_CODE = 79


def handback_message(reason: str, summary: str = "") -> str:
    return ("Turn-limit hand-back: " + reason.strip() + "\nDefault: return the card for the next "
            "worker; this slice limit is not a task failure. Choose a smaller task or an explicit "
            "hold if it keeps running out.\n"
            + (summary.strip()[:3000] or "No final summary was available; inspect this run's worker log."))


class _HandedBack(_DeadWorker):
    @property
    def run_outcome(self) -> str:
        return BUDGET_HANDBACK


def handback_dead_worker(conn, task_id: str, pid: int, claimer: Optional[str]) -> _DeadWorker:
    """The sweep's booking for a current run whose owner receipt records a hand-back."""
    row = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.current_run_id = e.run_id "
        "WHERE t.id = ? AND e.task_id = t.id AND e.kind = 'worker_result' ORDER BY e.id DESC LIMIT 1",
        (task_id,)).fetchone()
    try:
        handback = (json.loads(row[0]) if row else {}).get("handback") or {}
    except ValueError:
        handback = {}
    reason = str(handback.get("reason") or "turn budget spent")
    return _HandedBack(
        BUDGET_HANDBACK, KANBAN_BUDGET_HANDBACK_EXIT_CODE,
        handback_message(reason, str(handback.get("summary") or "")),
        BUDGET_HANDBACK,
        {"pid": pid, "claimer": claimer, "exit_code": KANBAN_BUDGET_HANDBACK_EXIT_CODE, "reason": reason},
    )
