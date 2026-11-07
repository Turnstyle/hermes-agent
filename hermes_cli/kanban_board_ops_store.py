"""Additive Board Ops admission/outbox records in the authoritative board DB.

No tasks are copied here. Request rows bind existing tasks and events; their
terminal receipt and task mutation commit in the same checked transaction.
"""

from __future__ import annotations

import hashlib
import json

from hermes_cli.kanban_board_ops_policy import Refused, canonical

_DDL = (
    "CREATE TABLE IF NOT EXISTS kanban_board_ops_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "contract TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL, cursor INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS kanban_board_ops_grants (id TEXT PRIMARY KEY, contract TEXT NOT NULL, "
    "digest TEXT NOT NULL, revoked_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS kanban_board_ops_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "correlation TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL REFERENCES tasks(id), "
    "grant_id TEXT NOT NULL REFERENCES kanban_board_ops_grants(id), request TEXT NOT NULL, "
    "snapshot TEXT NOT NULL, original_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
    "receipt TEXT, due_at INTEGER, delivery_claimed_at INTEGER, delivery_result TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_board_ops_due ON kanban_board_ops_requests(state,due_at,id)",
)


def installed(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_board_ops_control'").fetchone() is not None


def initialize(conn) -> None:
    for sql in _DDL:
        conn.execute(sql)


def digest(data: dict) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def control(conn, board: str, *, require_enabled: bool = True) -> dict:
    if not installed(conn):
        raise Refused("Board Ops has no owner admission")
    row = conn.execute("SELECT * FROM kanban_board_ops_control WHERE singleton=1").fetchone()
    if row is None or (require_enabled and row["enabled"] != 1):
        raise Refused("Board Ops is disabled")
    data = json.loads(row["contract"])
    if data["board"] != board:
        raise Refused("persisted control belongs to another board")
    return {**data, "revision": row["revision"], "enabled": bool(row["enabled"]), "cursor": row["cursor"]}


def grant(conn, grant_id: str) -> dict:
    row = conn.execute("SELECT * FROM kanban_board_ops_grants WHERE id=?", (grant_id,)).fetchone()
    if row is None:
        raise Refused("grant not found")
    data = json.loads(row["contract"])
    if digest(data) != row["digest"]:
        raise Refused("grant content digest differs")
    return {**data, "digest": row["digest"], "revoked_at": row["revoked_at"]}


def state_snapshot(conn, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise Refused("task not found")
    task = dict(row)
    parents = [dict(r) for r in conn.execute(
        "SELECT l.parent_id,p.status FROM task_links l LEFT JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=? ORDER BY l.parent_id", (task_id,),
    )]
    run = conn.execute("SELECT * FROM task_runs WHERE id=?", (task["current_run_id"],)).fetchone()
    # Board Ops intake/receipt comments cannot invalidate their own request.
    event = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind NOT LIKE 'board_ops_%' "
        "AND kind != 'commented'", (task_id,),
    ).fetchone()[0]
    return {"task": task, "parents": parents, "run": dict(run) if run else None, "lifecycle_event": event}


def receipt(conn, correlation: str) -> dict | None:
    row = conn.execute("SELECT * FROM kanban_board_ops_requests WHERE correlation=?", (correlation,)).fetchone()
    if row is None:
        return None
    return {
        "correlation": correlation, "task_id": row["task_id"], "state": row["state"],
        "original_event_at": row["original_at"],
        "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
        "delivery_result": json.loads(row["delivery_result"]) if row["delivery_result"] else None,
    }
