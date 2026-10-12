"""Operator hold release for triage cards, separate from spec promotion."""

from __future__ import annotations

import sqlite3
from typing import Optional


def clear_triage_hold(
    conn: sqlite3.Connection, task_id: str, row: sqlite3.Row, *,
    actor: Optional[str], reason: Optional[str], now: int,
) -> bool:
    """Run inside unblock_task's write transaction; never promote or rewrite a spec."""
    from hermes_cli import kanban_db as kb

    installed_node_id = kb._fleet_adapter_installed_node_id(conn)
    if kb._is_foreign_fleet_mirror(conn, task_id, installed_node_id):
        raise ValueError('foreign fleet mirror cannot be unblocked locally')

    if row['block_kind'] == 'needs_input' and kb._has_turner_hold(
        conn, task_id, row['block_kind'], row['title'], row['body'],
    ):
        cleared_hold = 'needs_input'
    elif kb._has_unreleased_block_loop(conn, task_id):
        cleared_hold = 'block_loop_detected'
    elif kb._has_sticky_block(conn, task_id):
        cleared_hold = 'sticky_block'
    else:
        return False

    clean_actor = (actor or '').strip()
    clean_reason = (reason or '').strip()
    if not clean_actor or not clean_reason:
        raise ValueError('triage hold clear requires a non-empty actor and reason')

    kb._append_event(conn, task_id, 'unblocked', {
        'status': 'triage', 'cleared_hold': cleared_hold,
        'actor': clean_actor, 'reason': clean_reason,
    })
    kb._insert_comment(
        conn, task_id, clean_actor,
        f'UNBLOCK: {clean_reason} (cleared {cleared_hold} by {clean_actor}; kept in triage).', now,
    )
    return True
