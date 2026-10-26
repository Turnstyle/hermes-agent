"""Fleet dispatch-loop policy: tick results, placeholder/nonspawnable owners, capacity and claim fences.

The owner-managed worker path (``kanban_worker_client`` -> owner ``kanban.run`` admission)
decides how an admitted claim executes. Which ready rows THIS node may claim at all, how a
lifecycle-fence refusal on the claim write is isolated and backed off, which ``running`` rows
occupy a local worker slot, and how a zero-spawn tick names its cause are fleet board policy,
reached from the ``kanban_db_dispatch`` facade (``dispatch_once`` and its lane loop).
"""

from __future__ import annotations

import fnmatch
import socket
import sqlite3
import time
from typing import Any
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

from hermes_cli.kanban_db_dispatch_guard import _DEGREE_RESPAWN_REASONS
from hermes_cli.kanban_db_dispatch_fence import (
    _CLAIM_RETRY_AFTER,
    _LEASE_WAIT_GRACE_SECONDS,
    _claim_fence,
    _is_canonical_blocked_sync_pending,
    _isolate_fenced_row,
    _trim_fence_cache,
)

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch_result import DispatchResult


# ---------------------------------------------------------------------------
# Placeholder / nonspawnable owners
# ---------------------------------------------------------------------------

PLACEHOLDER_PROFILES: frozenset[str] = frozenset({"default", "alpha", "beta", "orch"})
OWNER_UNAVAILABLE_EVENT = "owner_unavailable"


def _canonical_profile(name: str) -> str:
    """Normalized profile name; raises ``ValueError`` for a name no profile can have."""
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(name)


def is_placeholder_profile(name: Optional[str]) -> bool:
    """True when *name* is a synthetic/placeholder pool profile
    (``default``, ``alpha``, ``beta``, ``orch``) with no gateway worker."""
    if not name or not isinstance(name, str):
        return False
    try:
        canon = _canonical_profile(name)
    except ValueError:
        canon = name.strip().lower()
    return canon in PLACEHOLDER_PROFILES


def dispatch_enabled(name: str, allowlist: Optional[frozenset], profile_exists) -> bool:
    """The one spawn-gate rule: listed (when ``kanban.dispatch_profiles`` is set) or not a
    placeholder (when unset), and an existing profile on this node."""
    try:
        canon = _canonical_profile(name)
    except ValueError:
        return False
    if allowlist is not None:
        return canon in allowlist and bool(profile_exists(name))
    if canon in PLACEHOLDER_PROFILES:
        return False
    return bool(profile_exists(name))


def is_dispatch_enabled_profile(name: str) -> bool:
    """True when *name* is a real profile with dispatch enabled on this node."""

    gate = _kd._profile_exists_fn()
    return True if gate is None else gate(name)


def _control_plane_lane_patterns() -> tuple[str, ...]:
    """``kanban.control_plane_lanes``: assignee names/``fnmatch`` patterns served by
    something other than this home's dispatcher (terminal lanes that pull via
    ``claim_task``, or profiles another home owns on a shared board). Absent, empty or
    unreadable config yields ``()``: no assignee is exempted as a lane."""

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
    raw = kanban.get("control_plane_lanes") if isinstance(kanban, Mapping) else None
    if not raw:
        return ()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return tuple(n.strip().lower() for n in names if n and n.strip())


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone() is not None


def _fleet_owner_verdict(owner: Any, node: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        return "foreign"
    return "missing" if owner.strip().lower() == node else "foreign"


def _check_fleet_nonspawnable(conn: sqlite3.Connection, name: str, *, task_id: Optional[str] = None) -> Optional[str]:
    """Fleet verdict for a nonspawnable assignee: ``"placeholder"``, ``"foreign"`` (another
    node owns the assignee home or the card), ``"missing"`` (this node is the verified
    owner) or ``None`` (no Fleet adapter, or no Fleet evidence: use the local rules)."""

    node_id = _kb._fleet_adapter_installed_node_id(conn)
    if not node_id:
        return None
    node = node_id.strip().lower()
    if is_placeholder_profile(name):
        return "placeholder"
    try:
        if _has_table(conn, "fleet_kanban_assignee_homes"):
            home = conn.execute(
                "SELECT home_node FROM fleet_kanban_assignee_homes WHERE LOWER(assignee) = LOWER(?)",
                (name,),
            ).fetchone()
            if home is not None and home[0]:
                return _fleet_owner_verdict(str(home[0]), node)
        if not _has_table(conn, "fleet_kanban_issue_map"):
            return None
        if task_id:
            row = conn.execute(
                "SELECT current_node, source_node FROM fleet_kanban_issue_map WHERE local_task_id = ?",
                (task_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT m.current_node, m.source_node FROM fleet_kanban_issue_map m "
                "JOIN tasks t ON m.local_task_id = t.id WHERE LOWER(t.assignee) = LOWER(?) "
                "ORDER BY t.id DESC LIMIT 1",
                (name,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _fleet_owner_verdict(row[1] if row[0] is None else row[0], node)


def _nonspawnable_kind(assignee: str, conn: Optional[sqlite3.Connection] = None,
                       task_id: Optional[str] = None) -> str:
    """Why ``assignee`` failed the spawn gate: ``"lane"`` (configured control-plane lane),
    ``"placeholder"`` (placeholder pool profile with no worker), ``"foreign"`` (not in this
    home's ``dispatch_profiles``, or another Fleet node owns the card/assignee) or
    ``"missing"`` (this home should run it but has no such profile)."""

    name = (assignee or "").strip().lower()
    if any(fnmatch.fnmatchcase(name, pat) for pat in _control_plane_lane_patterns()):
        return "lane"
    if conn is not None:
        fleet = _check_fleet_nonspawnable(conn, name, task_id=task_id)
        if fleet is not None:
            return fleet
    try:
        canon = _canonical_profile(assignee)
    except ValueError:
        return "missing"
    from hermes_cli.profiles import normalize_profile_name

    allowlist = _kd._dispatch_profile_allowlist(normalize_profile_name)
    if canon in PLACEHOLDER_PROFILES and (allowlist is None or canon not in allowlist):
        return "placeholder"
    if allowlist is not None and canon not in allowlist:
        return "foreign"
    return "missing"


def _owner_unavailable_recorded(conn: sqlite3.Connection, task_id: str, assignee: str) -> bool:

    last = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN (?, 'assigned', 'claimed') ORDER BY id DESC LIMIT 1",
        (task_id, OWNER_UNAVAILABLE_EVENT),
    ).fetchone()
    if last is None or last["kind"] != OWNER_UNAVAILABLE_EVENT:
        return False
    prev = _kb._json_or(last["payload"], {})
    return isinstance(prev, dict) and prev.get("assignee") == assignee


def _still_waiting_for(conn: sqlite3.Connection, task_id: str, assignee: str) -> bool:
    row = conn.execute(
        "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return (row is not None and row["status"] in ("ready", "review")
            and row["assignee"] == assignee and row["claim_lock"] is None)


def _record_owner_unavailable(conn: sqlite3.Connection, task_id: str, assignee: str) -> bool:
    """Append ONE ``owner_unavailable`` event per unavailability episode.

    Deduped against the task's latest ``owner_unavailable`` / ``assigned`` / ``claimed``
    event: repeat ticks write nothing; a reassignment or a later claim starts a new
    episode. The repeat-tick case is a plain read; the check repeats inside the write
    transaction. A write refused by a board fence is logged and skipped — it must never
    abort the tick. Returns True when a row was written.
    """

    try:
        if _owner_unavailable_recorded(conn, task_id, assignee):
            return False
        with _kb.write_txn(conn):
            if (_owner_unavailable_recorded(conn, task_id, assignee)
                    or not _still_waiting_for(conn, task_id, assignee)):
                return False
            _kb._append_event(conn, task_id, OWNER_UNAVAILABLE_EVENT, {
                "assignee": assignee,
                "reason": "profile_not_found",
                "node": socket.gethostname(),
                "detail": (
                    f"assignee {assignee!r} is homed on this node but the profile does not exist; "
                    f"create profile with 'hermes profile create {assignee}' or recover with "
                    "one explicit reassign_task(..., reason=...)"
                ),
            })
        return True
    except sqlite3.Error as exc:
        _kb._log.warning(
            "kanban: could not record owner_unavailable on %s (%s: %s)",
            task_id, type(exc).__name__, exc,
        )
        return False


def skip_nonspawnable(conn: sqlite3.Connection, task_id: str, assignee: str,
                      result: DispatchResult, *, dry_run: bool) -> None:
    """Bucket a row whose assignee failed the spawn gate.

    A control-plane lane, a foreign profile and a placeholder pool profile are expected
    to sit quietly (no per-tick event). A missing/unavailable LOCAL owner is not: it gets
    one durable, deduped event so the card reads as an exception an operator can
    recover (explicit reassign), not as idle.
    """
    result.skipped_nonspawnable.append(task_id)
    kind = _nonspawnable_kind(assignee, conn, task_id)
    if kind == "placeholder":
        result.skipped_placeholder.append((task_id, assignee))
    elif kind == "missing":
        result.owner_unavailable.append((task_id, assignee))
        if not dry_run:
            _record_owner_unavailable(conn, task_id, assignee)
            _note_skipped_nonspawnable(conn, task_id, assignee)


def _note_skipped_nonspawnable(conn: sqlite3.Connection, task_id: str, assignee: str) -> None:
    """Upstream's once-per-episode ``skipped_nonspawnable`` event (#122422): a repeat only
    when something else happened on the card since. Written after ``owner_unavailable`` so
    the pair stays at one row each across ticks."""

    with _kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if (last is None or last["kind"] != "skipped_nonspawnable"
                or last["payload"] != _kb._json_or_null({"assignee": assignee})):
            _kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})


def guard_holds(task_id: str, guard_reason: Optional[str]) -> bool:
    """Whether a respawn-guard verdict holds the row back from claim this tick.

    A degree reason (``_DEGREE_RESPAWN_REASONS``) is advisory only. A lease refusal has two
    throttles: the durable ``execution_lease_retry`` guard covers a restarted process, while
    in-process the claim-fence backoff paces the retry AND keeps a late lease wait in the
    stall buckets — a guard hold there would hide eligible work from "dispatcher stuck".
    """
    if guard_reason is None or guard_reason in _DEGREE_RESPAWN_REASONS:
        return False
    return not (guard_reason == "execution_lease_retry" and task_id in _claim_fence)


def lane_held(conn: sqlite3.Connection, task_id: str, assignee: str, result: DispatchResult, *,
              lane: str, dry_run: bool, per_profile_cap: Optional[int],
              per_profile_running: dict[str, int]) -> bool:
    """True (and the hold recorded on ``result``) when a spawnable row must wait this tick.

    Per-profile cap: one profile's local model / API quota / browser pool must not be
    overwhelmed by a fan-out even with global headroom. A respawn guard whose reason is
    only a degree (``_DEGREE_RESPAWN_REASONS``) leaves an advisory and still dispatches;
    any other guard holds the row with one deduped ``respawn_guarded`` event. A dry run
    writes nothing.
    """

    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            if not dry_run:
                _kd._record_degree_warning(conn, task_id, "profile_count_cap")
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return True
    guard_reason = _kd.check_respawn_guard(conn, task_id, lane=lane, lifted=result.respawn_guard_lifted)
    if guard_reason is None:
        return False
    if not dry_run:
        _kd._record_degree_warning(conn, task_id, guard_reason)
    if not guard_holds(task_id, guard_reason):
        return False
    result.respawn_guarded.append((task_id, guard_reason))
    if not dry_run:
        record_respawn_guarded(conn, task_id, guard_reason)
    return True


# ---------------------------------------------------------------------------
# Node-owned row counts (health telemetry)
# ---------------------------------------------------------------------------

# A ``running`` row only occupies a worker slot here when it carries a claim or a worker
# pid. A synced Fleet board also holds ``running`` MIRROR rows for cards another node is
# working: no ``claim_lock``, no ``worker_pid``. Counting them let other nodes' work fill
# this host's cap and stop every local spawn. Every capacity count uses this predicate.
_CLAIMED_RUNNING_SQL = "status = 'running' AND (claim_lock IS NOT NULL OR worker_pid IS NOT NULL)"


def _owned_task_rows(conn: sqlite3.Connection, status: str) -> list:
    """Unclaimed ``status`` rows ``(id, assignee)`` THIS Fleet node owns.

    Same ownership rule as ``kanban_db._is_foreign_fleet_mirror`` applied set-wise (no
    per-row WARNING: this runs every tick): no adapter or no map row -> local; owner =
    ``source_node`` only when ``current_node`` IS NULL; a blank/NULL/non-text owner ->
    foreign. Foreign mirrors are refused by the lease fence by design, so counting them
    made a node holding only foreign cards log "dispatcher stuck" forever.
    """

    base = ("SELECT t.id, t.assignee FROM tasks t "
            "WHERE t.status = ? AND t.assignee IS NOT NULL AND t.claim_lock IS NULL")
    node_id = _kb._fleet_adapter_installed_node_id(conn)
    if node_id is None or not _has_table(conn, "fleet_kanban_issue_map"):
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
    """Unclaimed node-owned ``status`` rows whose assignee passes the spawn gate and that no
    holding respawn guard defers (an advisory degree reason still dispatches)."""

    rows = _owned_task_rows(conn, status)
    if not rows:
        return 0
    gate = _kd._profile_exists_fn()
    return sum(
        1 for row in rows
        if (gate is None or gate(row["assignee"]))
        and not guard_holds(row["id"], _kd.check_respawn_guard(conn, row["id"], lane=status))
    )


def _count_placeholder(conn: sqlite3.Connection, status: str) -> int:
    """Unclaimed node-owned ``status`` rows assigned to a placeholder profile without
    dispatch enabled on this node."""

    rows = _owned_task_rows(conn, status)
    if not rows:
        return 0
    gate = _kd._profile_exists_fn()
    return sum(1 for row in rows
               if is_placeholder_profile(row["assignee"]) and (gate is None or not gate(row["assignee"])))


def count_spawnable_ready(conn: sqlite3.Connection) -> int:
    """Ready tasks owned by this node whose assignee is dispatch-enabled here."""
    return _count_spawnable(conn, "ready")


def count_placeholder_ready(conn: sqlite3.Connection) -> int:
    """Ready tasks owned by this node assigned to a placeholder profile."""
    return _count_placeholder(conn, "ready")


def count_spawnable_review(conn: sqlite3.Connection) -> int:
    """:func:`count_spawnable_ready` for the review column."""
    return _count_spawnable(conn, "review")


def count_placeholder_review(conn: sqlite3.Connection) -> int:
    """:func:`count_placeholder_ready` for the review column."""
    return _count_placeholder(conn, "review")


def note_capacity_held(result: DispatchResult, reason: str) -> None:
    """Record + log a tick-level capacity hold so a zero-spawn tick names its cause."""

    result.capacity_held = reason
    _kb._log.warning("kanban dispatch: %s; deferring new workers (cards stay queued)", reason)


def count_board_running(board_slug: str, *, dry_run: bool) -> int:
    """Claimed ``running`` rows on another board; a dry run opens it read-only."""

    if dry_run:
        with _kbc.connect_readonly_closing(board=board_slug) as other:
            return _kd.count_running_tasks(other)
    other = _kbc.connect(board=board_slug)
    try:
        return _kd.count_running_tasks(other)
    finally:
        other.close()


# ---------------------------------------------------------------------------
# Lane-loop fences: respawn-guard events and the claim write
# ---------------------------------------------------------------------------

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


def record_respawn_guarded(conn: sqlite3.Connection, task_id: str, guard_reason: str) -> None:
    """One ``respawn_guarded`` event per hold reason (not one per tick), with the
    operator's next step when the reason has one."""

    last = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if last is not None and last["kind"] == "respawn_guarded":
        prev = _kb._json_or(last["payload"], {})
        if isinstance(prev, dict) and prev.get("reason") == guard_reason:
            return
    payload = {"reason": guard_reason}
    next_step = _GUARD_NEXT_STEP.get(guard_reason)
    if next_step:
        payload["next_step"] = next_step.format(task_id=task_id)
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, "respawn_guarded", payload)


def _claim_fence_errors(result: DispatchResult, kind: str, since: float, now: float) -> list:
    if kind == "foreign":
        return result.foreign_claim_errors
    if kind == "held" or now - since < _LEASE_WAIT_GRACE_SECONDS:
        return result.held_claim_errors
    return result.eligible_claim_errors


def _claim_backoff_due(task_id: str, result: DispatchResult, now: float) -> bool:
    """True (and the skip recorded) while a fenced claim waits out its retry backoff."""
    fenced = _claim_fence.get(task_id)
    if fenced is None:
        return False
    kind, since = fenced["kind"], fenced["since"]
    if kind == "foreign":
        retry_after = _CLAIM_RETRY_AFTER["foreign"]
    elif kind == "lease_wait" and now - since >= _LEASE_WAIT_GRACE_SECONDS:
        retry_after = _CLAIM_RETRY_AFTER["lease_late"]
    else:
        retry_after = 0
    if now - fenced["last"] >= retry_after:
        return False
    _claim_fence_errors(result, kind, since, now).append((task_id, "claim: waiting to retry lifecycle fence"))
    return True


def _book_claim_fence(conn: sqlite3.Connection, task_id: str, result: DispatchResult,
                      exc: sqlite3.Error, now: float) -> None:

    _kd._record_degree_warning(conn, task_id, "execution_lease", retry_seconds=300)
    node_id = _kb._fleet_adapter_installed_node_id(conn)
    if _kb._is_foreign_fleet_mirror(conn, task_id, node_id):
        kind = "foreign"
    elif _is_canonical_blocked_sync_pending(conn, task_id):
        kind = "held"
    else:
        kind = "lease_wait"
    fenced = _claim_fence.get(task_id)
    since = fenced["since"] if fenced is not None and fenced["kind"] == kind else now
    _claim_fence[task_id] = {"kind": kind, "since": since, "last": now}
    _trim_fence_cache(_claim_fence, now, lambda value: value["last"])
    _claim_fence_errors(result, kind, since, now).append((task_id, f"claim: {exc}"))


def fenced_claim(conn: sqlite3.Connection, task_id: str, claim, result: DispatchResult, **claim_kwargs):
    """``claim(conn, task_id, **claim_kwargs)`` with lifecycle-fence isolation and backoff.

    A known fence means this node holds no verified lease for the card yet: the claim
    rolled back, the row stays queued, the rest of the lane still runs this tick, and the
    row is retried on a bounded backoff by kind (foreign mirror, held, late lease). Any
    other database error aborts the tick. Returns the claimed task or ``None``.
    """
    now = time.time()
    if _claim_backoff_due(task_id, result, now):
        return None
    try:
        claimed = claim(conn, task_id, **claim_kwargs)
    except sqlite3.Error as exc:
        if not _isolate_fenced_row(result.claim_errors, "claim", task_id, exc):
            raise
        _book_claim_fence(conn, task_id, result, exc, now)
        return None
    _claim_fence.pop(task_id, None)
    return claimed


# Late-bound origin namespaces (the facade imports this module LAST, after ``kanban_db``):
# bound at import so a facade always reaches the kanban_db/facade objects it was loaded with.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_dispatch as _kd  # noqa: E402


def launch_paused() -> bool:
    """Pre-claim ESTOP / drain gate (``kanban_launch.pause_reason``, fail closed): a paused host
    claims nothing this tick. ``require_launch`` still re-checks just before ``Popen`` for a pause
    that lands between the claim and the spawn."""
    from hermes_cli.kanban_launch import pause_reason
    return pause_reason() is not None
