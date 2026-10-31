"""Fleet lifecycle-fence recognition, per-row isolation and recovery ownership.

An external Fleet adapter may install SQLite triggers on a board that
``RAISE(ABORT, <exact message>)`` to protect rows this node holds no verified
remote lease for. Those are EXPECTED refusals, not bugs in the dispatcher, and
must be isolated per row rather than aborting an entire dispatcher tick. The
canonical row-ownership verdict (``kanban_db._fleet_row_ownership``) decides,
BEFORE any host-local PID probe or write, whether this node may recover a row
at all. Owner admission (``kanban_owner_recovery``) stays the execution
authority: ownership here only ever narrows what a reclaim path may touch.

Reached from the ``kanban_db_dispatch`` facade, which re-exports these names
for ``kanban_db`` and existing importers.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

# Deliberately an exact-string allowlist, not a blanket "catch IntegrityError":
# an IntegrityError whose message is NOT here (a NOT NULL failure, a UNIQUE
# collision, a foreign-key violation from real corruption) must never be
# silently swallowed as an expected fence. Keep this in exact sync with the
# adapter's own RAISE(ABORT, ...) literals.
_KNOWN_LIFECYCLE_FENCE_MESSAGES = frozenset({
    "verified execution lease required before running",
    "verified run generation no longer owns lifecycle write",
})

_FENCE_LOG_EVERY_SECONDS = 3600.0
_LEASE_WAIT_GRACE_SECONDS = 600.0  # Fleet sync normally records a new lease within minutes.
_CLAIM_RETRY_AFTER = {"foreign": 600.0, "lease_late": 300.0}
_FENCE_CACHE_MAX = 4096
_fence_logged: dict = {}  # (step, task_id) -> last WARNING epoch
_claim_fence: dict = {}  # task_id -> {"kind", "since", "last"}


def _trim_fence_cache(cache, now, timestamp):
    if len(cache) <= _FENCE_CACHE_MAX:
        return
    for key in list(cache):
        if now - timestamp(cache[key]) > 7200:
            cache.pop(key, None)
    # A burst of fresh cards must also stay bounded.
    for key in sorted(cache, key=lambda key: timestamp(cache[key]))[:max(0, len(cache) - _FENCE_CACHE_MAX)]:
        cache.pop(key, None)


def _fence_log_due(step, task_id, now=None):
    """True once per hour per (step, task): the first refusal is loud."""
    now = time.time() if now is None else now
    key = (step, task_id)
    last = _fence_logged.get(key)
    if last is not None and now - last < _FENCE_LOG_EVERY_SECONDS:
        return False
    _fence_logged[key] = now
    _trim_fence_cache(_fence_logged, now, lambda value: value)
    return True


def _is_known_lifecycle_fence(exc: BaseException) -> bool:
    """True iff ``exc`` is one of the adapter's own ``RAISE(ABORT, msg)`` refusals.

    A trigger ``RAISE`` surfaces as ``sqlite3.IntegrityError`` with extended code
    ``SQLITE_CONSTRAINT_TRIGGER`` and ``str()`` exactly ``msg``. Both must hold: a
    CHECK constraint merely NAMED after a fence phrase fails as
    ``CHECK constraint failed: <phrase>`` (``SQLITE_CONSTRAINT_CHECK``) and is a
    genuine constraint failure, not an expected refusal.
    """
    return (
        isinstance(exc, sqlite3.IntegrityError)
        and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_TRIGGER
        and str(exc) in _KNOWN_LIFECYCLE_FENCE_MESSAGES
    )


def _is_canonical_blocked_sync_pending(conn: sqlite3.Connection, task_id: str) -> bool:
    """True iff the Fleet mirror row says canonical ``blocked`` with sync ``pending``.

    The local row is ready only because an unblock has not synced yet; the
    lease fence refusing the claim is the fence working, so the dispatcher
    counts it as a hold rather than a stall. Plain boards (no adapter table)
    and any read error return False, preserving the stall signal.
    """
    try:
        row = conn.execute(
            "SELECT canonical_status, sync_state FROM fleet_kanban_issue_map "
            "WHERE local_task_id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    status, sync_state = row[0], row[1]
    return (
        isinstance(status, str) and status.strip().lower() == "blocked"
        and sync_state == "pending"
    )


def _isolate_fenced_row(
    errors_out: Optional[list],
    step: str,
    task_id: str,
    exc: sqlite3.Error,
    termination: Optional[dict] = None,
) -> bool:
    """Per-row verdict on a database error raised by one reclaim/claim write.

    A known lifecycle fence is expected authority: log at most hourly at WARNING,
    append ``(task_id, "<step>: <message>")`` to the caller-owned ``errors_out``
    and return True so the caller skips only this row (its own transaction
    already rolled back). Anything else returns False after an ERROR log and the
    caller re-raises: an unknown constraint or an ``OperationalError`` must abort
    the tick. When the worker was already signalled inside the failed
    transaction, the ERROR carries the termination report — the rolled-back
    write can no longer record it.
    """
    from hermes_cli import kanban_db as _kb

    if _is_known_lifecycle_fence(exc):
        log = _kb._log.warning if _fence_log_due(step, task_id) else _kb._log.debug
        log(
            "kanban %s: task %s left unchanged — lifecycle fence refused the write: %s; "
            "this machine holds no verified lease for the card yet; the fleet sync pass "
            "records it, normally within minutes. If this line comes again in an hour, "
            "check the fleet-kanban-sync log on this machine.",
            step, task_id, exc,
        )
        if errors_out is not None:
            errors_out.append((task_id, f"{step}: {exc}"))
        return True
    if termination and termination.get("termination_attempted"):
        _kb._log.error(
            "kanban %s: task %s worker was signalled but its release did not commit "
            "(termination=%r) — aborting the tick: %s", step, task_id, termination, exc,
        )
    else:
        _kb._log.error(
            "kanban %s: task %s raised %s, not a known lifecycle fence — aborting the tick: %s",
            step, task_id, type(exc).__name__, exc,
        )
    return False


def _recovery_owned_here(
    conn: sqlite3.Connection,
    errors_out: Optional[list],
    step: str,
    task_id: str,
    node_id: Optional[str],
) -> bool:
    """True iff this node may attempt a recovery write on ``task_id``.

    Runs BEFORE any host-local PID probe and before any write. On a synced Fleet
    board the local copy of another node's ``running`` card has no local claim,
    worker or lease: a PID check against it is meaningless (a foreign PID number
    can collide with a live local process) and the write is refused by the
    lifecycle fence on every tick. Ownership comes from
    ``kanban_db._fleet_row_ownership`` (canonical ``current_node`` /
    ``source_node`` against the installed adapter node id, cross-checked against
    the card's ``tenant`` home).

    * local   -> True (owner admission fences still apply afterwards).
    * foreign -> False, silently (debug log): the owner node recovers it.
    * unknown / contradictory -> False, but visible: a WARNING plus an
      ``errors_out`` entry, the row stays untouched. No trigger is bypassed and
      no lease is fabricated.
    """
    from hermes_cli import kanban_db as _kb

    owner, detail = _kb._fleet_row_ownership(conn, task_id, node_id)
    if owner == _kb.FLEET_OWNER_LOCAL:
        return True
    if owner == _kb.FLEET_OWNER_FOREIGN:
        _kb._log.debug(
            "kanban %s: task %s skipped — %s, recovery belongs to that node",
            step, task_id, detail,
        )
        return False
    log = _kb._log.warning if _fence_log_due("ownership", task_id) else _kb._log.debug
    log(
        "kanban %s: task %s left unchanged — fleet ownership cannot be verified (%s); "
        "check the fleet-kanban-sync log on this machine to repair the mapping",
        step, task_id, detail,
    )
    if errors_out is not None:
        errors_out.append((task_id, f"{step}: ownership unverified, left unchanged ({detail})"))
    return False


class _ReclaimAbortForLiveWorker(Exception):
    """Rolls back a reclaim transaction whose worker survived termination.

    A reclaim path that signals does so INSIDE the transaction holding its
    release UPDATE. If the worker is still alive afterwards, raising this rolls
    the release back — a claim is never released beside a live worker
    (duplicate spawn) — and the caller records the defer in its own
    transaction. Never escapes the reclaim path.
    """
