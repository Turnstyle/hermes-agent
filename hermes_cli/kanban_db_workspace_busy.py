"""Workspace exclusivity for claims (fleet carry K04, H24).

Two running lanes on one ``worktree``/``dir`` workspace overwrite each other's
files. A claim is refused (``claim_rejected`` / ``workspace_busy``) while another
RUNNING task holds the same normalized path; scratch and unspecified workspaces
are unaffected. Checked inside the claim's write transaction, before the run opens.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def _normalize_workspace_path(path: Optional[str]) -> str:
    """Normalize a workspace path for comparison (expand ~ and resolve symlinks)."""
    if not path:
        return ""
    expanded = os.path.expanduser(str(path).strip())
    try:
        return str(Path(expanded).resolve())
    except (OSError, RuntimeError):
        return os.path.normpath(expanded)


def _is_workspace_busy(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The normalized shared path when another RUNNING task holds this task's
    ``worktree``/``dir`` workspace, else None."""
    row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row or row["workspace_kind"] not in ("worktree", "dir") or not row["workspace_path"]:
        return None
    target = _normalize_workspace_path(row["workspace_path"])
    if not target:
        return None
    running = conn.execute(
        "SELECT workspace_path FROM tasks WHERE status = 'running' AND id != ? "
        "AND workspace_kind IN ('worktree', 'dir') AND workspace_path IS NOT NULL",
        (task_id,),
    ).fetchall()
    if any(_normalize_workspace_path(r["workspace_path"]) == target for r in running):
        return target
    return None


def reject_busy_workspace(conn: sqlite3.Connection, task_id: str) -> bool:
    """Record the refusal inside the claim transaction; True when the claim must stop."""
    from hermes_cli import kanban_db as kb

    busy = _is_workspace_busy(conn, task_id)
    if busy:
        kb._append_event(conn, task_id, "claim_rejected", {"reason": "workspace_busy", "workspace_path": busy})
    return bool(busy)
