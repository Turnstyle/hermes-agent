"""Fleet respawn-guard policy: degree warnings, explicit holds, unresolved handoffs, and
current-assignee PR attribution lifted by the board-confined PR-state cache.

Behaviour behind the ``kanban_db_dispatch`` facade's ``check_respawn_guard``. Nothing here
changes card ownership or status: a degree warning is an advisory event and every hold here is
a skip, never a counted failure.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

_DEGREE_RESPAWN_REASONS = frozenset({"unchanged_block_reason", "blocker_auth"})
_DEGREE_NEXT_STEP = (
    "Assess your own confidence against the card; when uncertain use hermes jev evaluate --file "
    "<request.json> or a quick second model. Low confidence: send the card/evidence pointer through "
    "fleet handoff to a Decider; continue the existing card/PR, never create duplicate work. "
    "A stop is the last step; a Decider may release the advisory."
)


def _record_degree_warning(conn: sqlite3.Connection, task_id: str, reason: str, *, retry_seconds: int = 60) -> None:
    """One durable, visible advisory per reason/window; never changes card ownership or status."""
    from hermes_cli import kanban_db as _kb

    now = int(time.time())
    recent = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'degree_warning' "
        "AND created_at >= ? ORDER BY id DESC", (task_id, now - retry_seconds),
    ).fetchall()
    if any(_kb._json_dict(row["payload"]).get("reason") == reason for row in recent):
        return
    next_step = _DEGREE_NEXT_STEP
    if reason == "execution_lease":
        next_step = (
            "Run the installed fleet-kanban-sync owner-node sync/acquire pass; verify current-node "
            "execution lease and generation before retry. Never bypass the lease trigger or another "
            "writer. If ownership remains uncertain, send this card/lease pointer to a Decider. "
            + next_step
        )
    _kb._log.warning("LOUD WARNING: card %s: %s. %s", task_id, reason, next_step)
    with _kb.write_txn(conn, allow_nested=True):
        _kb._append_event(conn, task_id, "degree_warning", {
            "reason": reason, "next_step": next_step, "card_pointer": task_id,
            "retry_seconds": retry_seconds,
        })


def _handoff_held(conn, task_id: str, errors_out: Optional[list] = None, stage: str = "launch") -> bool:
    """True while a pre-spawn worker handoff is unresolved (``kanban_launch``): a worker may be
    running without a recorded pid, so nothing may respawn, release or requeue the card."""
    try:
        from hermes_cli.kanban_launch import handoff_pending
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_cli.kanban_launch":
            raise
        return False  # launch admission not installed: owner receipts are the only hold
    return handoff_pending(conn, task_id, errors_out=errors_out, stage=stage)


_EXPLICIT_DO_NOT_DISPATCH_MARKERS = (
    "do not dispatch",
    "king seat is already doing this",
)


def _explicit_do_not_dispatch_marked(conn: sqlite3.Connection, task_id: str) -> bool:
    """The NEWEST comment starts with an explicit hold marker. Titles and bodies often describe
    dispatch rules for other cards/profiles, so only a comment's leading words count."""
    from hermes_cli import kanban_db as _kb

    comment = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if comment is None:
        return False
    text = (_kb._lossy_text(comment["body"]) or "").strip().casefold()
    return text.startswith(_EXPLICIT_DO_NOT_DISPATCH_MARKERS)


def _blocked_event_reason(payload: Optional[str]) -> str:
    from hermes_cli import kanban_db as _kb

    data = _kb._json_or(payload, {})
    if not isinstance(data, dict):
        return ""
    return _kb._lossy_text(data.get("reason")) or ""


def _unchanged_blocked_reason(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the two newest ``blocked`` events share the same non-empty reason.

    A human ``unblocked`` event after the newest ``blocked`` event clears the
    guard so promote-and-respawn can proceed.
    """
    newest_blocked = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if newest_blocked is None:
        return False
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'unblocked' AND id > ? LIMIT 1",
        (task_id, newest_blocked["id"]),
    ).fetchone():
        return False
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 2",
        (task_id,),
    ).fetchall()
    if len(rows) < 2:
        return False
    newest, previous = (_blocked_event_reason(r["payload"]) for r in rows)
    return bool(newest) and newest == previous


def _verified_execution_lease(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('fleet_kanban_issue_map', 'fleet_kanban_verified_leases')"
    ).fetchone()[0]
    if tables != 2:
        return False
    return conn.execute(
        "SELECT 1 FROM tasks t JOIN fleet_kanban_issue_map im ON im.local_task_id=t.id "
        "JOIN fleet_kanban_verified_leases lease ON lease.issue_id=im.issue_id "
        "WHERE t.id=? AND lease.holder_profile=t.assignee "
        "AND lease.holder_node=COALESCE(im.current_node,im.source_node) "
        "AND lease.expires_at>? LIMIT 1", (task_id, now),
    ).fetchone() is not None


def _execution_lease_retry(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    """A claim the Fleet lease fence refused within 5 minutes holds the card until the lease
    is verified or a human/assignment/lease event follows the advisory."""
    from hermes_cli import kanban_db as _kb

    for notice in conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'degree_warning' "
        "AND created_at > ? ORDER BY id DESC", (task_id, now - 300),
    ).fetchall():
        if _kb._json_dict(notice["payload"]).get("reason") != "execution_lease":
            continue
        recovered = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
            "AND kind IN ('unblocked', 'assigned', 'lease_acquired') LIMIT 1", (task_id, notice["id"]),
        ).fetchone()
        return recovered is None and not _verified_execution_lease(conn, task_id, now)
    return False


def _upstream_active_pr(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    """Upstream rule without the fleet PR module: any PR URL in a recent comment holds the card
    unless a handoff event followed the newest one (strictly after; a same-second tie guards)."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kd

    pr_times = [t for t, c in _kd._rows_since(conn, "task_comments", task_id, now - _kd._RESPAWN_GUARD_PR_WINDOW,
                                              cols=", body", unreadable=now)  # duplicate-PR risk: fail closed
                if _kd._RESPAWN_GUARD_PR_URL_RE.search(_kb._lossy_text(c["body"]) or "")]
    if not pr_times:
        return False
    return not any(_kd._is_handoff_event(e["kind"], e["payload"]) for _, e in _kd._rows_since(
        conn, "task_events", task_id, max(pr_times),
        "AND kind IN ('assigned', 'changes_requested', 'review_reopened')", cols=", kind, payload", strict=True))


def _active_pr_guard(conn: sqlite3.Connection, row, task_id: str, now: int, lifted: Optional[list]) -> Optional[str]:
    """``"active_pr"`` while the CURRENT assignee's announced PR may still be open.

    Attribution (``kanban_pr_state.active_pr_keys``): only the assignee's own recent comments
    count, task-body links are inputs, an ``unblocked`` after the comment releases it. The
    board-confined PR-state cache lifts the hold once every PR is fresh MERGED/CLOSED evidence.
    """
    try:
        from hermes_cli.kanban_pr_state import active_pr_keys, all_terminal
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_cli.kanban_pr_state":
            raise
        return "active_pr" if _upstream_active_pr(conn, task_id, now) else None
    prs = active_pr_keys(conn, task_id, row["assignee"], row["body"], now)
    if not prs:
        return None
    if all_terminal(prs, now):
        if lifted is not None:
            lifted.append((task_id, "active_pr_lifted_terminal"))
        return None
    return "active_pr"


def _fleet_hold(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """Fleet holds that precede every upstream guard: none of them is ever counted."""
    if _handoff_held(conn, task_id, stage="respawn_guard"):
        return "worker_handoff_uncertain"
    if _explicit_do_not_dispatch_marked(conn, task_id):
        return "explicit_do_not_dispatch"
    if _unchanged_blocked_reason(conn, task_id):
        return "unchanged_block_reason"
    if _execution_lease_retry(conn, task_id, now):
        return "execution_lease_retry"
    return None


def _cooldown_guard(latest_run, rl_cooldown: int, now: int):
    """``(decided, reason)`` for the infrastructure / rate-limit cooldowns (upstream order)."""
    from hermes_cli import kanban_db as _kb

    if latest_run is None:
        return False, None
    outcome = latest_run["outcome"]
    ended_at = _kb._to_epoch(latest_run["ended_at"])
    if outcome == "spawn_failed" and rl_cooldown > 0 and _kb._json_dict(latest_run["metadata"]).get("infrastructure"):
        if ended_at is not None and (now - ended_at) < rl_cooldown:
            return True, "infrastructure_cooldown"
    if outcome == "rate_limited":
        # Cooldown disabled or elapsed: retry, skipping blocker_auth so the stamped
        # rate-limit text does not re-trap the task. Never counted.
        if rl_cooldown > 0 and ended_at is not None and (now - ended_at) < rl_cooldown:
            return True, "rate_limit_cooldown"
        return True, None
    return False, None


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
    lifted: Optional[list] = None,
) -> Optional[str]:
    """See ``kanban_db_dispatch.check_respawn_guard``."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kd

    row = conn.execute("SELECT assignee, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    now = int(time.time())
    hold = _fleet_hold(conn, task_id, now)
    if hold:
        return hold
    latest_run = conn.execute(
        "SELECT id, outcome, ended_at, metadata, error FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        # ``id`` breaks same-second ties so the newest run decides.
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    decided, reason = _cooldown_guard(latest_run, _kb._resolve_rate_limit_cooldown_seconds(), now)
    if decided:
        return reason
    # Quota / auth blocker, read from the LATEST run's own error: a leftover task-level
    # string from an older run is not this attempt's diagnosis. A plain crash's error carries
    # the worker's captured output, which is context, not a diagnosis (#117097).
    run_err = _kb._lossy_text(latest_run["error"]) if latest_run is not None else None
    if run_err and latest_run["outcome"] != "crashed" and _kd._RESPAWN_BLOCKER_RE.search(run_err):
        return "blocker_auth"
    if lane == "review":
        return None
    success = max((t for t, _ in _kd._rows_since(
        conn, "task_runs", task_id, now - _kd._RESPAWN_GUARD_SUCCESS_WINDOW, "AND outcome = 'completed'")), default=None)
    if success is not None and not any(_kd._rows_since(
            conn, "task_events", task_id, success, "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed')")):
        return "recent_success"
    return _active_pr_guard(conn, row, task_id, now, lifted)
