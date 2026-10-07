"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import signal
import socket as _socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

from hermes_cli.kanban_launch import handoff_pending
from hermes_cli.quiet_single_query import KANBAN_WORKER_BUSY_MARKER, KANBAN_WORKER_EXIT_TRAILER

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and make a terminal board call (kanban_block/kanban_complete/kanban_request_review)
# before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# A healthy worker is still alive for a while after kanban_complete /
# kanban_request_review returns (final assistant turn, session persistence), so
# a run's retained worker is only reaped once ended_at is at least this old
# (two default dispatch ticks).
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Backoff after a ``profile_busy`` requeue (worker refused its profile's only active-session
# slot, t_76b0d62d): 60s, doubling per consecutive busy run, capped at 15 min. Never counts a
# failure, so it must be bounded here instead of by the breaker. Overridable via
# ``HERMES_KANBAN_PROFILE_BUSY_BACKOFF_SECONDS`` / ``..._BACKOFF_MAX_SECONDS``.
DEFAULT_PROFILE_BUSY_BACKOFF_SECONDS = 60
DEFAULT_PROFILE_BUSY_BACKOFF_MAX_SECONDS = 900
# From this many consecutive busy runs on, each ``profile_busy`` event carries ``long_busy``
# and the card's error line names the streak, so a long hold stays visible on the board.
PROFILE_BUSY_LONG_STREAK = 6

# Bound the hold when the fleet PR-state cache is not refreshed.
_RESPAWN_GUARD_PR_WINDOW = 7200  # 2 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


def _respawn_guard_pr_key(match: re.Match[str]) -> tuple[str, str, int]:
    """Compare PR links by owner, repo and number, independent of URL spelling."""
    owner, repo, _, number = match.group(0).casefold().split("github.com/", 1)[1].split("/")
    return owner, repo, int(number)


_EXPLICIT_DO_NOT_DISPATCH_MARKERS = (
    "do not dispatch",
    "king seat is already doing this",
)


_GUARD_NEXT_STEP = {
    "explicit_do_not_dispatch": (
        'post a newer comment without a hold marker: '
        'hermes kanban comment {task_id} "go ahead"'
    ),
    "active_pr": (
        "this releases by itself 2 hours after the assignee's PR comment, "
        "or when every PR in it is merged or closed. To release now, reassign "
        "to a different profile: hermes kanban reassign {task_id} <different-profile>"
    ),
}


def _active_pr_keys(conn: sqlite3.Connection, task_id: str, assignee: Optional[str],
                    task_body: Optional[str], now: int) -> set[tuple[str, str, int]]:
    """PRs behind the existing ready-lane hold, without consulting the network/cache."""
    if not assignee:
        return set()
    input_prs = {
        _respawn_guard_pr_key(match)
        for match in _RESPAWN_GUARD_PR_URL_RE.finditer(_kb._lossy_text(task_body) or "")
    }
    guarded: set[tuple[str, str, int]] = set()
    for c in conn.execute(
        "SELECT author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (task_id, now - _RESPAWN_GUARD_PR_WINDOW),
    ).fetchall():
        if c["author"] != assignee:
            continue
        prs = {
            key for match in _RESPAWN_GUARD_PR_URL_RE.finditer(_kb._lossy_text(c["body"]) or "")
            if (key := _respawn_guard_pr_key(match)) not in input_prs
        }
        if not prs:
            continue
        created_at = int(c["created_at"] or 0)
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND created_at > ? "
            "AND kind = 'unblocked' LIMIT 1", (task_id, created_at),
        ).fetchone():
            continue
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND created_at > ? "
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened')",
            (task_id, created_at),
        ).fetchall()
        if any(_is_handoff_event(e["kind"], e["payload"]) for e in events):
            return guarded
        guarded.update(prs)
    return guarded

# ---------------------------------------------------------------------------
# Lifecycle-guard fence recognition
# ---------------------------------------------------------------------------
#
# An external adapter may install SQLite triggers on a board that
# ``RAISE(ABORT, <exact message>)`` to protect rows this node holds no verified
# remote lease for. Those are EXPECTED refusals, not bugs in this module, and
# must be isolated per row rather than aborting an entire dispatcher tick.
#
# Deliberately an exact-string allowlist, not a blanket "catch IntegrityError":
# an IntegrityError whose message is NOT here (a NOT NULL failure, a UNIQUE
# collision, a foreign-key violation from real corruption) must never be
# silently swallowed as an expected fence. Keep this in exact sync with the
# adapter's own RAISE(ABORT, ...) literals — a message that used to match and
# stops matching is precisely the case this allowlist protects against
# mis-classifying.
_KNOWN_LIFECYCLE_FENCE_MESSAGES = frozenset({
    "verified execution lease required before running",
    "verified run generation no longer owns lifecycle write",
})


_FENCE_LOG_EVERY_SECONDS = 3600.0
_LEASE_WAIT_GRACE_SECONDS = 600.0  # Fleet sync normally records a new lease within minutes.
_CLAIM_RETRY_AFTER = {"foreign": 600.0, "lease_late": 300.0}
_fence_logged = {}  # (step, task_id) -> last WARNING epoch
_claim_fence = {}  # task_id -> kind, since, last


def _trim_fence_cache(cache, now, timestamp):
    if len(cache) <= 4096:
        return
    for key in list(cache):
        if now - timestamp(cache[key]) > 7200:
            cache.pop(key, None)
    # A burst of fresh cards must also stay bounded.
    for key in sorted(cache, key=lambda key: timestamp(cache[key]))[:max(0, len(cache) - 4096)]:
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


def _claim_fence_errors(result, kind, since, now):
    if kind == "foreign":
        return result.foreign_claim_errors
    if kind == "held" or now - since < _LEASE_WAIT_GRACE_SECONDS:
        return result.held_claim_errors
    return result.eligible_claim_errors


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

    A known lifecycle fence is expected authority: log at most hourly at WARNING, append
    ``(task_id, "<step>: <message>")`` to the caller-owned ``errors_out`` and
    return True so the caller skips only this row (its own transaction or
    savepoint already rolled back). Anything else returns False after an ERROR
    log and the caller re-raises: an unknown constraint or an ``OperationalError``
    must abort the tick. When the worker was already signalled inside the failed
    transaction, the ERROR carries the termination report — the rolled-back
    write can no longer record it.
    """
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

    Runs BEFORE any host-local PID probe and before any write (t_23c40d89). On a
    synced Fleet board the local copy of another node's ``running`` card has no
    local claim, worker or lease: a PID check against it is meaningless (a
    foreign PID number can collide with a live local process) and the write is
    refused by the lifecycle fence on every tick. Ownership comes from
    :func:`kanban_db._fleet_row_ownership` (canonical ``current_node`` /
    ``source_node`` against the installed adapter node id, cross-checked against
    the card's ``tenant`` home).

    * local   -> True.
    * foreign -> False, silently (debug log): the owner node recovers it.
    * unknown / contradictory -> False, but visible: a WARNING plus an
      ``errors_out`` entry (so it surfaces as a reclaim refusal), the row stays
      untouched. No trigger is bypassed and no lease is fabricated.
    """
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

    Every reclaim path that signals does so INSIDE the transaction holding its
    fence-guarded release UPDATE, after the UPDATE was accepted. If the worker
    is still alive afterwards, raising this rolls the release back — a claim is
    never released beside a live worker (duplicate spawn) — and the caller
    records the defer in its own transaction. Never escapes the reclaim path.
    """


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    reclaim_phase: Optional[str] = None
    """Set when the reclaim and promotion phase was skipped for a dry run."""
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    reclaim_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, "<step>: <reason>")`` for a pending worker handoff or a write
    an installed lifecycle-guard trigger refused this tick; ``step`` names the sweep
    (``release_stale_claims``, ``reconcile_orphaned_running``,
    ``detect_stale_running``, ``detect_crashed_workers``,
    ``enforce_max_runtime``). Each row is left completely
    unchanged — no partial write, no termination signal — and is NOT counted in
    ``reclaimed``/``reconciled_orphans``/``stale``/``crashed``/``timed_out``.
    Surfaced here instead of raised so the tick continues to sibling
    rows and later dispatch steps. Purely observability: never a trigger to
    bypass the fence, which stays enforced until the owner-side lease is
    reconciled elsewhere."""
    claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, "claim: <fence message>")`` for each ready/review row whose
    CLAIM write a lifecycle-guard trigger refused this tick. The row stayed
    unclaimed and is not counted in ``spawned``; later rows still ran."""
    foreign_claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """Refusals and retry-backoff skips for a foreign Fleet mirror."""
    held_claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, "claim: <fence message>")`` for a node-owned card whose
    canonical Fleet status is ``blocked`` with sync still ``pending``, or in
    the first 10 minutes of waiting for its lease. Includes backoff skips:
    these are holds, not dispatcher failures."""
    eligible_claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """Refusals and retry-backoff skips for a node-owned, dispatch-enabled
    card whose lease wait has exceeded the grace period."""
    spawn_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, str(exc))`` for each eligible card whose worker spawn failed
    with an exception this tick."""
    reaped_terminal_workers: list[str] = field(default_factory=list)
    """Task ids whose worker outlived its closed run and was terminated by
    :func:`reap_terminal_workers`."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a
    misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids that had ``kanban.default_assignee`` applied this
    tick before spawning, so telemetry/CLI/dashboard can show the dispatcher
    acting on the fallback rule rather than explicit assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    owner_unavailable: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, assignee)`` subset of ``skipped_nonspawnable`` whose assignee
    is neither a configured ``kanban.control_plane_lanes`` entry nor a profile
    outside this home's ``kanban.dispatch_profiles`` — i.e. the owning profile
    is missing/unavailable here. Operator-actionable; the card carries one
    deduped ``owner_unavailable`` event until it is claimed or reassigned."""
    skipped_placeholder: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, assignee)`` subset of ready tasks assigned to a placeholder profile
    (default, alpha, beta, orch) without dispatch enabled. Reported as a separate non-paging
    count so health telemetry does not alarm."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    respawn_guard_lifted: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` ready-lane PR holds lifted by fresh terminal cache evidence."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    profile_busy: list[str] = field(default_factory=list)
    """Task ids whose workers were refused their profile's active-session slot
    (the profile's one session was held elsewhere) and were released WITHOUT
    counting a failure, behind a bounded backoff (t_76b0d62d)."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    capacity_held: Optional[str] = None
    """Concurrency cap that stopped every spawn this tick, e.g.
    ``"host cap: 12 running of 12"`` (``max_in_progress``, all boards) or
    ``"board cap: 3 running of 3"`` (``max_spawn``). ``None`` = no cap hold.
    Only claimed ``running`` rows count (fleet mirrors of other nodes' cards
    do not). Deferred tasks stay queued."""


def describe_suppression(results: Iterable[Optional["DispatchResult"]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher, which otherwise report a
    bare zero-spawn count while ``hermes kanban tail`` is the only place the
    guard reason is written (#111910).
    """
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    capacity: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if getattr(res, "profile_busy", None):
            counts["profile_busy"] = counts.get("profile_busy", 0) + len(res.profile_busy)
        if getattr(res, "skipped_per_profile_capped", None):
            counts["profile_capped"] = counts.get("profile_capped", 0) + len(res.skipped_per_profile_capped)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
        if getattr(res, "capacity_held", None):
            capacity = res.capacity_held
        if getattr(res, "held_claim_errors", None):
            counts["lease_held"] = counts.get("lease_held", 0) + len(res.held_claim_errors)
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    if capacity:
        parts.append(capacity)
    return ", ".join(parts)


def eligible_work_stalled(
    results: Iterable[Any],
    ready_spawnable: int,
) -> bool:
    """Determine whether eligible work itself failed to start this tick.

    Returns True if there was eligible ready work that could not spawn due to
    dispatcher failure (e.g. lease refusal, claim error, spawn failure, or
    unheld ready work failing to start), rather than being held back by policy
    guards (active_pr, recent_success, rate_limited), capacity limits (host/board
    caps, memory pressure), profile busy/concurrency caps, or placeholder profiles.
    """
    res_list: list["DispatchResult"] = []
    for item in results:
        if item is None:
            continue
        if isinstance(item, tuple) and len(item) == 2 and (isinstance(item[0], str) or item[0] is None):
            res = item[1]
        else:
            res = item
        if res is not None and isinstance(res, DispatchResult):
            res_list.append(res)

    # 1. Explicit auto-blocked failure or spawn exception on eligible cards.
    # An explicit spawn failure or auto-block must NEVER be masked by worker spawns
    # on other cards, capacity holds on other boards, or the card being removed to blocked status.
    has_spawn_or_blocked_errors = any(
        bool(getattr(r, "auto_blocked", None)) or bool(getattr(r, "spawn_errors", None))
        for r in res_list
    )
    if has_spawn_or_blocked_errors:
        return True

    # 2. If there are no eligible ready cards on this node, the dispatcher cannot be stalled.
    # Foreign Fleet mirrors or placeholder cards may produce expected claim refusals,
    # but they do not represent eligible work for this node.
    if ready_spawnable <= 0:
        return False

    # 3. Explicit claim/lease refusal on node-owned, dispatch-enabled cards.
    # Must never be masked by spawns on other cards or capacity holds on other boards.
    def _has_eligible_claim_errors(r: Any) -> bool:
        if getattr(r, "eligible_claim_errors", None):
            return True
        # Foreign mirrors and lease-held cards (canonical blocked, sync
        # pending) are expected fence refusals, never dispatcher failures.
        excluded_ids = {tid for tid, _ in (getattr(r, "foreign_claim_errors", None) or [])}
        excluded_ids |= {tid for tid, _ in (getattr(r, "held_claim_errors", None) or [])}
        if excluded_ids:
            return any(tid not in excluded_ids for tid, _ in (getattr(r, "claim_errors", None) or []))
        return bool(getattr(r, "claim_errors", None))

    if any(_has_eligible_claim_errors(r) for r in res_list):
        return True

    if not res_list:
        return True

    # 4. If any worker spawned this tick and no eligible cards explicitly failed, the dispatcher made progress.
    if any(bool(r.spawned) for r in res_list):
        return False

    # 3. Host-wide capacity or critical memory pressure halts all spawns host-wide.
    host_capacity_held = any(
        (getattr(r, "capacity_held", None) or "").startswith("host cap")
        or getattr(r, "memory_pressure", None) == "critical"
        for r in res_list
    )
    if host_capacity_held:
        return False

    # 4. If all reporting boards are held by board capacity caps or locked DBs,
    # then no board had capacity to dispatch.
    if all(bool(getattr(r, "capacity_held", None)) or bool(getattr(r, "skipped_locked", False)) for r in res_list):
        return False

    # 5. Account for all legitimately held spawnable tasks (profile caps, profile busy, rate limits).
    # Note: respawn_guarded (active_pr, recent_success, etc.) tasks are already excluded from
    # ready_spawnable by _count_spawnable and must NEVER be subtracted from ready_spawnable here;
    # doing so would allow an unrelated policy-held card to hide an eligible card's failure.
    held_spawnable_count = 0
    for r in res_list:
        if getattr(r, "capacity_held", None) or getattr(r, "skipped_locked", False):
            continue
        held_spawnable_count += len(getattr(r, "skipped_per_profile_capped", []) or [])
        held_spawnable_count += len(getattr(r, "profile_busy", []) or [])
        held_spawnable_count += len(getattr(r, "rate_limited", []) or [])
        # Lease-held cards (canonical blocked, sync pending) are still counted
        # by _count_spawnable; the fence refusing them is a hold.
        held_spawnable_count += len(getattr(r, "held_claim_errors", []) or [])

    return ready_spawnable > held_spawnable_count


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap. Process-local by nature (``waitpid`` only reaps our own
# children): a per-tick ``hermes kanban dispatch`` process finds it empty, so
# ``_classify_dead_worker_exit`` falls back to the exit trailer the worker
# leaves in its own log (``KANBAN_WORKER_EXIT_TRAILER``).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}

# Windows has no ``waitpid(-1)``: a child's exit code is only recoverable
# through a live handle, so ``_default_spawn`` parks each worker's ``Popen``
# here (Windows only) and ``reap_worker_zombies`` polls it. Entry: ``pid -> Popen``.
_live_worker_procs: "dict[int, subprocess.Popen]" = {}
# Wall-clock time the dispatcher reaped each worker IT spawned (a pid present in
# ``_live_worker_procs`` at reap time). Other children of this process (terminal
# commands, git, ps) never enter it. Upper bound for the crash-path orphan
# group reap (``_vet_orphaned_worker_group``).
_worker_reaped_at: "dict[int, float]" = {}


def _track_worker_proc(proc: "subprocess.Popen") -> None:
    """Keep a spawned worker's ``Popen`` until the dispatcher reaps it (see ``_live_worker_procs``).
    A leftover handle for the same pid (its worker was reaped by another thread before it was
    tracked) is marked finished first, so its ``__del__`` never polls the new worker's pid."""
    stale = _live_worker_procs.get(int(proc.pid))
    if stale is not None and stale is not proc and stale.returncode is None:
        stale.returncode = -1
    _worker_reaped_at.pop(int(proc.pid), None)
    _live_worker_procs[int(proc.pid)] = proc


def _note_worker_reaped(pid: int, raw_status: int) -> None:
    """The dispatcher reaped ``pid``: record its exit status and, when it is a worker this process
    spawned, the reap time. Marks the parked ``Popen`` finished so its ``__del__`` never waits on a
    PID that may already belong to another process."""
    _record_worker_exit(pid, raw_status)
    proc = _live_worker_procs.pop(int(pid), None)
    if proc is None:
        return
    if proc.returncode is None:
        try:
            proc.returncode = os.waitstatus_to_exitcode(int(raw_status))
        except (AttributeError, ValueError):
            proc.returncode = -1
    now = time.time()
    _worker_reaped_at[int(pid)] = now
    if len(_worker_reaped_at) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, t in _worker_reaped_at.items() if t < cutoff]:
            _worker_reaped_at.pop(_pid, None)


def _wait_status_from_returncode(returncode: int) -> int:
    """Encode a ``Popen.returncode`` in the wait-status layout the registry stores."""
    return (int(returncode) & 0xFF) << 8


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None)."""
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    # Bit-level POSIX wait-status decode instead of os.WIFEXITED/WEXITSTATUS/
    # WIFSIGNALED/WTERMSIG: those helpers do not exist on Windows, where the
    # registry is fed by reap_worker_zombies' Popen poll. Low 7 bits = signal
    # (0 = normal exit, 0x7F = stopped), bits 8-15 = exit code.
    raw = int(raw)
    signal_number = raw & 0x7F
    if signal_number == 0:
        return _exit_code_kind((raw >> 8) & 0xFF)
    if signal_number != 0x7F:
        return ("signaled", signal_number)
    return ("unknown", None)


def _exit_code_kind(code: int) -> "tuple[str, int]":
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)


_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)


def _worker_log_exit_code(task_id: str, board: Optional[str] = None) -> Optional[int]:
    """Exit code from the trailer the worker CLI wrote to its own log; None when absent.

    The durable twin of ``_recent_worker_exits``: written by the worker itself
    (``hermes_cli.quiet_single_query.exit_single_query``), so it is there whether
    or not the process running this sweep ever reaped the worker. Last trailer
    wins — the log is append-mode across re-runs.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return None
    matches = _EXIT_TRAILER_RE.findall(raw or "")
    return int(matches[-1]) if matches else None


# Whole line only: a tool that echoes the marker mid-line never matches.
_BUSY_MARKER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_BUSY_MARKER) + r"([A-Z_]+) run=(\d+)\s*$", re.MULTILINE,
)

# The only refusal reason booked ``profile_busy``: the active-session CAP. An ownership refusal
# (another live writer owns the session) or an unprovable registry is not a capacity wait.
_PROFILE_BUSY_REASON = "MAX_CONCURRENT_SESSIONS"


def _worker_log_busy_for_run(task_id: str, run_id: Optional[int], board: Optional[str] = None) -> bool:
    """True when the worker's own log carries the busy marker for exactly ``run_id``.

    The worker writes it (``quiet_single_query.write_worker_busy_marker``) only when it was
    refused its profile's session slot by the cap and before any turn ran. Bound to the run id,
    so a marker an earlier run left in the append-mode log never classifies a later run, and
    printing the human refusal phrase (which a real crash may do) is never enough.
    """
    if not task_id or run_id is None:
        return False
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return False
    return any(
        reason == _PROFILE_BUSY_REASON and int(run) == int(run_id)
        for reason, run in _BUSY_MARKER_RE.findall(raw or "")
    )


def reap_worker_zombies() -> "list[int]":
    """Reap exited workers without blocking; returns reaped PIDs. POSIX reaps
    every child via ``waitpid(-1)``; Windows polls the ``Popen`` handles
    parked by ``_default_spawn`` (the only way to learn a child's exit code
    there), so the rate-limit sentinel exit is classified on both hosts."""
    reaped: "list[int]" = []
    if _kb._IS_WINDOWS:
        for pid, proc in list(_live_worker_procs.items()):
            returncode = proc.poll()
            if returncode is None:
                continue
            _note_worker_reaped(pid, _wait_status_from_returncode(returncode))
            reaped.append(pid)
        return reaped
    try:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            _note_worker_reaped(pid, status)
            reaped.append(pid)
    except Exception:
        pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Uses ``gateway.status._pid_exists`` (OpenProcess on Windows, ``os.kill(pid, 0)``
    on POSIX). **DO NOT** call ``os.kill(pid, 0)`` directly on Windows — there
    ``sig=0`` is ``CTRL_C_EVENT`` broadcast to the console group, potentially
    killing unrelated processes.

    Zombies (exited, not yet reaped) still pass the existence check, so a
    worker would look "alive" forever between exit and reap. Linux: peek at
    ``/proc/<pid>/status`` and treat ``State: Z`` as dead; macOS: ask ``ps``
    for the BSD ``stat`` field and treat ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


# ``worker_started_at`` value for a spawn whose fingerprint could not be captured. Distinct from the
# NULL legacy row (pre-fingerprint spawn): such a worker is held (its claim is never released beside
# the live PID) but NEVER signalled — missing process identity is refusal, not permission (#99558).
UNVERIFIED_WORKER_FINGERPRINT = "unverified"


def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<instantiation epoch>|<start time>"``. The start
    time alone (``/proc/<pid>/stat`` field 22 on Linux) is clock ticks since THIS boot, so a row that
    survives a reboot could match an unrelated process with the same PID and the same tick value;
    ``gateway.drain_control.current_instantiation_epoch`` (``boot_id`` + PID-1 start) changes on every
    reboot / container recreate, so the composed value never survives one. ``None`` when unreadable."""
    from gateway.drain_control import current_instantiation_epoch
    from gateway.status import get_process_start_time
    start = get_process_start_time(int(pid))
    if start is None:
        return None
    return f"{current_instantiation_epoch()}|{start}"


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned. ``started_at`` is the fingerprint
    recorded by ``_set_worker_pid``; after a reboot (or any PID recycle) an unrelated process can own
    the number, so bare existence is never enough to extend a claim or to signal. A legacy row without
    a fingerprint keeps the existence answer: killing it is the pre-fingerprint behaviour and the row is
    rewritten with a fingerprint on its next spawn. An UNVERIFIED spawn also keeps the existence answer
    (a claim is never released beside a possibly-live worker) but ``_terminate_reclaimed_worker``
    refuses to signal it."""
    if not _kb._pid_alive(pid):
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    return not _pid_recycled(pid, started_at)


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is NOT the process fingerprinted at spawn (or the fingerprint can no
    longer be read). Signalling it would hit a stranger. ``None`` fingerprint = legacy row, never
    recycled; the UNVERIFIED marker is always foreign. An integer fingerprint (rows written before the
    boot witness was added) compares the start time only."""
    if started_at is None or not pid:
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    if isinstance(started_at, str) and "|" in started_at:
        return _process_fingerprint(int(pid)) != started_at
    from gateway.status import _start_times_agree, get_process_start_time
    current = get_process_start_time(int(pid))
    if current is None:
        return True
    try:
        return not _start_times_agree(current, started_at)
    except (TypeError, ValueError):
        return True


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int, started_at: Optional[int] = None) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _worker_alive(pid, started_at):
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _worker_leads_own_group(pid: int, started_at, signal_fn) -> bool:
    """True when the worker ``pid`` is live, fingerprint-verified AND leads its own process group, so
    ``os.killpg(pid, sig)`` reaches the worker and every child it started in its own group (e.g. an
    external ``claude`` / ``codex`` CLI started with plain ``subprocess.Popen``) without touching
    anything else. ``_default_spawn`` starts workers
    with ``start_new_session=True``, so the group id equals the PID. Only the exact equality counts:
    the group is NEVER derived as ``killpg(getpgid(pid))`` — a worker sharing the dispatcher's group
    would make that kill the dispatcher itself. Group signals need a real spawn fingerprint: a legacy
    row (``None``), the UNVERIFIED marker, a zombie or a recycled PID keep the single-PID path.
    POSIX only (Windows keeps single-PID termination), and only with real signals: a ``signal_fn``
    test hook keeps the single-PID behaviour. Children that start their own session leave the group
    and are out of reach here: daemons, MCP servers, and every Hermes terminal-tool command
    (foreground and background run with ``start_new_session=True``; a ``hermes`` worker's own SIGTERM
    handler ends its foreground terminal group)."""
    if signal_fn is not None or _kb._IS_WINDOWS:
        return False
    if not (hasattr(os, "killpg") and hasattr(os, "getpgid")):
        return False
    if started_at is None or started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return False
    if not _kb._pid_alive(pid) or _pid_recycled(pid, started_at):
        return False
    try:
        return os.getpgid(int(pid)) == int(pid)
    except OSError:
        return False


def _signal_worker_group(pid: int, sig) -> bool:
    """``os.killpg(pid, sig)``; True when delivered. ESRCH (group already empty) / EPERM → False."""
    try:
        os.killpg(int(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _live_group_members(pgid: int) -> Optional[list[int]]:
    """PIDs of NON-zombie processes in group ``pgid`` (``ps -A -o pid=,pgid=,stat=``, same on macOS
    and Linux). A zombie leader that its real parent has not reaped yet is not a member worth
    waiting for or killing. ``None`` when ``ps`` cannot be read: callers then assume members remain
    (fail toward finishing the kill, never toward skipping it)."""
    try:
        proc = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None  # unreadable: never read as "no members"
    out = proc.stdout
    members: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            member, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == int(pgid) and not parts[2].startswith("Z"):
            members.append(member)
    return members


def _worker_group_has_live_members(pgid: int) -> bool:
    members = _live_group_members(pgid)
    return True if members is None else bool(members)


def _reap_exited_leader(pid: int) -> None:
    """Reap the worker leader when it is our own exited child, recording its exit status exactly as
    ``reap_worker_zombies`` would, so the dispatcher's exit classification is unchanged. Not our
    child / still running → no-op."""
    try:
        reaped, status = os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError):
        return
    if reaped == int(pid):
        _note_worker_reaped(reaped, status)


def _poll_worker_group_exit(pid: int, started_at=None) -> bool:
    """Same ~5 s grace as ``_poll_worker_exit``, but ends early only when the leader AND every live
    group member are gone, so children get the full grace to exit on SIGTERM. A zombie leader waiting
    on a parent that is not us does not hold the poll open. Returns whether the leader exited."""
    for _ in range(10):
        if not _worker_alive(pid, started_at):
            _reap_exited_leader(pid)
            if not _worker_group_has_live_members(pid):
                return True
        time.sleep(0.5)
    return not _worker_alive(pid, started_at)


def _worker_exit_reaped_at(pid: int) -> Optional[float]:
    """Wall-clock time the dispatcher in THIS process reaped worker ``pid`` that it had itself
    spawned and tracked (``_worker_reaped_at``), or None: another process spawned it (per-tick CLI
    dispatcher, gateway restarted since the spawn), a test spawn_fn that bypasses
    ``_track_worker_proc``, or the entry expired. Until that reap the PID stays pinned by the zombie,
    so no other process can hold it; it is the last moment the PID (and so the process group id) is
    known to have been the worker's. A reap of any other child that happens to reuse the number
    never writes here."""
    reaped = _worker_reaped_at.get(int(pid))
    return None if reaped is None else float(reaped)


# A reused group id belongs to a process created AFTER the reap; its members start later still. A
# genuine orphan group keeps at least one helper started while the worker ran, so the oldest member
# must predate the reap by this margin (absorbs the ~1 s macOS create_time drift, fails toward
# refusal).
_ORPHAN_GROUP_REAP_MARGIN_SECONDS = 2.0


def _vet_orphaned_worker_group(pid: int, started_at) -> dict[str, Any]:
    """Decide whether the dead worker ``pid``'s process group may be signalled (t_590dc20c).

    The group id is the dead leader's PID (``_default_spawn`` uses ``start_new_session=True``). POSIX
    never hands out a PID equal to an existing group id, so while one of the worker's helpers
    survives, the id still names the worker's group. Once the group fully empties the PID can be
    reused, and a new process that leads group P and exits leaves a look-alike orphaned group whose
    members all started after the reap. So every guard refuses rather than signal a stranger:
      * the row carries a real same-boot spawn fingerprint (legacy ``None``, integer and UNVERIFIED
        rows are refused; another boot's fingerprint is refused);
      * the leader PID is dead now (a live holder means the PID was reused);
      * never the caller's own process group; ``ps`` must be readable;
      * every member's start time is readable and not before the worker's start;
      * the dispatcher in THIS process spawned the worker and reaped it (``_worker_exit_reaped_at``)
        and the oldest member started at least ``_ORPHAN_GROUP_REAP_MARGIN_SECONDS`` before that
        reap. Without an observed reap there is no upper bound, so the group is left alone
        (``leader_exit_unobserved``).
    Returns ``{"pgid", "members", "refused"}``; ``refused`` None with members = safe to signal."""
    info: dict[str, Any] = {"pgid": None, "members": [], "refused": None}
    if _kb._IS_WINDOWS or not (hasattr(os, "killpg") and hasattr(os, "getpgrp")):
        info["refused"] = "platform"
        return info
    if not pid or int(pid) <= 1:
        info["refused"] = "no_pid"
        return info
    if not (isinstance(started_at, str) and "|" in started_at):
        # None = legacy row, UNVERIFIED marker, or an integer pre-epoch fingerprint.
        info["refused"] = "no_fingerprint"
        return info
    from gateway.drain_control import current_instantiation_epoch
    epoch, _, start_raw = started_at.rpartition("|")
    if epoch != current_instantiation_epoch():
        info["refused"] = "other_boot"
        return info
    try:
        worker_start = int(start_raw)
    except ValueError:
        info["refused"] = "no_fingerprint"
        return info
    pgid = int(pid)
    info["pgid"] = pgid
    if _kb._pid_alive(pgid):
        info["refused"] = "leader_pid_live"
        return info
    try:
        if pgid == os.getpgrp():
            info["refused"] = "own_group"
            return info
    except OSError:
        info["refused"] = "own_group"
        return info
    members = _live_group_members(pgid)
    if members is None:
        info["refused"] = "ps_unreadable"
        return info
    info["members"] = sorted(members)
    if not members:
        return info
    from gateway.status import get_process_start_time
    try:
        import psutil  # type: ignore
    except ImportError:  # pragma: no cover - core dependency
        info["refused"] = "no_psutil"
        return info
    created: list[float] = []
    for member in members:
        member_start = get_process_start_time(int(member))
        if member_start is None:
            info["refused"] = "member_start_unreadable"
            return info
        if int(member_start) < worker_start:
            info["refused"] = "member_predates_worker"
            return info
        try:
            created.append(float(psutil.Process(int(member)).create_time()))
        except Exception:
            info["refused"] = "member_start_unreadable"
            return info
    reaped_at = _worker_exit_reaped_at(pgid)
    if reaped_at is None:
        info["refused"] = "leader_exit_unobserved"
        return info
    if min(created) > reaped_at - _ORPHAN_GROUP_REAP_MARGIN_SECONDS:
        info["refused"] = "members_postdate_leader_exit"
        return info
    return info


# Refusals worth a durable event even though nothing was signalled: something unexpected is
# holding the dead worker's group id, or the orphans could not be checked.
_ORPHAN_GROUP_EVENT_REFUSALS = frozenset({
    "leader_pid_live", "own_group", "ps_unreadable", "member_start_unreadable",
    "member_predates_worker", "leader_exit_unobserved", "members_postdate_leader_exit",
})


def _reap_orphaned_worker_groups(targets: list[tuple[int, Any]]) -> dict[int, dict[str, Any]]:
    """End what is left of each dead worker's process group, all groups together: vet every group
    (``_vet_orphaned_worker_group``), SIGTERM the approved ones, ONE shared ~5 s grace, then SIGKILL
    any approved group that still has members. ``targets`` = ``[(pid, spawn fingerprint)]``.
    Returns ``{pid: {"members", "signalled", "sigkill", "survivors", "refused"}}``."""
    results: dict[int, dict[str, Any]] = {}
    approved: list[int] = []
    for pid, started_at in targets:
        vet = _vet_orphaned_worker_group(pid, started_at)
        results[int(pid)] = {
            "members": vet["members"], "signalled": False, "sigkill": False, "survivors": [],
            "refused": vet["refused"],
        }
        if vet["refused"] is None and vet["members"]:
            if _signal_worker_group(vet["pgid"], signal.SIGTERM):
                results[int(pid)]["signalled"] = True
                approved.append(vet["pgid"])
            else:
                results[int(pid)]["survivors"] = sorted(_live_group_members(vet["pgid"]) or [])
    if not approved:
        return results
    for _ in range(10):
        if not any(_worker_group_has_live_members(g) for g in approved):
            break
        time.sleep(0.5)
    killed = False
    for pgid in approved:
        # Still dead leader + members left after the grace → the id still names the same group.
        if _worker_group_has_live_members(pgid) and not _kb._pid_alive(pgid):
            if _signal_worker_group(pgid, getattr(signal, "SIGKILL", signal.SIGTERM)):
                results[pgid]["sigkill"] = True
                killed = True
    if killed:
        time.sleep(0.2)
    for pgid in approved:
        results[pgid]["survivors"] = sorted(_live_group_members(pgid) or [])
    return results


def _reap_crashed_worker_groups(conn: sqlite3.Connection, dead_groups: list) -> None:
    """Crash-sweep follow-up, run LAST in ``detect_crashed_workers`` (after crash accounting and the
    worker-exited hook, outside every txn): end the released workers' orphaned groups and record one
    ``worker_group_reaped`` event per card whose group had members or hit a notable refusal.
    Best-effort: a failure here never un-releases a card or breaks the tick."""
    try:
        results = _reap_orphaned_worker_groups([(pid, fp) for (_t, pid, fp, _r) in dead_groups])
    except Exception as exc:  # pragma: no cover - defensive
        _kb._log.warning("kanban: orphaned worker group reap failed: %s", exc)
        return
    for task_id, pid, _fp, run_id in dead_groups:
        info = results.get(int(pid))
        if not info:
            continue
        if not info["members"] and info["refused"] not in _ORPHAN_GROUP_EVENT_REFUSALS:
            continue
        if info["refused"] in _ORPHAN_GROUP_EVENT_REFUSALS:
            _kb._log.warning(
                "kanban: left worker group %s of task %s alone (%s); members=%s",
                pid, task_id, info["refused"], info["members"],
            )
        try:
            with _kb.write_txn(conn):
                _kb._append_event(
                    conn, task_id, "worker_group_reaped", {"pid": int(pid), **info}, run_id=run_id,
                )
        except sqlite3.Error:
            pass


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
    started_at=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths. ``started_at`` is the spawn-time
    fingerprint: when the live process no longer matches it, the PID was recycled and nothing is
    signalled — the worker is gone, which is what the reclaim wanted (``terminated`` = True). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never signalled either, but
    it is reported as surviving (``signal_refused``) so the reclaim holds the claim instead of
    spawning a duplicate beside it.

    Process group: when the live, fingerprint-verified worker leads its own group
    (``getpgid(pid) == pid``, the ``_default_spawn`` shape), SIGTERM and SIGKILL go to the whole
    group, so a CLI the worker launched does not outlive a timeout / archive / reclaim / terminal
    reap. After the grace poll, live group members are SIGKILLed even when the leader exited DURING
    the grace (orphaned children keep the group alive). Not covered: children of a leader that had
    already exited before termination began (no verified group to address), and children that called
    ``setsid`` themselves (daemons, MCP servers, Hermes terminal-tool commands). ``group_signalled``
    records whether the group path ran."""
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
        "group_signalled": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info["host_local"] = True

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Never signal by bare number: a dead PID is "gone" (reclaim proceeds), a live one is held.
        info["signal_refused"] = True
        info["terminated"] = not _kb._pid_alive(pid)
        return info
    if _kb._pid_alive(pid) and _pid_recycled(pid, started_at):
        info["terminated"] = True
        info["pid_recycled"] = True
        return info

    group = _worker_leads_own_group(pid, started_at, signal_fn)
    info["termination_attempted"] = True
    try:
        if group and _signal_worker_group(pid, signal.SIGTERM):
            info["group_signalled"] = True
        else:
            kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already gone = successful termination. Leaving terminated=False would
        # make the reclaim guard misread a dead worker as alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    if info["group_signalled"]:
        exited = _poll_worker_group_exit(pid, started_at)
        # Grace is over: SIGKILL the group even when the leader exited during the grace, so orphaned
        # children (an external CLI ignoring SIGTERM) die too. POSIX does not hand out a PID equal
        # to an existing group id, so while a member survives the id still names our group; a leader
        # PID that is live again under a different fingerprint was recycled → not signalled.
        leader_recycled = _kb._pid_alive(pid) and _pid_recycled(pid, started_at)
        if not leader_recycled and _worker_group_has_live_members(pid):
            if _signal_worker_group(pid, getattr(signal, "SIGKILL", signal.SIGTERM)):
                info["sigkill"] = True
    else:
        exited = _poll_worker_exit(pid, started_at)
    if exited:
        info["terminated"] = True
        return info
    if _worker_alive(pid, started_at):
        if not _sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = not _worker_alive(pid, started_at)
    return info


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    rows = conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL",
        (int(time.time()) - TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid() or not str(row["claim_lock"] or "").startswith(host_prefix):
        return
    if fingerprint == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    alive = _worker_alive(pid, fingerprint)
    termination = None
    if alive:
        termination = _terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint)
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive:
        reaped.append(row["task_id"])


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("host_local")
        and (termination.get("termination_attempted") or termination.get("signal_refused"))
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
    errors_out: Optional[list] = None,
) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.

    The worker is signalled inside the release transaction, only after the
    lifecycle fence accepted the release UPDATE: a refused row is never killed
    (recorded in ``errors_out``, see :func:`_isolate_fenced_row`). A worker that
    survives SIGKILL keeps its claim (``reclaim_deferred``) and is retried next tick.
    """
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
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        started_at = _kb._row_get(row, "worker_started_at")
        if started_at == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
            # Fingerprint capture failed at spawn: we cannot prove this live PID is our worker, so
            # it is neither signalled nor released beside (duplicate). It is reclaimed once it exits.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but has no verified "
                             "identity; not signalled", tid, pid)
            continue

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        termination: dict[str, Any] = {}
        try:
            with _kb.write_txn(conn):
                retry_status = _kb._retry_status_for_run(conn, tid)
                cur = conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "last_heartbeat_at = NULL "
                    "WHERE id = ? AND status = 'running' "
                    "  AND worker_pid = ? AND claim_lock IS ?",
                    (retry_status, tid, pid, row["claim_lock"]),
                )
                if cur.rowcount != 1:
                    continue
                # The fence accepted the release; only now may the worker be
                # signalled. SIGTERM then SIGKILL after the grace poll; a recycled
                # PID (fingerprint mismatch) is never signalled.
                termination = _kb._terminate_reclaimed_worker(
                    pid, row["claim_lock"], signal_fn=signal_fn, started_at=started_at,
                )
                if _worker_survived_termination(termination):
                    raise _ReclaimAbortForLiveWorker()
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "retry_status": retry_status,
                }
                payload.update(termination)
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
        except _ReclaimAbortForLiveWorker:
            _defer_reclaim_for_live_worker(
                conn, tid, row["claim_lock"], now, termination,
                reason="max_runtime_worker_alive",
            )
            continue
        except sqlite3.Error as exc:
            if not _isolate_fenced_row(errors_out, "enforce_max_runtime", tid, exc, termination):
                raise
            continue
        timed_out.append(tid)
        # Own txn, after the release committed. If the breaker trips this flips
        # the task to ``blocked`` and emits ``gave_up`` on top of ``timed_out``.
        _record_task_failure(
            conn, tid,
            error=error,
            outcome="timed_out",
            release_claim=False,
            end_run=False,
            event_payload_extra={
                "pid": pid, "sigkill": termination["sigkill"], "retry_status": retry_status,
            },
        )
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
    errors_out: Optional[list] = None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.

    Only rows CLAIMED BY THIS HOST are considered — a claim_lock from another
    node is out of scope entirely, matching :func:`detect_crashed_workers`'
    host_prefix filter. Without it a remote-owned running row (no host-local
    worker for this process to manage at all) could reach the guarded UPDATE
    below and trip an installed lifecycle-fence trigger.

    Lifecycle-fence isolation, same class as :func:`reconcile_orphaned_running`
    and the claim boundary: a row this node's claim_lock legitimately owns may
    still be protected if its generation was independently revoked (lease
    expired/reassigned between claim and tick). That row's guarded UPDATE is
    caught per row and recorded in ``errors_out`` (:func:`_isolate_fenced_row`)
    — no whole-tick abort, no signal, no trigger disabled, no lease fabricated.

    Signal ordering: the host-local termination signal is issued INSIDE the same
    ``write_txn`` as the guarded claim-release UPDATE and strictly AFTER that
    UPDATE is accepted, never before. A fence-refused release therefore signals
    nothing: a same-host worker whose generation was revoked must not be killed
    by a reclaim path that is not authorized to release its claim. If the worker
    is signalled and survives, the whole transaction rolls back via
    :class:`_ReclaimAbortForLiveWorker` — the release never becomes durable — and
    a separate, non-guarded transaction records the defer.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue

        # Host-local claims only — another node's claim_lock is out of this
        # process's authority to reclaim OR signal at all.
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue

        if handoff_pending(conn, row["id"], errors_out=errors_out, stage="detect_stale_running"):
            continue

        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        if pid and _kb._worker_alive(pid, _kb._row_get(row, "worker_started_at")):
            _record_degree_warning(conn, tid, "stale_heartbeat_live_owner")
            continue

        # Set strictly AFTER the guarded UPDATE is accepted, so a fenced
        # reclaim never signals the worker.
        termination: dict[str, Any] = {}
        try:
            with _kb.write_txn(conn):
                if handoff_pending(conn, tid, errors_out=errors_out, stage="detect_stale_running"):
                    continue
                retry_status = _kb._retry_status_for_run(conn, tid)
                cur = conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "last_heartbeat_at = NULL "
                    "WHERE id = ? AND status = 'running' "
                    "  AND claim_lock IS ?",
                    (retry_status, tid, row["claim_lock"]),
                )
                if cur.rowcount != 1:
                    continue

                # The lifecycle fence accepted the release (this node's claimed
                # generation is still authorized) — only now is it safe to
                # signal the worker.
                termination = _kb._terminate_reclaimed_worker(
                    pid, lock, signal_fn=signal_fn,
                    started_at=_kb._row_get(row, "worker_started_at"),
                )

                # Never release a claim while our own worker is still alive:
                # that would spawn a duplicate beside it. Roll the whole
                # transaction back and defer instead — nothing counts as
                # reclaimed unless the release actually stuck.
                if _worker_survived_termination(termination):
                    raise _ReclaimAbortForLiveWorker()

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
        except _ReclaimAbortForLiveWorker:
            # The transaction above rolled back in full — the row is exactly as
            # it was (still 'running', original claim_lock intact). Record the
            # defer in a fresh, independent transaction.
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue
        except sqlite3.Error as exc:
            # A fence refusal fires on the UPDATE, before the signal: the row
            # is untouched and its worker was never signalled.
            if not _isolate_fenced_row(errors_out, "detect_stale_running", tid, exc, termination):
                raise
            continue
        reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(
    conn: sqlite3.Connection,
    *,
    errors_out: Optional[list] = None,
) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to ``ready``
    with a comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID is deferred so no duplicate spawns beside it.

    Only cards THIS node verifiably owns are candidates (t_23c40d89). On a Fleet
    board the mirror carries other nodes' ``running`` rows with no local claim;
    those are skipped before the PID probe and before any write (see
    :func:`_recovery_owned_here`). A row whose home is foreign is skipped
    silently; a row whose home is unknown or contradictory is left unchanged and
    surfaced in ``errors_out``. Boards without a Fleet adapter are unchanged.

    ``errors_out``, when given, is a caller-owned list this appends
    ``(task_id, "reconcile_orphaned_running: <message>")`` to for every row an
    installed lifecycle-guard trigger refused. Such a refusal is rolled back to
    that row's own transaction (leaving it completely unchanged — no partial
    write), logged as an expected fence, recorded, and reconciliation continues
    to sibling rows. No trigger is ever disabled and no lease/generation row is
    ever fabricated to satisfy one: a protected row stays protected until its
    owner-side lease is reconciled elsewhere.

    Any other database error — an unrecognized constraint, or
    ``sqlite3.OperationalError`` (lock contention, I/O, corruption) — is not an
    expected fence and still aborts the tick visibly (:func:`_isolate_fenced_row`).
    """
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
        tid = row["id"]
        pid = row["worker_pid"]
        # Ownership BEFORE the PID probe or any write (t_23c40d89): a foreign
        # node's mirrored running row is not ours to probe or requeue.
        if not _recovery_owned_here(conn, errors_out, "reconcile_orphaned_running", tid, node_id):
            continue
        if handoff_pending(conn, tid, errors_out=errors_out, stage="reconcile_orphaned_running"):
            continue
        if pid and _worker_alive(pid, _kb._row_get(row, "worker_started_at")):
            # Never requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        try:
            with _kb.write_txn(conn):
                if handoff_pending(conn, tid, errors_out=errors_out, stage="reconcile_orphaned_running"):
                    continue
                cur = conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "last_heartbeat_at = NULL "
                    "WHERE id = ? AND status = 'running' "
                    "  AND claim_lock IS ? AND claim_expires IS ?",
                    (tid, row["claim_lock"], row["claim_expires"]),
                )
                if cur.rowcount != 1:
                    continue
                payload = {
                    "reason": "orphaned_running",
                    "claim_lock": row["claim_lock"],
                    "claim_expires": _kb._opt_int(row["claim_expires"]),
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
        except sqlite3.Error as exc:
            if not _isolate_fenced_row(errors_out, "reconcile_orphaned_running", tid, exc):
                raise
            continue
        # Only rows whose write_txn actually committed reach here — durable
        # counting happens strictly after commit.
        reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    # Neutral outcomes are filtered in SQL, not skipped inside the window: a long quota wall or
    # busy hold (any number of runs) must not push an earlier violation out of the scan.
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "  AND COALESCE(outcome, '') NOT IN ('rate_limited', 'profile_busy') "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "crashed" and (
            _kb._json_dict(row["metadata"]).get("protocol_violation")
            or "protocol violation" in (row["error"] or "")
        ):
            streak += 1
            continue
        break
    return streak


def _profile_busy_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of closed ``profile_busy`` runs (any other outcome breaks it).

    Exact at any length (no scan window): the busy runs newer than the newest closed run
    that was not ``profile_busy``.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL AND outcome = 'profile_busy' "
        "  AND id > COALESCE((SELECT MAX(id) FROM task_runs WHERE task_id = ? "
        "      AND ended_at IS NOT NULL AND COALESCE(outcome, '') != 'profile_busy'), 0)",
        (task_id, task_id),
    ).fetchone()
    return int(row["n"] or 0)


def _profile_busy_backoff_seconds(streak: int) -> int:
    """Wait before re-spawning after ``streak`` consecutive busy runs: base doubling per run, capped."""
    # Overrides may tune the curve only inside the published contract. A bad value cannot
    # cause every-tick retry (<60s) or park a card longer than 15 minutes (>900s).
    base = min(DEFAULT_PROFILE_BUSY_BACKOFF_MAX_SECONDS, max(
        DEFAULT_PROFILE_BUSY_BACKOFF_SECONDS,
        _kb._env_int(
            "HERMES_KANBAN_PROFILE_BUSY_BACKOFF_SECONDS", DEFAULT_PROFILE_BUSY_BACKOFF_SECONDS,
        ),
    ))
    cap = min(DEFAULT_PROFILE_BUSY_BACKOFF_MAX_SECONDS, max(
        DEFAULT_PROFILE_BUSY_BACKOFF_SECONDS,
        _kb._env_int(
            "HERMES_KANBAN_PROFILE_BUSY_BACKOFF_MAX_SECONDS", DEFAULT_PROFILE_BUSY_BACKOFF_MAX_SECONDS,
        ),
    ))
    # Exponent clamped so a very long streak cannot build a huge integer before the cap applies.
    exponent = min(max(int(streak), 1) - 1, 32)
    return int(min(base * (2 ** exponent), cap))


def _book_profile_busy(dead: "_DeadWorker", streak: int) -> None:
    """Stamp the busy streak, the next retry and (past the threshold) ``long_busy`` on the
    run/event payload and lead the card's error line with them, so a long hold is visible."""
    wait = _profile_busy_backoff_seconds(streak)
    dead.event_payload["busy_streak"] = streak
    dead.event_payload["next_retry_after_seconds"] = wait
    long_busy = streak >= PROFILE_BUSY_LONG_STREAK
    if long_busy:
        dead.event_payload["long_busy"] = True
    dead.error_text = (
        f"profile busy: {'LONG HOLD, ' if long_busy else ''}refused its profile's only "
        f"active-session slot {streak} times in a row; requeued without counting a failure, "
        f"next try in {wait}s. " + dead.error_text
    )


_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    # Keep this short: ``_record_task_failure`` caps the stored error at 500 chars and the worker's own
    # last output (``_worker_final_output``, up to 400 chars) is appended after it — a longer preamble
    # truncates away the worker's explanation, which is the part the board and the retry worker need.
    "worker exited cleanly (rc=0) without kanban_complete, kanban_block "
    "or kanban_request_review — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report it via kanban_complete (or kanban_request_review); "
    "a run without a terminal kanban call counts as failed no "
    "matter what it did."
)


_EXIT_SUMMARY_MARKER = "Resume this session with:"
# Rich panel/rule chrome around the rendered response, and the CLI's own preamble lines.
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")
_LOG_NOISE_PREFIXES = ("session_id:", "Query:", "Initializing agent")


def _worker_final_output(task_id: str, board: Optional[str] = None) -> str:
    """Best-effort read of a dead worker's last printed text, for the board diagnostic.

    A ``chat -q`` worker's stdout/stderr are redirected to its per-task log
    (``_default_spawn``), so when it exits without a terminal board call the
    reason is usually sitting there: the model's own explanation of why it could
    not comply (#88603), or the rendered provider error (#46593). The reap used to
    discard it in favour of a canned message on every retry. Trims the CLI exit
    summary, rule lines and the ``session_id:`` trailer; returns "" (never raises)
    on a missing/empty log.

    ``board`` must come from the dispatching tick: ambient current-board resolution
    is wrong for every board but the one the dispatcher thread happens to call
    "current", so the log would silently not be found.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return ""
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", raw)
    cut = raw.rfind(_EXIT_SUMMARY_MARKER)
    if cut != -1:
        raw = raw[:cut]
    lines = []
    for ln in raw.splitlines():
        ln = _LOG_CHROME.sub("", ln).strip()
        if ln and not ln.startswith(_LOG_NOISE_PREFIXES):
            lines.append(ln)
    return " ".join(lines)[-400:]


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's
    credential/model — trips the breaker on this first occurrence."""
    profile_busy: bool = False
    """EX_TEMPFAIL exit plus the run-bound busy marker: the worker was refused its
    profile's active-session slot and never ran — requeued with backoff, never counted."""

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` (and a busy profile as
        # ``profile_busy``) so board history doesn't show a phantom crash.
        if self.profile_busy:
            return "profile_busy"
        return "rate_limited" if self.rate_limited else "crashed"


def _classify_dead_worker(
    pid: int, claimer: Optional[str], *, task_id: Optional[str] = None, board: Optional[str] = None,
    run_id: Optional[int] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output``
    in the event payload, appended to the error text) so the board and the retry
    worker see WHY instead of a bare label; a rate-limited requeue does not need it.
    """
    dead = _classify_dead_worker_exit(pid, claimer, task_id=task_id, board=board, run_id=run_id)
    if task_id and not dead.rate_limited:
        worker_output = _worker_final_output(task_id, board=board)
        if worker_output:
            dead.error_text += f" Worker's last output: {worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
    return dead


def _classify_dead_worker_exit(
    pid: int,
    claimer: Optional[str],
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
    run_id: Optional[int] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    The reap registry only knows children of THIS process; a per-tick dispatcher
    reads the exit trailer the worker left in its log instead, so the same death
    gets the same booking (protocol violation / rate-limit requeue / crash) as
    under the gateway-embedded dispatcher. A worker that never reached its exit
    epilogue (killed, OOM) leaves no trailer and stays a plain crash.
    """
    kind, code = _classify_worker_exit(pid)
    if kind == "unknown" and task_id:
        logged = _worker_log_exit_code(task_id, board=board)
        if logged is not None:
            kind, code = _exit_code_kind(logged)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited" and task_id and _worker_log_busy_for_run(task_id, run_id, board=board):
        # Profile busy (t_76b0d62d): the worker was refused its profile's active-session slot
        # by the cap and never started its turn. Needs BOTH the EX_TEMPFAIL exit and the marker
        # written for THIS run; the refusal phrase in the output alone never classifies.
        return _DeadWorker(
            "profile_busy", code,
            f"pid {pid} was refused its profile's active-session slot",
            "profile_busy",
            {"pid": pid, "claimer": claimer, "exit_kind": "profile_busy", "exit_code": code,
             "reason": _PROFILE_BUSY_REASON},
            profile_busy=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    profile_busy: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, dead_worker)``: accounted after the txn via
    # ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, _DeadWorker]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)
    dead_groups: list[tuple[str, int, Any, Optional[int]]] = field(default_factory=list)


def _reclaim_dead_workers(
    conn: sqlite3.Connection,
    board: Optional[str] = None,
    *,
    errors_out: Optional[list] = None,
) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead.

    Each row writes under its own savepoint, so a lifecycle-fence refusal rolls
    back only that row (recorded in ``errors_out``) and never the siblings
    already released in the enclosing sweep transaction.
    """
    sweep = _CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee, "
            "current_run_id "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch grace protects a freshly spawned PID before it is visible to the OS.
            # Explicit, run-bound profile-busy evidence proves this worker already exited,
            # so it alone may bypass grace; generic crashes still wait for the normal probe.
            started_at = _kb._row_get(row, "started_at")
            dead: Optional[_DeadWorker] = None
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                candidate = _classify_dead_worker(
                    int(row["worker_pid"]), row["claim_lock"], task_id=row["id"], board=board,
                    run_id=_kb._row_get(row, "current_run_id"),
                )
                if not candidate.profile_busy:
                    continue
                dead = candidate
            if _worker_alive(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                continue

            pid = int(row["worker_pid"])
            if dead is None:
                dead = _classify_dead_worker(
                    pid, row["claim_lock"], task_id=row["id"], board=board,
                    run_id=_kb._row_get(row, "current_run_id"),
                )
            try:
                with _kb.write_txn(conn, allow_nested=True):
                    retry_status = _kb._retry_status_for_run(conn, row["id"])
                    dead.event_payload["retry_status"] = retry_status
                    cur = conn.execute(
                        "UPDATE tasks SET status = ?, claim_lock = NULL, "
                        "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                        "WHERE id = ? AND status = 'running' "
                        "  AND worker_pid = ? AND claim_lock IS ?",
                        (retry_status, row["id"], pid, row["claim_lock"]),
                    )
                    if cur.rowcount != 1:
                        continue
                    if dead.profile_busy:
                        _book_profile_busy(dead, _profile_busy_streak(conn, row["id"]) + 1)
                    run_id = _kb._end_run(
                        conn, row["id"],
                        outcome=dead.run_outcome, status=dead.run_outcome,
                        error=dead.error_text,
                        metadata=dict(dead.event_payload),
                    )
                    _kb._append_event(
                        conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id,
                    )
                    if dead.rate_limited or dead.protocol_violation or dead.profile_busy:
                        # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                        # a rate-limited requeue must show ``check_respawn_guard`` a quota
                        # blocker; a below-budget protocol violation never reaches
                        # ``_record_task_failure`` (which stamps this column), yet the
                        # board UI and retry worker need the corrective message.
                        conn.execute(
                            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                            (dead.error_text[:500], row["id"]),
                        )
            except sqlite3.Error as exc:
                if not _isolate_fenced_row(errors_out, "detect_crashed_workers", row["id"], exc):
                    raise
                continue
            sweep.dead_groups.append(
                (row["id"], pid, _kb._row_get(row, "worker_started_at"), run_id),
            )
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation or dead.profile_busy:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.profile_busy:
                sweep.profile_busy.append(row["id"])
            elif dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep


def _account_crashes(
    conn: sqlite3.Connection, crash_details: list, *, failure_limit: Optional[int] = None,
) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, dead in crash_details:
        fp = _error_fingerprint(dead.error_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, dead in crash_details:
        error_text = dead.error_text
        if dead.protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        elif dead.terminal_provider:
            # A retry cannot heal a revoked credential or a missing model, so
            # the whole ``failure_limit`` budget would be spent on identical
            # failures. ``force_trip`` blocks now, sticky: ``recompute_ready``
            # must not auto-resume it before the operator fixes the provider.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True},
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            extra = {"pid": pid, "claimer": claimer}
            if is_systemic:
                # Trips at 1, below any ``failure_limit``: hold it for an operator.
                extra["sticky"] = True
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else failure_limit,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(
    conn: sqlite3.Connection,
    board: Optional[str] = None,
    *,
    errors_out: Optional[list] = None,
    failure_limit: Optional[int] = None,
) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    Lifecycle-fence refusals are isolated per row into ``errors_out``.
    """
    sweep = _reclaim_dead_workers(conn, board=board, errors_out=errors_out)
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = (
        _account_crashes(conn, sweep.crash_details, failure_limit=failure_limit)
        if sweep.crash_details else []
    )
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    detect_crashed_workers._last_profile_busy = sweep.profile_busy  # type: ignore[attr-defined]
    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    if sweep.dead_groups:
        _reap_crashed_worker_groups(conn, sweep.dead_groups)
    return sweep.crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: Optional[int] = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    infrastructure: bool = False,
) -> bool:
    """Record failure evidence and restore the retry phase; thresholds warn before any stop.

    Native lease/generation and explicit owner holds remain authoritative. The
    counter stays durable, and its warning sends uncertainty through Jev/another
    model and a Decider instead of automatically parking the card.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        threshold_reached = not infrastructure and (force_trip or failures >= effective_limit)
        if release_claim:
            conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "consecutive_failures = ?, last_failure_error = ? "
                "WHERE id = ? AND status = 'running'",
                (retry_status, failures, error, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ?, last_failure_error = ? WHERE id = ?",
                (failures, error, task_id),
            )
        if end_run:
            detail = {"failures": failures, "retry_status": retry_status}
            if infrastructure:
                detail["infrastructure"] = True
            run_id = _kb._end_run(conn, task_id, outcome=outcome, status=outcome, error=error, metadata=detail)
            _kb._append_event(conn, task_id, outcome, {"error": error, **detail}, run_id=run_id)
        if threshold_reached:
            _record_degree_warning(conn, task_id, "failure_counter")
        return False


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + its restart-stable fingerprint (``_process_fingerprint``), and
    emit a ``spawned`` event carrying them. The fingerprint is what lets every later liveness/kill
    decision tell OUR worker from a process that recycled the PID after a reboot. A failed capture is
    persisted as ``UNVERIFIED_WORKER_FINGERPRINT``, never NULL: NULL is the legacy pre-fingerprint row
    whose bare-PID kill authority a new spawn must not inherit."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                     (int(pid), started_at, task_id))
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, run_id))
        _kb._append_event(conn, task_id, "worker_handoff_resolved", {"pid": int(pid)}, run_id=run_id)
        _kb._append_event(conn, task_id, "spawned", {"pid": int(pid), "started_at": started_at}, run_id=run_id)


def _blocked_event_reason(payload: Optional[str]) -> str:
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
        "SELECT id FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if newest_blocked is None:
        return False
    if conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND kind = 'unblocked' AND id > ? LIMIT 1",
        (task_id, newest_blocked["id"]),
    ).fetchone():
        return False
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 2",
        (task_id,),
    ).fetchall()
    if len(rows) < 2:
        return False
    newest, previous = (_blocked_event_reason(r["payload"]) for r in rows)
    return bool(newest) and newest == previous


def _explicit_do_not_dispatch_marked(conn: sqlite3.Connection, task_id: str) -> bool:
    comment = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if comment is None:
        return False
    # Titles and bodies often describe dispatch rules for other cards/profiles.
    text = (_kb._lossy_text(comment["body"]) or "").strip().casefold()
    return text.startswith(_EXPLICIT_DO_NOT_DISPATCH_MARKERS)


def _record_worker_handoff(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    from hermes_cli.kanban_launch import WorkerHandoffUncertain

    try:
        _set_worker_pid(conn, task_id, pid)
    except Exception as exc:
        # The pre-spawn intent remains durable even if all later writes fail.
        raise WorkerHandoffUncertain(
            f"Worker {pid} started for {task_id}; PID persistence failed. "
            "Unresolved handoff retained for Decider verification."
        ) from exc


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


_DEGREE_RESPAWN_REASONS = frozenset({"unchanged_block_reason", "active_pr", "blocker_auth"})
_DEGREE_NEXT_STEP = (
    "Assess your own confidence against the card; when uncertain use hermes jev evaluate --file "
    "<request.json> or a quick second model. Low confidence: send the card/evidence pointer through "
    "fleet handoff to a Decider; continue the existing card/PR, never create duplicate work. "
    "A stop is the last step; a Decider may release the advisory."
)


def _record_degree_warning(conn: sqlite3.Connection, task_id: str, reason: str, *, retry_seconds: int = 60) -> None:
    """One durable, visible advisory per reason/window; never changes card ownership or status."""
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


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
    lifted: Optional[list[tuple[str, str]]] = None,
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    an explicit ``do not dispatch`` / ``king seat is already doing this`` marker
    at the start of the newest comment, then ``unchanged_block_reason`` (two newest blocked reasons match and no
    later ``unblocked`` event), then
    ``"infrastructure_cooldown"`` (latest run is a ``spawn_failed`` the host
    refused — no restart-safe scope — within the cooldown; never counted),
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"blocker_auth"``
    (quota/auth pattern; the breaker still trips eventually), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (current assignee's new PR URL in a recent comment; re-spawning risks a
    duplicate PR — unless an explicit unblock or handoff followed the comment).
    The review lane skips the last two: they are the *inputs* to a review
    handoff. Stale / dead claim locks are NOT a guard reason — the reclaim
    passes own those.
    """
    if handoff_pending(conn, task_id):
        return "worker_handoff_uncertain"

    row = conn.execute(
        "SELECT last_failure_error, assignee, body FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    if _explicit_do_not_dispatch_marked(conn, task_id):
        return "explicit_do_not_dispatch"

    if _unchanged_blocked_reason(conn, task_id):
        return "unchanged_block_reason"

    now = int(time.time())
    lease_notice = conn.execute(
        "SELECT id, created_at, payload FROM task_events WHERE task_id = ? AND kind = 'degree_warning' "
        "AND created_at > ? ORDER BY id DESC", (task_id, now - 300),
    ).fetchall()
    for notice in lease_notice:
        if _kb._json_dict(notice["payload"]).get("reason") != "execution_lease":
            continue
        recovery = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
            "AND kind IN ('unblocked', 'assigned', 'lease_acquired') LIMIT 1", (task_id, notice["id"]),
        ).fetchone()
        if recovery is None:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('fleet_kanban_issue_map', 'fleet_kanban_verified_leases')"
            ).fetchone()[0]
            verified = None
            if tables == 2:
                verified = conn.execute(
                    "SELECT 1 FROM tasks t JOIN fleet_kanban_issue_map im ON im.local_task_id=t.id "
                    "JOIN fleet_kanban_verified_leases lease ON lease.issue_id=im.issue_id "
                    "WHERE t.id=? AND lease.holder_profile=t.assignee "
                    "AND lease.holder_node=COALESCE(im.current_node,im.source_node) "
                    "AND lease.expires_at>? LIMIT 1", (task_id, now),
                ).fetchone()
            if verified is None:
                return "execution_lease_retry"
        break

    # 1. Rate-limit cooldown — see docstring for why this precedes blocker_auth.
    #    LATEST run only: a newer crash/completion supersedes the rate-limit run.
    #    An infrastructure spawn refusal (#114720) shares the cooldown: the host
    #    condition is not the card's, so it retries forever, spaced, and never
    #    reaches the breaker.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at, metadata, error FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        # ``id`` breaks same-second ties so the newest run decides (checker round 1).
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "spawn_failed":
        if rl_cooldown > 0 and _kb._json_dict(latest_run["metadata"]).get("infrastructure"):
            ended_at = latest_run["ended_at"]
            if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
                return "infrastructure_cooldown"
    if latest_run is not None and latest_run["outcome"] == "profile_busy":
        # Bounded backoff (60s doubling, 15 min cap) keyed on the consecutive busy streak.
        # Returns before blocker_auth: the stamped error carries the worker's output, which
        # is context, not a quota/auth diagnosis. Retries forever, spaced, never counted.
        ended_at = latest_run["ended_at"]
        wait = min(60, _profile_busy_backoff_seconds(_profile_busy_streak(conn, task_id)))
        if ended_at is not None and (now - int(ended_at)) < wait:
            return "profile_busy_backoff"
        return None
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, skipping blocker_auth so
            # the stamped rate-limit text doesn't re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — return early so blocker_auth doesn't catch the
        # stamped rate-limit text; this path intentionally retries forever
        # (spaced by the cooldown) until quota returns or a real run supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.  A plain
    # crash is different: its persisted error includes the worker's last
    # captured output, which is context rather than a diagnosis and may contain
    # benign commands such as ``claude auth status`` (#117097).
    run_err = (
        _kb._lossy_text(latest_run["error"])
        if latest_run is not None
        else None
    )
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    if run_err and latest_outcome != "crashed" and _RESPAWN_BLOCKER_RE.search(run_err):
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. A current assignee's new GitHub PR URL in a recent comment — that
    #    worker may have already opened a PR. Links in the task body are inputs,
    #    and an explicit unblock after the comment permits another run.
    #    Automatic 'promoted' and 'status' events do not authorize a duplicate
    #    implementation: they can follow recovery or descendant invalidation.
    #    Exception: a handoff AFTER the newest PR comment (operator reassign,
    #    reviewer changes_requested, review reopen) names the profile that must
    #    now work on THAT PR — a closer or the implementer finishing it, not a
    #    duplicate implementation (#111910). A crash/reclaim is not a handoff,
    #    so the worker that opened the PR is still not re-spawned against it.
    prs = _active_pr_keys(conn, task_id, row["assignee"], row["body"], now)
    if prs:
        from hermes_cli.kanban_pr_state import all_terminal
        if all_terminal(prs, now):
            if lifted is not None:
                lifted.append((task_id, "active_pr_lifted_terminal"))
            return None
        return "active_pr"

    return None


def _is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


PLACEHOLDER_PROFILES: frozenset[str] = frozenset({"default", "alpha", "beta", "orch"})


def is_placeholder_profile(name: Optional[str]) -> bool:
    """True when *name* is a synthetic/placeholder pool profile
    (``default``, ``alpha``, ``beta``, ``orch``) with no gateway worker."""
    if not name or not isinstance(name, str):
        return False
    try:
        from hermes_cli.profiles import normalize_profile_name
        canon = normalize_profile_name(name)
    except Exception:
        canon = name.strip().lower()
    return canon in PLACEHOLDER_PROFILES


def is_dispatch_enabled_profile(name: str) -> bool:
    """True when *name* is a real profile with dispatch enabled on this node.

    1. When ``kanban.dispatch_profiles`` is set (#110995), the profile must be
       in the allowlist AND exist.
    2. When ``kanban.dispatch_profiles`` is unset:
       - Placeholder profiles (``default``, ``alpha``, ``beta``, ``orch``) have
         no gateway worker and are NOT dispatch-enabled.
       - Other profiles must actually exist as live named profiles on this node.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return True
    try:
        canon = normalize_profile_name(name)
    except ValueError:
        return False
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is not None:
        return canon in allowlist and bool(profile_exists(name))
    if canon in PLACEHOLDER_PROFILES:
        return False
    return bool(profile_exists(name))


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """Predicate testing whether an assignee is a real profile with dispatch
    enabled on this node.

    Returns ``None`` when ``hermes_cli.profiles`` cannot be imported (callers
    fall back to trusting the assignee).

    When ``kanban.dispatch_profiles`` is set (#110995) the returned predicate
    additionally requires the assignee to be listed, fail-closed — so a card
    assigned to ``default`` is only claimable by homes that opted into it.
    When unset, placeholder profiles (``default``, ``alpha``, ``beta``, ``orch``)
    with no gateway worker are excluded. Foreign and placeholder assignees land in
    ``skipped_nonspawnable`` / ``skipped_placeholder``.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return None
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)

    def _gated(name: str) -> bool:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            return False
        if allowlist is not None:
            return canon in allowlist and bool(profile_exists(name))
        if canon in PLACEHOLDER_PROFILES:
            return False
        return bool(profile_exists(name))

    return _gated


def _dispatch_profile_allowlist(normalize_profile_name) -> Optional[frozenset]:
    """Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995).

    On a shared board (one ``kanban.db`` mounted across several Hermes homes),
    every home's ``profile_exists`` returns True for ``default`` — the root
    profile every home has — so a card assigned to ``default`` is claimable by
    every home's dispatcher. A home opts out of foreign claims by declaring
    which assignees it may claim::

        kanban:
          dispatch_profiles: ["sage", "researcher"]   # or "sage,researcher"

    Returns ``None`` only when the key is absent from the user config (upstream
    behavior: any existing profile is claimable). A present value is
    fail-closed: an empty list, ``null`` or a bare ``dispatch_profiles:`` claims
    nothing. The user layer is read without the ``DEFAULT_CONFIG`` merge (whose
    ``None`` placeholder would make the key look present in every home), and a
    config read that raises also claims nothing — a corrupt config on a shared
    board must never widen this home's claim scope silently (#113620).
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read kanban.dispatch_profiles (%s: %s) — "
            "this home claims no cards until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping) or "dispatch_profiles" not in kanban:
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _kb._log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for n in names:
        try:
            allowed.add(normalize_profile_name(n))
        except ValueError:
            continue
    return frozenset(allowed)


def _control_plane_lane_patterns() -> tuple[str, ...]:
    """``kanban.control_plane_lanes``: assignee names/``fnmatch`` patterns that
    are served by something other than this home's dispatcher — terminal lanes
    that pull via ``claim_task``, or profiles another home owns on a shared
    board. Their ready cards are expected to wait quietly.

    Accepts a list or a comma-separated string. Absent, empty or unreadable
    config yields ``()``: no assignee is exempted as a lane. (An unreadable
    config also makes ``dispatch_profiles`` fail closed, so this home claims
    nothing and every skipped card reads as ``foreign`` until it is fixed.)
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read kanban.control_plane_lanes (%s: %s) — "
            "no assignee is exempted as a control-plane lane",
            type(exc).__name__, exc,
        )
        return ()
    if not isinstance(kanban, Mapping):
        return ()
    raw = kanban.get("control_plane_lanes")
    if not raw:
        return ()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return tuple(n.strip().lower() for n in names if n and n.strip())


_CURRENT_DISPATCH_TASK: contextvars.ContextVar[Optional[tuple[sqlite3.Connection, str]]] = (
    contextvars.ContextVar("_CURRENT_DISPATCH_TASK", default=None)
)


def _check_fleet_nonspawnable(
    conn: sqlite3.Connection,
    name: str,
    *,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Check fleet ownership for a nonspawnable assignee on a Fleet board.
    Returns 'foreign' if another node owns the assignee or card,
    'placeholder' if it is a placeholder profile,
    'missing' if this node is the verified owner,
    or None if no fleet adapter is installed (fall back to non-fleet logic).
    """
    try:
        installed_node_id = _kb._fleet_adapter_installed_node_id(conn)
    except Exception:
        return None

    if not installed_node_id:
        return None  # Non-fleet board: preserve legacy behavior

    clean_installed = installed_node_id.strip().lower()

    # 1. Check placeholder profiles first
    try:
        from hermes_cli.profiles import normalize_profile_name
        canon = normalize_profile_name(name)
    except Exception:
        canon = name
    if canon in PLACEHOLDER_PROFILES:
        return "placeholder"

    # 2. Check fleet_kanban_assignee_homes first
    try:
        has_homes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fleet_kanban_assignee_homes'"
        ).fetchone() is not None
        if has_homes:
            home_row = conn.execute(
                "SELECT home_node FROM fleet_kanban_assignee_homes WHERE LOWER(assignee) = LOWER(?)",
                (name,),
            ).fetchone()
            if home_row is not None and home_row[0]:
                home_node = str(home_row[0]).strip().lower()
                if home_node != clean_installed:
                    return "foreign"
                return "missing"
    except Exception:
        pass

    # 3. Check fleet_kanban_issue_map
    try:
        has_map = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fleet_kanban_issue_map'"
        ).fetchone() is not None
        if has_map:
            if task_id:
                map_row = conn.execute(
                    "SELECT current_node, source_node FROM fleet_kanban_issue_map WHERE local_task_id = ?",
                    (task_id,),
                ).fetchone()
            else:
                map_row = conn.execute(
                    "SELECT m.current_node, m.source_node FROM fleet_kanban_issue_map m "
                    "JOIN tasks t ON m.local_task_id = t.id WHERE LOWER(t.assignee) = LOWER(?) "
                    "ORDER BY t.id DESC LIMIT 1",
                    (name,),
                ).fetchone()
            if map_row is not None:
                current_node, source_node = map_row[0], map_row[1]
                owner = source_node if current_node is None else current_node
                if isinstance(owner, str) and owner.strip():
                    owner_clean = owner.strip().lower()
                    if owner_clean != clean_installed:
                        return "foreign"
                    return "missing"
                else:
                    return "foreign"
    except Exception:
        pass

    return None


def _nonspawnable_kind(
    assignee: str,
    conn: Optional[sqlite3.Connection] = None,
    task_id: Optional[str] = None,
) -> str:
    """Why ``assignee`` failed the spawn gate: ``"lane"`` (configured
    control-plane lane), ``"placeholder"`` (placeholder pool profile with no
    worker), ``"foreign"`` (this home's ``dispatch_profiles`` does not list it,
    or a Fleet board where another node owns the card/assignee) or ``"missing"``
    (this home should run it but has no such profile)."""
    import fnmatch

    name = (assignee or "").strip().lower()
    if any(fnmatch.fnmatchcase(name, pat) for pat in _control_plane_lane_patterns()):
        return "lane"

    ctx = _CURRENT_DISPATCH_TASK.get()
    if ctx is not None:
        if conn is None:
            conn = ctx[0]
        if task_id is None:
            task_id = ctx[1]

    # Query fleet ownership / assignee homes BEFORE profile-missing branch
    if conn is not None:
        fleet_res = _check_fleet_nonspawnable(conn, name, task_id=task_id)
        if fleet_res is not None:
            return fleet_res
    else:
        try:
            with _kbc.connect_readonly_closing() as ro_conn:
                fleet_res = _check_fleet_nonspawnable(ro_conn, name, task_id=task_id)
                if fleet_res is not None:
                    return fleet_res
        except Exception:
            pass

    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception:
        return "missing"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is not None:
        try:
            canon = normalize_profile_name(assignee)
        except ValueError:
            return "missing"
        if canon not in allowlist:
            if canon in PLACEHOLDER_PROFILES:
                return "placeholder"
            return "foreign"
    else:
        try:
            canon = normalize_profile_name(assignee)
        except ValueError:
            return "missing"
        if canon in PLACEHOLDER_PROFILES:
            return "placeholder"
    return "missing"


OWNER_UNAVAILABLE_EVENT = "owner_unavailable"


def _record_owner_unavailable(conn: sqlite3.Connection, task_id: str, assignee: str) -> bool:
    """Append ONE ``owner_unavailable`` event per unavailability episode.

    Deduped against the task's latest ``owner_unavailable`` / ``assigned`` /
    ``claimed`` event: repeat ticks for the same assignee write nothing; a
    reassignment or a later claim starts a new episode. Returns True when a
    row was written. The common repeat-tick case is a plain read (no write
    lock); the check is repeated inside the write transaction. A write refused
    by a board fence is logged and skipped — it must never abort the tick.
    """
    def _still_waiting() -> bool:
        # The row list was read at tick start; a reassign/claim may have
        # landed since. Only flag a card still queued for this assignee.
        row = conn.execute(
            "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        return (
            row is not None and row["status"] in ("ready", "review")
            and row["assignee"] == assignee and row["claim_lock"] is None
        )

    def _already_recorded() -> bool:
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind IN (?, 'assigned', 'claimed') ORDER BY id DESC LIMIT 1",
            (task_id, OWNER_UNAVAILABLE_EVENT),
        ).fetchone()
        if last is None or last["kind"] != OWNER_UNAVAILABLE_EVENT:
            return False
        prev = _kb._json_or(last["payload"], {})
        return isinstance(prev, dict) and prev.get("assignee") == assignee

    try:
        if _already_recorded():
            return False
        with _kb.write_txn(conn):
            if _already_recorded() or not _still_waiting():
                return False
            _kb._append_event(
                conn, task_id, OWNER_UNAVAILABLE_EVENT,
                {
                    "assignee": assignee,
                    "reason": "profile_not_found",
                    "node": _socket.gethostname(),
                    "detail": (
                        f"assignee {assignee!r} is homed on this node but the profile does not exist; "
                        f"create profile with 'hermes profile create {assignee}' or recover with "
                        "one explicit reassign_task(..., reason=...)"
                    ),
                },
            )
        return True
    except sqlite3.Error as exc:
        _kb._log.warning(
            "kanban: could not record owner_unavailable on %s (%s: %s)",
            task_id, type(exc).__name__, exc,
        )
        return False


def dispatch_profile_allowlist_summary() -> str:
    """Human-readable resolution of ``kanban.dispatch_profiles`` for this home.

    Surfaced by ``hermes kanban diagnostics`` so an operator on a shared board
    can see what a home believes it may claim (#113620): ``any`` (key absent),
    the sorted allowed names, or ``none (fail-closed: ...)``.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")


def _owned_assignee_rows(conn: sqlite3.Connection, status: str) -> list:
    """Distinct assignees of unclaimed ``status`` rows THIS Fleet node owns.

    Same ownership rule as :func:`kanban_db._is_foreign_fleet_mirror`, applied
    set-wise (no per-row WARNING: this runs every tick): no Fleet adapter or no
    ``fleet_kanban_issue_map`` row -> local; owner = ``source_node`` only when
    ``current_node`` IS NULL; a blank/NULL owner or an unparseable adapter
    trigger -> foreign. Foreign mirrors are refused by the lease fence by
    design, so counting them made a node holding only foreign cards log
    "dispatcher stuck" forever (t_46ff0060).
    """
    base = ("SELECT DISTINCT t.assignee FROM tasks t "
            "WHERE t.status = ? AND t.assignee IS NOT NULL AND t.claim_lock IS NULL")
    node_id = _kb._fleet_adapter_installed_node_id(conn)
    has_map = node_id is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fleet_kanban_issue_map'"
    ).fetchone() is not None
    if not has_map:
        return conn.execute(base, (status,)).fetchall()
    owner = "(CASE WHEN m.current_node IS NULL THEN m.source_node ELSE m.current_node END)"
    return conn.execute(
        base + " AND NOT EXISTS ("
        "SELECT 1 FROM fleet_kanban_issue_map m WHERE m.local_task_id = t.id AND ("
        f"{owner} IS NULL OR typeof({owner}) <> 'text'"
        # str.strip() parity: space, \t, \n, \v, \f, \r.
        f" OR trim({owner}, char(32, 9, 10, 11, 12, 13)) = ''"
        f" OR {owner} <> ?))",
        (status, node_id),
    ).fetchall()


def _owned_task_rows(conn: sqlite3.Connection, status: str) -> list:
    """All unclaimed ``status`` rows (id, assignee) THIS Fleet node owns."""
    base = ("SELECT t.id, t.assignee FROM tasks t "
            "WHERE t.status = ? AND t.assignee IS NOT NULL AND t.claim_lock IS NULL")
    node_id = _kb._fleet_adapter_installed_node_id(conn)
    has_map = node_id is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fleet_kanban_issue_map'"
    ).fetchone() is not None
    if not has_map:
        return conn.execute(base, (status,)).fetchall()
    owner = "(CASE WHEN m.current_node IS NULL THEN m.source_node ELSE m.current_node END)"
    return conn.execute(
        base + " AND NOT EXISTS ("
        "SELECT 1 FROM fleet_kanban_issue_map m WHERE m.local_task_id = t.id AND ("
        f"{owner} IS NULL OR typeof({owner}) <> 'text'"
        # str.strip() parity: space, \t, \n, \v, \f, \r.
        f" OR trim({owner}, char(32, 9, 10, 11, 12, 13)) = ''"
        f" OR {owner} <> ?))",
        (status, node_id),
    ).fetchall()


def _count_spawnable(conn: sqlite3.Connection, status: str) -> int:
    """Count unclaimed ``status`` tasks owned by this node whose assignee is
    a real profile with dispatch enabled on this node and not held by a respawn guard."""
    rows = _owned_task_rows(conn, status)
    if not rows:
        return 0
    profile_exists = _profile_exists_fn()
    count = 0
    for row in rows:
        assignee = row["assignee"]
        if profile_exists is not None and not profile_exists(assignee):
            continue
        if check_respawn_guard(conn, row["id"], lane=status) not in (None, *_DEGREE_RESPAWN_REASONS):
            continue
        count += 1
    return count


def _count_placeholder(conn: sqlite3.Connection, status: str) -> int:
    """Count unclaimed ``status`` tasks owned by this node whose assignee is
    a placeholder profile without dispatch enabled on this node."""
    rows = _owned_task_rows(conn, status)
    if not rows:
        return 0
    profile_exists = _profile_exists_fn()
    return sum(
        1 for row in rows
        if is_placeholder_profile(row["assignee"]) and (profile_exists is None or not profile_exists(row["assignee"]))
    )


def count_spawnable_ready(conn: sqlite3.Connection) -> int:
    """Count ready tasks owned by this node whose assignee is a real profile
    with dispatch enabled on this node."""
    return _count_spawnable(conn, "ready")


def count_placeholder_ready(conn: sqlite3.Connection) -> int:
    """Count ready tasks owned by this node whose assignee is a placeholder profile
    (default, alpha, beta, orch) without dispatch enabled."""
    return _count_placeholder(conn, "ready")


def count_spawnable_review(conn: sqlite3.Connection) -> int:
    """:func:`count_spawnable_ready` for the review column."""
    return _count_spawnable(conn, "review")


def count_placeholder_review(conn: sqlite3.Connection) -> int:
    """:func:`count_placeholder_ready` for the review column."""
    return _count_placeholder(conn, "review")


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    return _count_spawnable(conn, status) > 0


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile
    with dispatch enabled on this node.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task`` or
    placeholder-assigned cards). Falls back to "any assigned" when
    ``profile_exists`` is unimportable.
    """
    return _count_spawnable(conn, "ready") > 0


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _count_spawnable(conn, "review") > 0


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


# A ``running`` row only occupies a worker slot here when it carries a claim
# or a worker pid. A synced board (fleet Kanban) also holds ``running`` MIRROR
# rows for cards another node is working: no ``claim_lock``, no ``worker_pid``.
# Counting them let other nodes' work fill this host's cap and stop every local
# spawn (t_b19ce8b9). ``worker_pid`` keeps a live local worker whose claim
# bookkeeping broke (``reconcile_orphaned_running`` defers those while the pid
# is alive) inside the count. Every capacity count uses this one predicate.
_CLAIMED_RUNNING_SQL = (
    "status = 'running' AND (claim_lock IS NOT NULL OR worker_pid IS NOT NULL)"
)


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of CLAIMED ``running`` tasks (see ``_CLAIMED_RUNNING_SQL``).

    Feeds the per-board ``max_spawn`` cap and, via
    :func:`count_running_tasks_other_boards`, the host-level
    ``max_in_progress`` budget — the memory-derived cap bounds the machine, not
    the board. Unclaimed ``running`` rows (fleet mirrors of other nodes' work)
    run nowhere on this host and are not counted. Fails open to 0 so a broken
    board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {_CLAIMED_RUNNING_SQL}"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None, *, dry_run: bool = False) -> int:
    """Total CLAIMED ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) yields 0. Fails open per board.
    """
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            if dry_run:
                with _kbc.connect_readonly_closing(board=slug) as other:
                    total += count_running_tasks(other)
            else:
                other = _kbc.connect(board=slug)
                try:
                    total += count_running_tasks(other)
                finally:
                    with contextlib.suppress(Exception):
                        other.close()
        except Exception:
            continue
    return total


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """Run one dispatcher tick under launch admission, including dry runs.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.
    """
    def _locked_tick() -> DispatchResult:
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            reconcile_orphans=reconcile_orphans,
        )

    from hermes_cli.kanban_launch import launch_guard

    try:
        db_path = _kb.kanban_db_path(board=board)
    except Exception as exc:
        _kb._log.warning("kanban dispatch skipped: board path unavailable (%s)", type(exc).__name__)
        return DispatchResult(skipped_locked=True)
    with launch_guard(conn, board=board, db_path=db_path) as held:
        if not held:
            result = DispatchResult(skipped_locked=True)
        else:
            result = _locked_tick()
            # Still under the dispatch lock: periodic PASSIVE WAL checkpoint.
            if not dry_run:
                _kbc._maybe_checkpoint_wal(conn, db_path)
    # Lock released. Fire the tick observer strictly OUTSIDE the critical
    # section: a slow subscriber must never stall a sibling dispatcher's tick.
    if not dry_run:
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    return result


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Pass ``board`` only when the callback supports it.

    A callback must return its worker PID or raise before starting a worker.
    If it cannot establish whether a worker started, raise WorkerHandoffUncertain.
    """
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: "DispatchResult",
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        # A control-plane lane (or a profile another home owns) is expected to
        # sit here quietly. A missing/unavailable local profile is not: leave
        # ONE durable, deduped event so the card reads as an exception an
        # operator can recover (explicit ``reassign_task``), not as idle.
        kind = _nonspawnable_kind(assignee)
        if kind == "placeholder":
            result.skipped_placeholder.append((task_id, assignee))
        elif kind == "missing":
            result.owner_unavailable.append((task_id, assignee))
            if not dry_run:
                _record_owner_unavailable(conn, task_id, assignee)
        return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            if not dry_run:
                _record_degree_warning(conn, task_id, "profile_count_cap")
    guard_reason = check_respawn_guard(
        conn, task_id, lane=lane, lifted=result.respawn_guard_lifted,
    )
    if guard_reason in _DEGREE_RESPAWN_REASONS:
        if not dry_run:
            _record_degree_warning(conn, task_id, guard_reason)
        guard_reason = None
    if guard_reason is not None:
        if not dry_run:
            _record_degree_warning(conn, task_id, guard_reason)
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck.
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            skip_event = False
            last = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if last is not None and last["kind"] == "respawn_guarded":
                prev = _kb._json_or(last["payload"], {})
                skip_event = (
                    isinstance(prev, dict)
                    and prev.get("reason") == guard_reason
                )
            if not skip_event:
                payload = {"reason": guard_reason}
                next_step = _GUARD_NEXT_STEP.get(guard_reason)
                if next_step:
                    payload["next_step"] = next_step.format(task_id=task_id)
                with _kb.write_txn(conn):
                    _kb._append_event(
                        conn, task_id, "respawn_guarded", payload,
                    )
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent
        # ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    now = time.time()
    fenced = _claim_fence.get(task_id)
    if fenced is not None:
        kind, since = fenced["kind"], fenced["since"]
        retry_after = _CLAIM_RETRY_AFTER["foreign"] if kind == "foreign" else (
            _CLAIM_RETRY_AFTER["lease_late"]
            if kind == "lease_wait" and now - since >= _LEASE_WAIT_GRACE_SECONDS else 0
        )
        if now - fenced["last"] < retry_after:
            _claim_fence_errors(result, kind, since, now).append(
                (task_id, "claim: waiting to retry lifecycle fence")
            )
            return False
    try:
        claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    except sqlite3.Error as exc:
        # A known fence means this node holds no verified lease for the task
        # yet: the claim rolled back, the row stays queued, and the rest of the
        # lane (and later phases) still run this tick. Anything else aborts.
        if not _isolate_fenced_row(result.claim_errors, "claim", task_id, exc):
            raise
        _record_degree_warning(conn, task_id, "execution_lease", retry_seconds=300)
        installed_node = _kb._fleet_adapter_installed_node_id(conn)
        if _kb._is_foreign_fleet_mirror(conn, task_id, installed_node):
            kind = "foreign"
        elif _is_canonical_blocked_sync_pending(conn, task_id):
            kind = "held"
        else:
            kind = "lease_wait"
        since = fenced["since"] if fenced is not None and fenced["kind"] == kind else now
        _claim_fence[task_id] = {"kind": kind, "since": since, "last": now}
        _trim_fence_cache(_claim_fence, now, lambda value: value["last"])
        _claim_fence_errors(result, kind, since, now).append((task_id, f"claim: {exc}"))
        return False
    _claim_fence.pop(task_id, None)
    if claimed is None:
        return False
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        result.spawn_errors.append((claimed.id, f"workspace: {exc}"))
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    try:
        from hermes_cli.kanban_launch import (
            HANDOFF_HOLD_REASON, WorkerHandoffUncertain, begin_worker_handoff,
            cancel_worker_handoff, require_launch,
        )

        with require_launch(conn, board=board):
            if spawn_fn is None:
                pid = _call_spawn_fn(_default_spawn, claimed, str(workspace), board)
            else:
                begin_worker_handoff(conn, claimed)
                try:
                    pid = _call_spawn_fn(spawn_fn, claimed, str(workspace), board)
                except WorkerHandoffUncertain:
                    raise
                except Exception:
                    cancel_worker_handoff(conn, claimed)
                    raise
                if not pid:
                    raise WorkerHandoffUncertain(HANDOFF_HOLD_REASON)
                _record_worker_handoff(conn, claimed.id, int(pid))
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        return True
    except Exception as exc:
        from tools.process_registry import RestartSafeScopeUnavailable
        from hermes_cli.kanban_launch import LaunchDeferred, WorkerHandoffUncertain

        if isinstance(exc, WorkerHandoffUncertain):
            _kb._log.error("kanban dispatcher: %s", exc)
            result.spawn_errors.append((claimed.id, str(exc)))
            return False

        # The host refused the spawn (no restart-safe scope): nothing about the
        # card ran, so it must not spend the card's retry budget (#114720).
        infrastructure = isinstance(exc, (RestartSafeScopeUnavailable, LaunchDeferred))
        if infrastructure:
            _kb._log.warning("kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s", claimed.id, exc)
        if _record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
            infrastructure=infrastructure,
        ):
            result.auto_blocked.append(claimed.id)
        result.spawn_errors.append((claimed.id, str(exc)))
        return False


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            updated = conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND status = 'ready' AND claim_lock IS NULL "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            if updated.rowcount != 1:
                return False
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote.

    Every sweep isolates lifecycle-fence refusals per row into
    ``result.reclaim_errors`` so one protected row never aborts the tick.
    """
    reap_worker_zombies()
    result.reaped_terminal_workers = reap_terminal_workers(conn)
    errors = result.reclaim_errors
    # Dead-worker exit evidence is more specific than an expired TTL.
    result.crashed = detect_crashed_workers(
        conn, board=board, errors_out=errors, failure_limit=failure_limit,
    )
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.profile_busy.extend(getattr(detect_crashed_workers, "_last_profile_busy", []))
    result.reclaimed = _kb.release_stale_claims(
        conn, failure_limit=failure_limit, errors_out=errors,
    )
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn, errors_out=errors)
    result.stale = detect_stale_running(
        conn, stale_timeout_seconds=stale_timeout_seconds, errors_out=errors,
    )
    result.timed_out = enforce_max_runtime(conn, errors_out=errors)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)


def _note_capacity_held(result: DispatchResult, reason: str) -> None:
    """Record + log a tick-level capacity hold so a zero-spawn tick names its cause."""
    result.capacity_held = reason
    _kb._log.warning(
        "LOUD WARNING: kanban count budget reached (%s); admitting one more worker after actual "
        "resource checks. Assess confidence/Jev or second model; low confidence goes to a Decider "
        "with this board pointer, not one Conductor.",
        reason,
    )


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
    dry_run: bool = False,
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker makes a terminal
    # board call (kanban_complete/kanban_block/kanban_request_review) or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            _note_capacity_held(result, f"board cap: {running_count} running of {max_spawn}")
            spawn_budget = 1
        else:
            spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board, dry_run=dry_run)
        if total_running >= max_in_progress:
            _note_capacity_held(
                result, f"host cap: {total_running} running of {max_in_progress}",
            )
        remaining = max(1, max_in_progress - total_running)
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Normal ticks have already run reclaim/promotion; deferred tasks wait for
    # a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "retry after the next resource sample; use Jev/second-model confidence and send the board/resource "
            "pointer to a Decider for another host or bounded release. Existing cards stay queued."
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _any_spawnable_review(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    *,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
) -> bool:
    """Mirror review dispatch gates before reserving ready-lane capacity.

    Unavailable profile metadata retains the historic fail-open behavior. A
    review row that :func:`_dispatch_lane_task` would refuse this tick — its
    assignee already at the per-profile cap, or respawn-guarded — cannot
    consume the reservation, so it must not withhold capacity from an
    otherwise ready task (one such row would pin ``ready_budget`` to 0).
    """
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    running = per_profile_running or {}
    for row in review_rows:
        assignee = row["assignee"]
        if not assignee:
            continue
        if profile_exists is not None and not profile_exists(assignee):
            continue
        if check_respawn_guard(conn, row["id"], lane="review") in (None, *_DEGREE_RESPAWN_REASONS):
            return True
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile this home may
    claim (``kanban.dispatch_profiles`` gated, same predicate as the spawn
    gate). Otherwise ``None`` so an unassigned shared-board card is never
    written to. When the profiles module isn't importable trust the
    operator's config: the downstream check still buckets a missing profile
    as nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`."""
    result = DispatchResult()
    if dry_run:
        result.reclaim_phase = "skipped (dry-run)"
    else:
        _run_reclaim_phase(
            conn, result, stale_timeout_seconds=stale_timeout_seconds,
            failure_limit=failure_limit, reconcile_orphans=reconcile_orphans, board=board,
        )
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
        dry_run=dry_run,
    )
    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    # Resolved BEFORE the review reservation so the reservation can see which
    # review rows the lane loop would refuse this tick.
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            f"WHERE {_CLAIMED_RUNNING_SQL} AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            per_profile_running[prow["assignee"]] = int(prow["n"])
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(
        conn, review_rows,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
    ):
        ready_budget = max(spawn_budget - 1, 0)
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    spawned = 0
    for row in ready_rows:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        tok = _CURRENT_DISPATCH_TASK.set((conn, row["id"]))
        try:
            if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
                spawned += 1
        finally:
            _CURRENT_DISPATCH_TASK.reset(tok)

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        tok = _CURRENT_DISPATCH_TASK.set((conn, row["id"]))
        try:
            if _dispatch_lane_task(conn, row, row["assignee"], result, lane="review", **lane_kwargs):
                spawned += 1
        finally:
            _CURRENT_DISPATCH_TASK.reset(tok)
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package).

    A bare interpreter (PM's store Python, no venv) finds ``hermes_cli`` and its
    dependencies only through the PYTHONPATH that boot activation exported, and the
    worker env builder strips that PYTHONPATH as Hermes-owned. ``python -m
    hermes_cli.main`` then dies with "No module named 'hermes_cli'" before the
    worker starts. Use the installation-bound runtime command instead: it pins the
    repo root and runs ``hermes_bootstrap`` (dependency selection) in the child.
    A venv interpreter carries its own packages and keeps the plain module form.
    """
    if sys.prefix == sys.base_prefix:
        try:
            from hermes_cli._launchers import runtime_command

            return runtime_command(Path(__file__).resolve().parents[1], python=sys.executable)
        except Exception:
            pass
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then the running interpreter's ``sys.executable -m
    hermes_cli.main`` (exactly this install; also covers shim-less cron,
    systemd ``User=``, launchd), then ``which("hermes")`` (Windows: safe PATH
    search, batch shims fall back to the module form) only when ``hermes_cli``
    is not importable. The module argv must win over PATH: a PATH-first lookup
    lets an attacker-planted ``hermes`` shadow the running install (#111569).
    Mirrors ``gateway.run._resolve_hermes_bin``; local because ``hermes_cli``
    sits below ``gateway`` in the dependency order.
    """
    import importlib.util
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    try:
        if importlib.util.find_spec("hermes_cli") is not None:
            return _module_hermes_argv()
    except Exception:
        pass

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


@contextlib.contextmanager
def _worker_profile_scope(hermes_home: str, *, bind_home: bool = True):
    """Bind an assigned profile's runtime scope (secrets + terminal policy, optionally home) for
    one dispatch-side read or spawn-env build.

    The dispatcher runs detached from any turn, so nothing binds a profile for it: ``load_config``,
    the toolset probes' ``get_secret`` reads and ``build_subprocess_env``'s passthrough resolution
    all fall back to the LAUNCH profile's ambient ``os.environ`` / ``TERMINAL_*``. Binding was
    previously conditional on ``is_multiplex_active()``, so on a single-profile host a worker for
    profile B was built entirely from the dispatcher's own environment.

    ``bind_home=False`` for the spawn-env build: which variables may cross into a child is the
    DISPATCHER's ``terminal.env_passthrough`` policy (#109494, read through the home override) —
    only their VALUES come from the assignee's scope, so that branch binds the secret scope alone.
    Toolset resolution binds the home and the terminal policy, as it always has.

    The secret mapping is never widened: a profile that is not this process's own home gets its own
    ``.env`` + external sources ONLY, while the launch home keeps its established
    env-over-``.env`` precedence (``launch_secret_scope``) so systemd / ``op run`` injection still
    resolves for a standalone dispatcher.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    from tui_gateway.launch_profile_policy import launch_secret_scope, launch_terminal_env

    home = Path(hermes_home)
    is_launch_home = str(home.resolve()) == str(Path(get_process_hermes_home()).resolve())
    home_token = secret_token = terminal_token = None
    try:
        home_token = set_hermes_home_override(str(home)) if bind_home else None
        secret_token = set_secret_scope(
            launch_secret_scope(home) if is_launch_home else build_profile_secret_scope(home),
            profile_home=None if is_launch_home else str(home))
        terminal_token = install_profile_terminal_scope(
            home, env_overlay=launch_terminal_env() if is_launch_home else None) if bind_home else None
        yield
    finally:
        if terminal_token is not None:
            reset_terminal_scope(terminal_token)
        if secret_token is not None:
            reset_secret_scope(secret_token)
        if home_token is not None:
            reset_hermes_home_override(home_token)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(hermes_home)
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}. Read its recent degree_warning events. " + _DEGREE_NEXT_STEP])
    # goal_mode rides the same `-q` path: cli.py runs the judge loop there too, so the
    # worker log keeps its live tool feed (forcing -Q blanked it).
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: Task, command: list[str]) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv


def _default_spawn(task: Task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import _is_routed_home, build_subprocess_env, strip_launch_profile_env

    try:
        profile_home = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        profile_home = None

    # Scrub for a ROUTED home, not only under multiplex: the authority test is "does this worker act
    # for another profile", exactly as served_profile_child_env decides it (tools/environments/local.py).
    # Gating on the gateway-wide flag left B's worker inheriting the dispatcher's own OPENAI_API_KEY and
    # systemd-injected tokens on every single-profile host.
    routed = bool(profile_home) and _is_routed_home(profile_home)
    # build_subprocess_env's secret scrub resolves terminal.env_passthrough vars through get_secret(),
    # which without a bound scope reads the LAUNCH profile's ambient environment for a worker spawned
    # on B's behalf (and raises under multiplex) — so bind B's secret scope around the build.
    with (_worker_profile_scope(profile_home, bind_home=False) if profile_home
          else contextlib.nullcontext()):
        env = build_subprocess_env(
            scrub_secrets=is_multiplex_active() or routed,
            inherit_profile_home=True,
        )
    # The dispatcher is detached from every conversation; its worker must never
    # inherit routing mirrored by a previous gateway turn.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    if profile_home:
        env["HERMES_HOME"] = profile_home
        # A multiplexer dispatching for another profile must not hand it the launch
        # profile's .env settings / TERMINAL_* policy — a standalone dispatcher never would.
        strip_launch_profile_env(env, profile_home)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    # `--cli` is the highest-precedence TUI override; dropping HERMES_TUI covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))
    # A worker spawned by a managed systemd gateway must leave the gateway's
    # cgroup before startup; otherwise restarting the service kills the worker
    # that is performing the handoff.
    cmd = _restart_safe_worker_argv(task, cmd)
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env(env)
    log_f = _open_worker_log(task, board)
    try:
        from hermes_cli.kanban_launch import (
            begin_worker_handoff, cancel_worker_handoff, require_launch,
        )

        with _kbc.connect_closing(board=board) as conn, require_launch(conn, board=board):
            _kb._assert_not_delegated_child_mutation(_kbc._main_db_file(conn))
            begin_worker_handoff(conn, task)
            try:
                proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
                    cmd,
                    cwd=workspace if os.path.isdir(workspace) else None,
                    stdin=subprocess.DEVNULL,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
                )
            except Exception:
                cancel_worker_handoff(conn, task)
                raise
            _track_worker_proc(proc)
            _record_worker_handoff(conn, task.id, int(proc.pid))
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    except BaseException:
        log_f.close()
        raise
    # Intentionally NOT closing log_f: the child keeps writing after return;
    # the OS-level FD stays open in the child until it exits.
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks. Each tick resolves ``kanban.max_in_progress`` exactly like
    the gateway dispatcher and ``hermes kanban dispatch`` — the standalone
    daemon must not be the one uncapped entry point.
    """
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            max_in_progress = resolve_max_in_progress(configured_max_in_progress())
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_workspace as _kbw  # noqa: E402
