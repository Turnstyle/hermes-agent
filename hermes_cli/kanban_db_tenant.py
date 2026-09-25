"""Tenant-edit guards for boards with the external Fleet adapter installed."""

import sqlite3


def refuse_active_fleet_lease(conn: sqlite3.Connection, task_id: str) -> None:
    """Check inside the caller's write transaction to fence the pre-claim window."""
    lease = conn.execute(
        "SELECT 1 FROM fleet_kanban_verified_leases AS lease "
        "JOIN fleet_kanban_issue_map AS issue ON issue.issue_id = lease.issue_id "
        "WHERE issue.local_task_id = ? "
        "AND lease.expires_at > CAST(strftime('%s', 'now') AS INTEGER) LIMIT 1",
        (task_id,),
    ).fetchone()
    if lease is not None:
        raise ValueError(f"cannot rehome {task_id}: an unexpired fleet lease is mirrored for this task")
