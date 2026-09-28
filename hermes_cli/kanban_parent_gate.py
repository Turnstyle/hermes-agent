"""Verified parent gate for Kanban task completion.

Completing a child while a direct parent is still open is allowed only when every
open parent is explicitly released: a system ``rehomed`` / ``superseded`` event
that names a live successor task, or a structured PR URL (``completion_contract``
or ``pr_acceptance`` event) verified merged via ``gh``. Human sticky holds and
unknown merge state keep the gate closed; free-text titles, comments, and note
fields never count.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from typing import Any

_PR_MERGE_CACHE_TTL_SECONDS = 120.0
_pr_merge_cache: dict[str, tuple[float, bool]] = {}


def record_parent_rehome(
    conn: sqlite3.Connection,
    parent_id: str,
    successor_id: str,
    *,
    action: str = "rehomed",
) -> bool:
    """Append the canonical parent rehome/supersede event; the only system writer."""
    if action not in ("rehomed", "superseded"):
        raise ValueError(f"action must be 'rehomed' or 'superseded', got {action!r}")
    parent_row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (parent_id,)).fetchone()
    successor_row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (successor_id,)).fetchone()
    if parent_row is None or successor_row is None:
        return False
    from hermes_cli.kanban_db import _append_event, write_txn

    with write_txn(conn):
        _append_event(conn, parent_id, action, {"successor_id": successor_id})
    return True


def query_pr_merge_state(url: str) -> dict[str, Any] | None:
    """Subprocess seam: ``gh pr view`` for ``state`` and ``mergedAt``."""
    from hermes_cli._subprocess_compat import windows_hide_flags

    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "check": False,
    }
    creationflags = windows_hide_flags()
    if creationflags:
        popen_kwargs["creationflags"] = creationflags
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state,mergedAt"],
            **popen_kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def clear_pr_merge_cache() -> None:
    """Clear the process-local PR merge verification cache."""
    _pr_merge_cache.clear()


def parent_gate_allows_completion(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    allow_network: bool = True,
) -> bool:
    from hermes_cli.kanban_db import unsatisfied_parents

    open_parents = unsatisfied_parents(conn, task_id)
    if not open_parents:
        return True
    for parent_id, status in open_parents:
        if not _parent_release(conn, parent_id, status, allow_network=allow_network):
            return False
    return True


# A parent in ``review`` is waiting on an approval gate (request_review writes
# no sticky block), so no rehome or merged-PR evidence may release it.
_NEVER_RELEASE_STATUSES = frozenset({"review"})
# A ``blocked`` parent may be a hold; only a verified rehome releases it.
_NO_MERGE_RELEASE_STATUSES = frozenset({"review", "blocked"})


def _parent_release(
    conn: sqlite3.Connection,
    parent_id: str,
    status: str | None,
    *,
    allow_network: bool,
) -> bool:
    from hermes_cli.kanban_db import _has_sticky_block

    if status in _NEVER_RELEASE_STATUSES:
        return False
    if _has_sticky_block(conn, parent_id):
        return False
    if _verified_parent_rehome(conn, parent_id):
        return True
    if status in _NO_MERGE_RELEASE_STATUSES:
        return False
    return _verified_merged_pr_parent(conn, parent_id, allow_network=allow_network)


def _verified_parent_rehome(conn: sqlite3.Connection, parent_id: str) -> bool:
    from hermes_cli.kanban_db import _json_dict

    rows = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND kind IN ('rehomed', 'superseded')",
        (parent_id,),
    ).fetchall()
    for row in rows:
        payload = _json_dict(row["payload"])
        successor_id = payload.get("successor_id")
        if not isinstance(successor_id, str) or not successor_id.strip():
            continue
        exists = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (successor_id,),
        ).fetchone()
        if exists is not None:
            return True
    return False


def _structured_pr_url(conn: sqlite3.Connection, parent_id: str) -> str | None:
    from hermes_cli.kanban_pr_acceptance import _PR

    row = conn.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?", (parent_id,),
    ).fetchone()
    if row is not None:
        contract = row["completion_contract"]
        if isinstance(contract, str):
            match = _PR.fullmatch(contract)
            if match:
                return contract
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance' "
        "ORDER BY id DESC LIMIT 1",
        (parent_id,),
    ).fetchone()
    if event is None:
        return None
    from hermes_cli.kanban_db import _json_dict

    pr_url = _json_dict(event["payload"]).get("pr_url")
    if not isinstance(pr_url, str):
        return None
    if _PR.fullmatch(pr_url):
        return pr_url
    return None


def _verified_merged_pr_parent(
    conn: sqlite3.Connection,
    parent_id: str,
    *,
    allow_network: bool,
) -> bool:
    url = _structured_pr_url(conn, parent_id)
    if not url:
        return False
    now = time.monotonic()
    cached = _pr_merge_cache.get(url)
    if cached is not None and now < cached[0]:
        return cached[1]
    if not allow_network:
        return False
    state = query_pr_merge_state(url)
    merged = _gh_state_is_merged(state)
    if state is not None:
        _pr_merge_cache[url] = (now + _PR_MERGE_CACHE_TTL_SECONDS, merged)
    return merged


def _gh_state_is_merged(state: dict[str, Any] | None) -> bool:
    if state is None:
        return False
    merged_at = state.get("mergedAt")
    return state.get("state") == "MERGED" and isinstance(merged_at, str) and bool(merged_at.strip())
