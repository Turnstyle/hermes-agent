"""Transactional board moves and fail-closed dispatcher worker authority."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def _unblock_in_txn(conn, task_id: str, audit: dict | None = None) -> bool:
    """Restore the resumable phase. Caller owns the write transaction."""
    from hermes_cli.kanban_db import (
        _resume_status_from_events, _task_status, _reclaim_dangling_run,
        _landing_status_after_parents, _append_event,
    )
    now = int(time.time())
    resume_status = (
        _resume_status_from_events(conn, task_id)
        if _task_status(conn, task_id) == "blocked"
        else "ready"
    )
    _reclaim_dangling_run(
        conn, task_id, statuses=("blocked", "scheduled"), now=now,
        note="invariant recovery on unblock",
    )
    # Re-gate on parent completion before restoring the source phase.
    landing_status = _landing_status_after_parents(conn, task_id)
    new_status = (
        "review"
        if landing_status == "ready" and resume_status == "review"
        else landing_status
    )
    # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
    # resetting them is the amnesia that let cron-unblock <-> re-block loop
    # unbounded; only complete_task clears them. ``consecutive_failures``
    # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
    # is a fresh start for the retry budget.
    cur = conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = NULL, "
        "consecutive_failures = 0, last_failure_error = NULL "
        "WHERE id = ? AND status IN ('blocked', 'scheduled')", (new_status, task_id),
    )
    if cur.rowcount != 1:
        return False
    payload = (
        {"status": new_status, "resume_status": resume_status}
        if new_status != "ready" or resume_status != "ready" else {}
    )
    payload.update(audit or {})
    _append_event(conn, task_id, "unblocked", payload or None)
    return True


def _require(condition, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _worker_pins(board: str | None) -> tuple[str, int, str, str, Path]:
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import cfg_get, load_config_readonly
    from hermes_cli.profiles import current_profile_name

    _require(is_dispatcher_owned_worker_context(), "not a dispatcher-owned parent worker")
    _require(cfg_get(load_config_readonly(), "kanban", "worker_board_moves", default=False) is True,
             "kanban.worker_board_moves must be boolean true")
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    lock = (os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
    db = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    slug = kb._normalize_board_slug(os.environ.get("HERMES_KANBAN_BOARD"))
    profile = current_profile_name()
    _require(task and lock and db and slug and profile, "missing dispatcher identity or board pins")
    _require(board is None or kb._normalize_board_slug(board) == slug,
             "explicit board does not match dispatcher board pin")
    pinned = Path(db).expanduser().resolve()
    _require(kb.kanban_db_path(board=board).resolve() == pinned, "dispatcher database mismatch")
    run = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "0")
    _require(run > 0, "missing dispatcher run pin")
    return task, run, lock, kb._canonical_assignee(profile), pinned


def _verify_worker(conn, board: str | None) -> dict:
    """Check runtime pins and persisted process ownership. Mutation callers hold write_txn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    from hermes_cli.kanban_db_connect import _main_db_file

    task, run, lock, profile, pinned = _worker_pins(board)
    actual = _main_db_file(conn)
    _require(actual and Path(actual).resolve() == pinned, "connection is not dispatcher database")
    source = conn.execute("SELECT * FROM tasks WHERE id = ?", (task,)).fetchone()
    attempt = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    _require(source is not None and attempt is not None, "source task or run missing")
    _require(source["status"] == "running" and source["current_run_id"] == run
             and attempt["task_id"] == task and attempt["status"] == "running"
             and attempt["ended_at"] is None, "source is not the current open running run")
    _require(source["assignee"] == profile and attempt["profile"] == profile,
             "source profile changed or mismatched")
    _require(lock.startswith(kb._host_prefix()) and source["claim_lock"] == lock
             and attempt["claim_lock"] == lock, "source claim lock mismatched")
    now = int(time.time())
    _require((source["claim_expires"] or 0) > now and (attempt["claim_expires"] or 0) > now,
             "source lease expired")
    pid = os.getpid()
    fingerprint = _process_fingerprint(pid)
    _require(fingerprint and source["worker_pid"] == pid and attempt["worker_pid"] == pid
             and source["worker_started_at"] == fingerprint
             and attempt["worker_started_at"] == fingerprint, "source process owner mismatched")
    return {"actor": profile, "actor_profile": profile,
            "source_task_id": task, "source_run_id": run}


def worker_moves_visible() -> bool:
    """Probe ownership without initializing or migrating any database."""
    try:
        *_, pinned = _worker_pins(None)
        with closing(sqlite3.connect(pinned.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            _verify_worker(conn, None)
        return True
    except Exception:
        return False


def worker_board_move(conn, task_id: str, *, move: str, board: str | None = None,
                      expected_blocked_event: int | None = None,
                      evidence: str | None = None, reason: str | None = None) -> str:
    """Authorize and move another card atomically, retaining the descendant write fence."""
    from hermes_cli import kanban_db as kb

    _require(move in ("unblock", "promote"), "unsupported worker board move")
    with kb.write_txn(conn):
        audit = _verify_worker(conn, board)
        _require(task_id != audit["source_task_id"], "workers cannot move their own source card")
        validate_board_move_text(evidence=evidence, reason=reason, require_evidence=move == "unblock")
        if move == "unblock":
            target = _idle_target(conn, task_id)
            _require(target["status"] == "blocked", "worker unblock requires blocked status")
            _require(type(expected_blocked_event) is int and expected_blocked_event > 0,
                     "expected_blocked_event must be a positive integer, not bool")
            blocked = conn.execute("SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                                   "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            _require(blocked and blocked["id"] == expected_blocked_event, "blocked event changed")
            claim = conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'claimed' "
                                 "AND id > ? LIMIT 1", (task_id, expected_blocked_event)).fetchone()
            _require(not claim, "target was claimed after the blocked event")
            audit.update(expected_blocked_event=expected_blocked_event,
                         evidence=kb.redact_review_value(evidence.strip()))
            _require(_unblock_in_txn(conn, task_id, audit), "target changed during unblock")
        else:
            audit["reason"] = kb.redact_review_value(reason)
            _promote_todo_in_txn(conn, task_id, audit)
        return kb._task_status(conn, task_id)


BOARD_MOVE_TEXT_MAX = 4000


def validate_board_move_text(*, evidence=None, reason=None, require_evidence: bool = False) -> None:
    """Reject malformed/oversized audit text before redaction; never truncate."""
    for name, value in (("evidence", evidence), ("reason", reason)):
        _require(value is None or isinstance(value, str), f"{name} must be a string or None")
        _require(value is None or len(value) <= BOARD_MOVE_TEXT_MAX,
                 f"{name} must be at most {BOARD_MOVE_TEXT_MAX} characters")
    if require_evidence:
        _require(isinstance(evidence, str) and evidence.strip(), "evidence is required")


def _idle_target(conn, task_id: str):
    from hermes_cli import kanban_db as kb

    target = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    _require(target is not None, "target task not found")
    open_run = conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL",
                            (task_id,)).fetchone()
    _require(not target["claim_lock"] and not target["current_run_id"] and not open_run,
             "target has a claim or open run")
    _require(not target["worker_pid"] or not kb._worker_alive(
        target["worker_pid"], target["worker_started_at"]), "target worker is still live")
    return target


def _promote_todo_in_txn(conn, task_id: str, audit: dict) -> None:
    """Safe native promotion for either caller; caller owns the write transaction."""
    from hermes_cli import kanban_db as kb

    target = _idle_target(conn, task_id)
    _require(target["status"] == "todo", "native promote requires todo status; use kanban_unblock for blocked cards")
    _require(kb._resume_status_from_events(conn, task_id) != "review",
             "native promote cannot bypass review resumption")
    _require(kb._parents_satisfied(conn, task_id), "unsatisfied parent dependencies")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'", (task_id,))
    kb._append_event(conn, task_id, "promoted_manual", audit)


def promote_todo_task(conn, task_id: str, *, actor: str, reason: str | None = None) -> str:
    """Native orchestrator tool promotion; human CLI promote_task is unchanged."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        validate_board_move_text(reason=reason)
        _promote_todo_in_txn(conn, task_id, {"actor": actor, "reason": kb.redact_review_value(reason)})
        return kb._task_status(conn, task_id)
