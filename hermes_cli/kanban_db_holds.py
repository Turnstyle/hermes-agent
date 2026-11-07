"""Fleet hold state and guarded operator release (fleet carry K04).

A ``needs_input`` hold pauses a todo/triage card for at most two hours: a hold may
not wait forever on a person. Card text never holds a card. Releases are explicit
(``unblocked``/``promoted_manual`` events), and ``unblock_task`` carries the
operator's actor/reason plus optional compare-and-swap guards so a release that was
decided against one blocked snapshot cannot release a newer one.

Imported blocked snapshots: direct SQL imports write ``status='blocked'`` without a
local ``blocked`` event, so ``task_block_entries`` records the event boundary of
every blocked write (triggers below). Only a recovery event AFTER that boundary may
release the snapshot automatically (``recompute_ready``).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

_HOLD_EVENT_KINDS = ("blocked", "block_loop_detected")
_HOLD_RELEASE_EVENT_KINDS = ("unblocked", "promoted_manual")
NEEDS_INPUT_TODO_HOLD_SECONDS = 2 * 3600  # A hold may not wait forever on a person.

# Appended to ``kanban_db.SCHEMA_SQL`` (connect() runs it statement by statement in
# one write transaction, so an importer cannot interleave between table and triggers).
BLOCK_ENTRY_SCHEMA_SQL = """
-- Direct SQL snapshot imports do not emit a local blocked event. Remember
-- the event boundary of every blocked write, including blocked-to-blocked
-- snapshots, so an older release cannot release a newer hold.
CREATE TABLE IF NOT EXISTS task_block_entries (
    task_id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS task_block_entry_insert AFTER INSERT ON tasks
WHEN NEW.status = 'blocked'
BEGIN
    INSERT OR REPLACE INTO task_block_entries VALUES (
        NEW.id, COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = NEW.id), 0)
    );
END;
CREATE TRIGGER IF NOT EXISTS task_block_entry_update AFTER UPDATE OF status ON tasks
WHEN NEW.status = 'blocked'
BEGIN
    INSERT OR REPLACE INTO task_block_entries VALUES (
        NEW.id, COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = NEW.id), 0)
    );
END;
CREATE TRIGGER IF NOT EXISTS task_block_entry_delete AFTER DELETE ON tasks
BEGIN
    DELETE FROM task_block_entries WHERE task_id = OLD.id;
END;
INSERT OR IGNORE INTO task_block_entries (task_id, event_id)
    SELECT t.id, COALESCE((SELECT MAX(e.id) FROM task_events e WHERE e.task_id = t.id), 0)
    FROM tasks t WHERE t.status = 'blocked';
"""


def _needs_input_hold_state(conn: sqlite3.Connection, task_id: str, block_kind: Optional[str]) -> Optional[str]:
    """'active', 'expired' or None for a needs_input hold on a todo/triage card."""
    if block_kind != "needs_input":
        return None
    row = conn.execute(
        "SELECT kind, created_at FROM task_events WHERE task_id = ? AND kind IN (?, ?, ?, ?) "
        "ORDER BY id DESC LIMIT 1", (task_id, *_HOLD_EVENT_KINDS, *_HOLD_RELEASE_EVENT_KINDS),
    ).fetchone()
    if row is None or row["kind"] not in _HOLD_EVENT_KINDS:
        return None
    age = int(time.time()) - int(row["created_at"] or 0)
    return "expired" if age >= NEEDS_INPUT_TODO_HOLD_SECONDS else "active"


def _has_turner_hold(
    conn: sqlite3.Connection, task_id: str, block_kind: Optional[str],
    title: str, body: Optional[str],
) -> bool:
    """Only a needs_input hold younger than 2 hours pauses a todo/triage card."""
    return _needs_input_hold_state(conn, task_id, block_kind) == "active"


def _has_explicit_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """Newest ``blocked``/``unblocked`` event is an explicit worker/owner block.

    Triage release paths (operator unblock, reviewed-spec release) treat automatic
    breaker trips (``gave_up`` stamped ``sticky``) as advisory: they are not a hold an
    operator must clear first. ``recompute_ready`` keeps the upstream breaker
    (``kanban_db._has_sticky_block``) for automatic promotion of blocked cards.
    """
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row is not None and row["kind"] == "blocked"


def imported_block_released(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether a recovery event postdates the current blocked snapshot's boundary.

    Without a boundary row (pre-trigger board) any ``gave_up`` still counts, which is
    the legacy automatic-breaker release; older evidence never releases a newer block.
    """
    recovery = conn.execute(
        "SELECT 1 FROM task_events e JOIN task_block_entries b ON b.task_id = e.task_id "
        "WHERE e.task_id = ? AND e.id > b.event_id "
        "AND e.kind IN ('unblocked', 'gave_up', 'promoted_manual') LIMIT 1", (task_id,),
    ).fetchone()
    if recovery is not None:
        return True
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'gave_up' "
        "AND id > COALESCE((SELECT event_id FROM task_block_entries WHERE task_id = ?), 0) "
        "LIMIT 1", (task_id, task_id),
    ).fetchone() is not None


def _unblock_guards_refuse(
    conn: sqlite3.Connection, task_id: str, hold_row: Optional[sqlite3.Row], *,
    to_todo: bool, expected_blocked_event_id: Optional[int],
    require_idle: bool, require_closed_parents: bool,
) -> bool:
    """CAS/idle/parent preconditions an operator release was decided against."""
    from hermes_cli import kanban_db as kb

    if require_idle and hold_row is not None:
        if any(hold_row[key] is not None for key in (
            "current_run_id", "claim_lock", "claim_expires", "worker_pid",
        )) or conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL LIMIT 1", (task_id,),
        ).fetchone() is not None:
            return True
    if require_closed_parents and kb.unsatisfied_parents(conn, task_id):
        return True
    if to_todo or expected_blocked_event_id is not None:
        if hold_row is None or hold_row["status"] != "blocked":
            return True
        if expected_blocked_event_id is not None:
            latest = conn.execute(
                "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                "ORDER BY id DESC LIMIT 1", (task_id,),
            ).fetchone()
            if (latest["id"] if latest is not None else 0) != expected_blocked_event_id:
                return True
    return False


def _release_phase(
    conn: sqlite3.Connection, task_id: str, hold_row: Optional[sqlite3.Row],
) -> tuple[tuple[str, ...], str]:
    """Statuses the release may leave and the phase it resumes (``review`` when it left off there)."""
    from hermes_cli import kanban_db as kb

    held_todo = hold_row is not None and hold_row["status"] == "todo" and _has_turner_hold(
        conn, task_id, hold_row["block_kind"], hold_row["title"], hold_row["body"],
    )
    unblockable = ("todo",) if held_todo else ("blocked", "scheduled")
    resumes = held_todo or (hold_row is not None and hold_row["status"] == "blocked")
    return unblockable, (kb._resume_status_from_events(conn, task_id) if resumes else "ready")


def unblock_task(
    conn: sqlite3.Connection, task_id: str, *,
    actor: Optional[str] = None, reason: Optional[str] = None,
    to_todo: bool = False, expected_blocked_event_id: Optional[int] = None,
    require_idle: bool = False, require_closed_parents: bool = False,
) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first. A ``todo``
    row with a fresh ``needs_input`` hold is also accepted: the ``unblocked`` event
    releases it before its expiry. Triage holds require actor/reason and stay in
    triage for separate spec release (``kanban_db_triage_unblock.clear_triage_hold``).
    ``to_todo`` accepts only currently blocked cards and lands in todo regardless of
    parent completion. Expected event 0 asserts absent blocked history; positive IDs
    assert the latest blocked event; None leaves event history unguarded.
    ``require_idle`` refuses outstanding run/claim/worker bookkeeping or any open run;
    ``require_closed_parents`` refuses release while any parent is unfinished."""
    from hermes_cli import kanban_db as kb

    if expected_blocked_event_id is not None and expected_blocked_event_id < 0:
        raise ValueError("expected_blocked_event_id must be nonnegative")
    now = int(time.time())
    with kb.write_txn(conn):
        hold_row = conn.execute(
            "SELECT status, block_kind, title, body, current_run_id, claim_lock, "
            "claim_expires, worker_pid FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if _unblock_guards_refuse(
            conn, task_id, hold_row, to_todo=to_todo,
            expected_blocked_event_id=expected_blocked_event_id,
            require_idle=require_idle, require_closed_parents=require_closed_parents,
        ):
            return False
        if hold_row is not None and hold_row["status"] == "triage":
            from hermes_cli.kanban_db_triage_unblock import clear_triage_hold
            return clear_triage_hold(conn, task_id, hold_row, actor=actor, reason=reason, now=now)
        unblockable, resume_status = _release_phase(conn, task_id, hold_row)
        kb._reclaim_dangling_run(
            conn, task_id, statuses=unblockable, now=now, note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = kb._landing_status_after_parents(conn, task_id)
        new_status = "review" if landing_status == "ready" and resume_status == "review" else landing_status
        if to_todo:
            new_status, unblockable = "todo", ("blocked",)
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock (only
        # complete_task clears them); ``consecutive_failures`` IS reset — a deliberate
        # unblock is a fresh start for the retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            f"WHERE id = ? AND status IN ({', '.join('?' for _ in unblockable)})",
            (new_status, task_id, *unblockable),
        )
        if cur.rowcount != 1:
            return False
        if reason and actor:
            kb.add_comment(conn, task_id, actor, f"UNBLOCK: {reason}")
        kb._append_event(
            conn, task_id, "unblocked",
            {"status": new_status, "resume_status": resume_status}
            if new_status != "ready" or resume_status != "ready" else None,
        )
        return True
