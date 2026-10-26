"""Fleet-aware reclaim paths: canonical ownership before any probe, per-row fence isolation,
and no claim released beside a worker that survived termination.

Behaviour behind the ``kanban_db_dispatch`` facade's ``reconcile_orphaned_running``,
``detect_stale_running``, ``enforce_max_runtime`` and the crash sweep. The owner-admission
fences (``kanban_owner_recovery.owner_reclaim_blocked`` and its after-kill re-check) stay the
execution authority exactly as upstream wires them; fleet ownership, a lifecycle-fence refusal
and an unresolved pre-spawn handoff only ever narrow what a reclaim path may touch.

Facade-resident helpers are reached through ``_kd`` and origin helpers through ``_kb`` so
monkeypatching ``kanban_db_dispatch.<name>`` / ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from hermes_cli.kanban_db_dispatch_fence import _isolate_fenced_row, _recovery_owned_here
from hermes_cli.kanban_db_dispatch_guard import _handoff_held, _record_degree_warning


class _FenceProbeAccepted(Exception):
    """Rolls back a release UPDATE the lifecycle fence accepted (probe only)."""


_ORPHAN_RELEASE_SQL = (
    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
    "last_heartbeat_at = NULL "
    "WHERE id = ? AND status = 'running' "
    "  AND claim_lock IS ? AND claim_expires IS ?"
)


def _requeue_orphan(conn, row, now: int) -> bool:
    tid, pid = row["id"], row["worker_pid"]
    cur = conn.execute(_ORPHAN_RELEASE_SQL, (tid, row["claim_lock"], row["claim_expires"]))
    if cur.rowcount != 1:
        return False
    payload = {
        "reason": "orphaned_running",
        "claim_lock": row["claim_lock"],
        "claim_expires": _kb._to_epoch(row["claim_expires"]),
        "worker_pid": int(pid) if pid else None,
        "now": now,
    }
    run_id = _kb._end_run(
        conn, tid,
        outcome="reclaimed", status="reclaimed",
        error="orphaned running card (broken claim bookkeeping)",
        metadata=payload,
    )
    _kb._insert_comment(
        conn, tid, "dispatcher",
        "reconciliation: card was 'running' with no valid claim "
        "(dead/gone worker) — requeued to ready",
        now,
    )
    _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
    return True


def reconcile_orphaned_running(conn: sqlite3.Connection, *, errors_out: Optional[list] = None) -> list[str]:
    """See ``kanban_db_dispatch.reconcile_orphaned_running``."""
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked

    step = "reconcile_orphaned_running"
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    # Resolved once per call (one sqlite_master read), as in recompute_ready.
    node_id = _kb._fleet_adapter_installed_node_id(conn) if rows else None
    for row in rows:
        tid, pid = row["id"], row["worker_pid"]
        # Ownership BEFORE the receipt/PID probes or any write: a foreign node's mirrored
        # running row has no local claim, worker or lease to judge.
        if not _recovery_owned_here(conn, errors_out, step, tid, node_id):
            continue
        if owner_reclaim_blocked(conn, tid) or _handoff_held(conn, tid, errors_out, step):
            continue
        if pid and _kd._worker_alive(pid, _kb._row_get(row, "worker_started_at")):
            # Never requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        try:
            with _kb.write_txn(conn):
                if owner_reclaim_blocked(conn, tid) or _handoff_held(conn, tid, None, step):
                    continue
                if not _requeue_orphan(conn, row, now):
                    continue
        except sqlite3.Error as exc:
            if not _isolate_fenced_row(errors_out, step, tid, exc):
                raise
            continue
        # Counted only after the row's own transaction committed.
        reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


_STALE_RELEASE_SQL = (
    "UPDATE tasks SET status = ?, claim_lock = NULL, "
    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
    "last_heartbeat_at = NULL "
    "WHERE id = ? AND status = 'running' "
    "  AND claim_lock IS ?"
)


def _stale_candidate(row, now: int, stale_timeout_seconds: int):
    """``(elapsed, last_hb, hb_age)`` when the row is past the stale window, else None."""
    if row["active_started_at"] is None:
        return None
    elapsed = now - (_kb._to_epoch(row["active_started_at"]) or now)  # unreadable clock: never reclaim
    if elapsed < stale_timeout_seconds:
        return None
    last_hb = _kb._to_epoch(row["last_heartbeat_at"])
    hb_age = (now - last_hb) if last_hb is not None else None
    if hb_age is not None and hb_age < _kd._STALE_HEARTBEAT_GAP_SECONDS:
        return None
    return elapsed, last_hb, hb_age


def _book_stale(conn, row, window, stale_timeout_seconds: int, termination: dict) -> bool:
    elapsed, last_hb, hb_age = window
    tid, pid = row["id"], row["worker_pid"]
    retry_status = _kb._retry_status_for_run(conn, tid)
    cur = conn.execute(_STALE_RELEASE_SQL, (retry_status, tid, row["claim_lock"]))
    if cur.rowcount != 1:
        return False
    payload = {
        "elapsed_seconds": int(elapsed),
        "last_heartbeat_at": _kb._opt_int(last_hb),
        "heartbeat_age_seconds": _kb._opt_int(hb_age),
        "timeout_seconds": stale_timeout_seconds,
        "pid": int(pid) if pid else None,
        "retry_status": retry_status,
    }
    payload.update(termination)
    run_id = _kb._end_run(
        conn, tid,
        outcome="stale", status="stale",
        error=(
            f"no heartbeat for {int(hb_age)}s "
            if hb_age is not None
            else "no heartbeat ever"
        ) + f" after {int(elapsed)}s running",
        metadata=payload,
    )
    _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
    return True


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
    errors_out: Optional[list] = None,
) -> list[str]:
    """See ``kanban_db_dispatch.detect_stale_running``."""
    if stale_timeout_seconds <= 0:
        return []
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_blocked_after_kill

    step = "detect_stale_running"
    now = int(time.time())
    host_prefix = _kb._host_prefix()
    reclaimed: list[str] = []
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()
    for row in rows:
        tid, pid, lock = row["id"], row["worker_pid"], row["claim_lock"] or ""
        # Host-local claims only: another node's claim is outside this process's authority to
        # reclaim OR signal (its release would also trip the Fleet lifecycle fence every tick).
        if not lock.startswith(host_prefix) or owner_reclaim_blocked(conn, tid):
            continue
        window = _stale_candidate(row, now, stale_timeout_seconds)
        if window is None or _handoff_held(conn, tid, errors_out, step):
            continue
        started_at = _kb._row_get(row, "worker_started_at")
        if pid and _kd._worker_alive(pid, started_at):
            # A live worker with a stale heartbeat is an advisory, never a kill: the attempt may
            # be mid tool call. The degree warning makes it visible without moving the card.
            _record_degree_warning(conn, tid, "stale_heartbeat_live_owner")
            continue
        termination = _kb._terminate_reclaimed_worker(pid, lock, signal_fn=signal_fn, started_at=started_at)
        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _kd._worker_survived_termination(termination):
            _kd._defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination, reason="heartbeat_stale_worker_alive",
            )
            continue
        try:
            with _kb.write_txn(conn):
                # Again under the write lock, after the termination wait (see enforce_max_runtime).
                if owner_reclaim_blocked_after_kill(conn, tid, signalled=bool(termination.get("termination_attempted"))):
                    continue
                if not _book_stale(conn, row, window, stale_timeout_seconds, termination):
                    continue
        except sqlite3.Error as exc:
            if not _isolate_fenced_row(errors_out, step, tid, exc, termination):
                raise
            continue
        reclaimed.append(tid)
    return reclaimed


_TIMEOUT_RELEASE_SQL = (
    "UPDATE tasks SET status = ?, claim_lock = NULL, "
    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
    "last_heartbeat_at = NULL "
    "WHERE id = ? AND status = 'running' "
    "  AND worker_pid = ? AND claim_lock IS ?"
)


def _fence_accepts_release(conn, tid: str, pid: int, lock, errors_out: Optional[list]) -> bool:
    """Probe the timeout release under the board write lock and roll it back.

    A Fleet lifecycle fence refuses the UPDATE itself, so probing it BEFORE any signal keeps a
    refused row's worker alive: a reclaim path not authorized to release a claim must not kill
    the attempt holding it. The real release runs after the kill, under the upstream owner
    re-check; a fence that changes in between is isolated there.
    """
    try:
        with _kb.write_txn(conn):
            conn.execute(_TIMEOUT_RELEASE_SQL, (_kb._retry_status_for_run(conn, tid), tid, pid, lock))
            raise _FenceProbeAccepted()
    except _FenceProbeAccepted:
        return True
    except sqlite3.Error as exc:
        if not _isolate_fenced_row(errors_out, "enforce_max_runtime", tid, exc):
            raise
        return False


def _book_timeout(conn, row, pid: int, elapsed, limit: int, termination: dict) -> Optional[str]:
    """Release one timed-out run; returns its retry status, or None when the CAS lost."""
    tid = row["id"]
    retry_status = _kb._retry_status_for_run(conn, tid)
    cur = conn.execute(_TIMEOUT_RELEASE_SQL, (retry_status, tid, pid, row["claim_lock"]))
    if cur.rowcount != 1:
        return None
    payload: dict[str, Any] = {
        "pid": pid,
        "elapsed_seconds": int(elapsed),
        "limit_seconds": limit,
        "sigkill": bool(termination.get("sigkill")),
        "retry_status": retry_status,
    }
    run_id = _kb._end_run(
        conn, tid, outcome="timed_out", status="timed_out",
        error=f"elapsed {int(elapsed)}s > limit {limit}s", metadata=payload,
    )
    _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
    return retry_status


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None, errors_out: Optional[list] = None) -> list[str]:
    """See ``kanban_db_dispatch.enforce_max_runtime``."""
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_blocked_after_kill

    step = "enforce_max_runtime"
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        if owner_reclaim_blocked(conn, tid) or not (row["claim_lock"] or "").startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - (_kb._to_epoch(row["active_started_at"]) or now)  # unreadable clock: never kill
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue
        pid, started_at = int(row["worker_pid"]), _kb._row_get(row, "worker_started_at")
        if started_at == _kd.UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
            # Fingerprint capture failed at spawn: we cannot prove this live PID is our worker, so
            # it is neither signalled nor released beside (duplicate). It is reclaimed once it exits.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but has no verified "
                             "identity; not signalled", tid, pid)
            continue
        if not _fence_accepts_release(conn, tid, pid, row["claim_lock"], errors_out):
            continue
        # SIGTERM, short grace, SIGKILL; a recycled PID (fingerprint mismatch) is never signalled.
        termination = _kb._terminate_reclaimed_worker(pid, row["claim_lock"], signal_fn=signal_fn, started_at=started_at)
        if _kd._worker_survived_termination(termination):
            # Releasing now would spawn a duplicate beside the survivor: hold the claim, retry.
            _kd._defer_reclaim_for_live_worker(
                conn, tid, row["claim_lock"], now, termination, reason="max_runtime_worker_alive",
            )
            continue
        try:
            with _kb.write_txn(conn):
                # Again under the write lock, after the kill wait: an interpreter that bound
                # meanwhile is invisible to the pid/claim CAS below. Host-local by the filter above.
                if owner_reclaim_blocked_after_kill(conn, tid, signalled=True):
                    continue
                retry_status = _book_timeout(conn, row, pid, elapsed, limit, termination)
        except sqlite3.Error as exc:
            if not _isolate_fenced_row(errors_out, step, tid, exc, termination):
                raise
            continue
        if retry_status is None:
            continue
        timed_out.append(tid)
        # Own txn, after the release committed. If the breaker trips this flips the task to
        # ``blocked`` and emits ``gave_up`` on top of the ``timed_out`` already emitted.
        _kd._record_task_failure(
            conn, tid,
            error=f"elapsed {int(elapsed)}s > limit {limit}s",
            outcome="timed_out",
            release_claim=False,
            end_run=False,
            event_payload_extra={"pid": pid, "sigkill": bool(termination.get("sigkill")), "retry_status": retry_status},
        )
    return timed_out


_CRASH_RELEASE_SQL = (
    "UPDATE tasks SET status = ?, claim_lock = NULL, "
    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
    "WHERE id = ? AND status = 'running' "
    "  AND worker_pid = ? AND claim_lock IS ?"
)


def _book_dead_worker(conn, sweep, row, dead, board) -> bool:
    """Release one dead worker's card and record it in ``sweep``; False when the CAS lost."""
    pid = int(row["worker_pid"])
    retry_status = _kb._retry_status_for_run(conn, row["id"])
    dead.event_payload["retry_status"] = retry_status
    cur = conn.execute(_CRASH_RELEASE_SQL, (retry_status, row["id"], pid, row["claim_lock"]))
    if cur.rowcount != 1:
        return False
    run_id = _kb._end_run(
        conn, row["id"],
        outcome=dead.run_outcome, status=dead.run_outcome,
        error=dead.error_text,
        metadata=dict(dead.event_payload),
    )
    _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
    sweep.exited_hook_payloads.append({
        "task_id": row["id"], "assignee": row["assignee"], "run_id": run_id, "worker_pid": pid,
        "exit_kind": dead.kind, "exit_code": dead.code, "outcome": dead.run_outcome,
        "retry_status": retry_status,
    })
    if dead.rate_limited or dead.protocol_violation:
        # Stamp last_failure_error WITHOUT touching ``consecutive_failures``: a rate-limited
        # requeue must show ``check_respawn_guard`` a quota blocker; a below-budget protocol
        # violation never reaches ``_record_task_failure`` (which stamps this column), yet the
        # board UI and retry worker need the corrective message.
        conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", (dead.error_text[:500], row["id"]))
    return True


def reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None, *, errors_out: Optional[list] = None):
    """See ``kanban_db_dispatch._reclaim_dead_workers``."""
    from hermes_cli.kanban_budget_handback import BUDGET_HANDBACK, KANBAN_BUDGET_HANDBACK_EXIT_CODE, handback_dead_worker
    from hermes_cli.kanban_owner_recovery import managed_run_exit_code, owner_reclaim_blocked

    sweep = _kd._CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            if not (row["claim_lock"] or "").startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._to_epoch(_kb._row_get(row, "started_at"))
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _kd._worker_alive(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                continue
            # An ambiguous receipt holds the card while the attempt may still run; once nothing
            # can be executing it (a dead bound interpreter, or a run that never bound one): crash.
            if owner_reclaim_blocked(conn, row["id"], crash_sweep=True):
                continue
            pid = int(row["worker_pid"])
            # A managed run's authority-recorded exit (its bound interpreter's, under this claim).
            exit_code = managed_run_exit_code(conn, row["id"], pid, row["claim_lock"])
            if exit_code == KANBAN_BUDGET_HANDBACK_EXIT_CODE:
                # The owner's receipt records a neutral turn-budget hand-back: same release, no crash.
                dead = handback_dead_worker(conn, row["id"], pid, row["claim_lock"])
            else:
                dead = _kd._classify_dead_worker(pid, row["claim_lock"], exit_code, task_id=row["id"], board=board)
            try:
                # Per-row savepoint: a lifecycle-fence refusal rolls back only this row, never
                # the siblings already released in this sweep.
                with _kb.write_txn(conn, allow_nested=True):
                    if not _book_dead_worker(conn, sweep, row, dead, board):
                        continue
            except sqlite3.Error as exc:
                if not _isolate_fenced_row(errors_out, "detect_crashed_workers", row["id"], exc):
                    raise
                continue
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            elif dead.kind != BUDGET_HANDBACK:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep


def _renew_claim(conn, task_id: str, row, run_id, claimer: Optional[str], now: int, warnings: list) -> bool:
    """Extend the claim inside the heartbeat's transaction; False on a known holder conflict."""
    lock = claimer or _kb._claimer_id()
    if row["claim_lock"] is not None and row["claim_lock"] != lock:
        return False
    if run_id is None:
        warnings.append("heartbeat run bookkeeping missing; no known run conflict")
    if row["claim_lock"] is None:
        warnings.append("heartbeat lease missing; liveness recorded without claim renewal")
        return True
    expires = now + _kb._resolve_claim_ttl_seconds(None)
    conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (expires, task_id))
    if run_id is not None:
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (expires, run_id))
    return True


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    extend_claim: bool = False,
    claimer: Optional[str] = None,
    warnings_out: Optional[list] = None,
) -> bool:
    """See ``kanban_db_dispatch.heartbeat_worker``."""
    now = int(time.time())
    warnings: list[str] = []
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            return False
        run_id = row["current_run_id"]
        # A stale run never touches its successor; only a renewing worker whose run row is
        # missing may proceed (warning-only: missing bookkeeping is not a foreign owner).
        if expected_run_id is not None and (run_id is not None or not extend_claim) and int(expected_run_id) != run_id:
            return False
        if extend_claim:
            if expected_run_id is None and run_id is not None:
                warnings.append("heartbeat run bookkeeping missing; no known run conflict")
            if not _renew_claim(conn, task_id, row, run_id, claimer, now, warnings):
                return False
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (now, task_id))
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(conn, task_id, "heartbeat", {"note": note} if note else None, run_id=run_id)
    if warnings_out is not None:
        warnings_out.extend(warnings)
    return True


from hermes_cli import kanban_db as _kb  # noqa: E402 -- late-bound, see module docstring
from hermes_cli import kanban_db_dispatch as _kd  # noqa: E402
