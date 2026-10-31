"""Dependency promotion (todo/blocked -> ready) with the fleet fences (fleet carry K04).

Promotion is automatic, node-local bookkeeping. It never touches:
- a card outside the active Board Ops composed transaction (``promotion_in_scope``);
- a Fleet mirror of another node's card (``_is_foreign_fleet_mirror``);
- a todo card under a fresh ``needs_input`` hold;
- an imported blocked snapshot without a recovery event after its block boundary.
Each card runs in its own savepoint so one failing card cannot discard the tick.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import sqlite3
import time

_log = logging.getLogger("hermes_cli.kanban_db")


@contextlib.contextmanager
def _card_write_savepoint(conn: sqlite3.Connection):
    """Roll back this card's writes without discarding other cards in the tick."""
    name = "card_" + secrets.token_hex(8)
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        raise
    finally:
        conn.execute(f"RELEASE {name}")


def _blocked_card_held(conn: sqlite3.Connection, task_id: str, row: sqlite3.Row, failure_limit: int) -> bool:
    """Explicit/sticky blocks, imported snapshots and the breaker limit keep a card blocked."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_holds import imported_block_released

    if kb._has_sticky_block(conn, task_id):
        # Explicit block, or a trip the counter cannot judge (terminal provider/credential,
        # protocol-violation budget, systemic wave): only ``unblock_task`` may exit it.
        return True
    if not imported_block_released(conn, task_id):
        # Older evidence cannot release the current imported blocked snapshot.
        return True
    # At the breaker limit, no auto-recovery (else block -> recover -> respawn ->
    # exhaust -> block forever). The counter is preserved across cycles.
    limit = int(row["max_retries"]) if row["max_retries"] is not None else int(failure_limit)
    return int(row["consecutive_failures"] or 0) >= limit


def _promote_card(conn: sqlite3.Connection, row: sqlite3.Row, installed_node_id, failure_limit: int) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_board_ops_transactions import promotion_in_scope

    task_id, cur_status = row["id"], row["status"]
    if not promotion_in_scope(conn, task_id):
        return False
    # Ownership FIRST: a foreign-owned mirror is out of scope for automatic promotion
    # (no write, no event) whatever its local status or dependency state.
    if kb._is_foreign_fleet_mirror(conn, task_id, installed_node_id):
        return False
    if cur_status == "todo" and kb._has_turner_hold(conn, task_id, row["block_kind"], row["title"], row["body"]):
        return False
    if cur_status == "blocked" and _blocked_card_held(conn, task_id, row, failure_limit):
        return False
    parents = conn.execute(
        "SELECT t.status FROM tasks t JOIN task_links l ON l.parent_id = t.id WHERE l.child_id = ?",
        (task_id,),
    ).fetchall()
    if not all(p["status"] in ("done", "archived") for p in parents):
        return False
    resume_status = kb._resume_status_from_events(conn, task_id)
    cur = conn.execute(
        "UPDATE tasks SET status = ? WHERE id = ? AND status = ?", (resume_status, task_id, cur_status),
    )
    if cur.rowcount != 1:
        return False
    if cur_status == "todo" and kb._needs_input_hold_state(conn, task_id, row["block_kind"]) == "expired":
        # The hold expired unanswered: the card moves on, visibly, exactly once
        # (it is no longer todo afterwards).
        kb._insert_comment(
            conn, task_id, "dispatcher",
            "NEEDS-INPUT HOLD EXPIRED: nobody answered within 2 hours, so this card moves on. "
            "The assignee takes the recommended option written in the card. "
            f'To hold it again run: hermes kanban block {task_id} "<reason>" --kind needs_input',
            int(time.time()),
        )
    kb._append_event(
        conn, task_id, "promoted", {"status": resume_status} if resume_status != "ready" else None,
    )
    return True


def recompute_ready(conn: sqlite3.Connection, failure_limit: int | None = None) -> int:
    """Promote ``todo``/``blocked`` tasks whose parents are all done/archived;
    returns the count. Opens its own IMMEDIATE txn — call OUTSIDE any write txn
    (a Board Ops composed transaction nests it as a savepoint instead).

    ``blocked`` is skipped when sticky (explicit ``kanban_block``), when an imported
    blocked snapshot has no later recovery event, or when ``consecutive_failures``
    reached the limit. Limit order matches ``_record_task_failure``: ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``.
    """
    from hermes_cli import kanban_db as kb

    if failure_limit is None:
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    promoted = 0
    with kb.write_txn(conn):
        installed_node_id = kb._fleet_adapter_installed_node_id(conn)
        rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries, block_kind, title, body "
            "FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in rows:
            try:
                with _card_write_savepoint(conn):
                    promoted += int(_promote_card(conn, row, installed_node_id, failure_limit))
            except sqlite3.Error:
                _log.warning(
                    "kanban recompute_ready: card %s failed; rolled back this card and continuing tick",
                    row["id"], exc_info=True,
                )
    return promoted
