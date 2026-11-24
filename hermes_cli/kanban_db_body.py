"""Bounded native parent body compare-and-set, without lifecycle transitions."""
from __future__ import annotations

from hermes_cli.kanban_db_parent import parent_assignment_is_live

BODY_MAX_BYTES = 256 * 1024


def compare_and_set_body(conn, task_id: str, *, old_body: str, new_body: str,
                         caller_assignment: dict) -> dict:
    """Exact strings only; done/archived cards require the existing lifecycle route.

    NULL bodies do not equal an empty string. Conflicts and refusals append no
    event. Body, audit, and readback share one IMMEDIATE transaction.
    """
    from hermes_cli import kanban_db as kb

    for name, value in (("old_body", old_body), ("new_body", new_body)):
        if not isinstance(value, str) or len(value.encode('utf-8')) > BODY_MAX_BYTES:
            raise ValueError(f"{name} must be a string of at most {BODY_MAX_BYTES} UTF-8 bytes")
    if old_body == new_body:
        raise ValueError("new_body must differ from old_body")
    with kb.write_txn(conn):
        if not parent_assignment_is_live(conn, caller_assignment):
            raise ValueError("native parent assignment is stale or outside this board")
        target = kb.get_task(conn, task_id)
        tenant_matches = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND typeof(tenant) IN ('text', 'null') "
            "AND tenant IS ? COLLATE BINARY",
            (task_id, caller_assignment.get('tenant')),
        ).fetchone()
        if target is None or not tenant_matches:
            raise ValueError("task is outside the native parent tenant")
        if target.status in ('done', 'archived'):
            raise ValueError("terminal task body edit refused; use the existing lifecycle route")
        # Board readers intentionally decode corrupt TEXT/BLOB values lossily.
        # Authorization and CAS must instead compare canonical storage bytes.
        changed = conn.execute(
            "UPDATE tasks SET body=? WHERE id=? AND typeof(body)='text' "
            "AND CAST(body AS BLOB)=CAST(? AS BLOB)",
            (new_body, task_id, old_body),
        ).rowcount
        if changed != 1:
            return {'ok': False, 'code': 'body_conflict', 'error': 'old_body does not match current body',
                    'task_id': task_id}
        kb._append_event(conn, task_id, 'edited', {
            'fields': ['body'], 'caller_task_id': caller_assignment['task_id'],
            'caller_run_id': caller_assignment['run_id'],
        })
        return {'ok': True, 'task_id': task_id, 'body': kb.get_task(conn, task_id).body}
