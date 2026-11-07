"""Dispatcher tick result and its zero-spawn diagnostics.

``DispatchResult`` is the facade's public tick outcome (``kanban_db_dispatch`` re-exports it;
``kanban_db`` imports it). The Fleet buckets (claim/reclaim fence refusals, placeholder and
owner-unavailable skips, capacity holds, neutral profile-busy releases) let a zero-spawn tick
name its cause, and ``eligible_work_stalled`` tells eligible work that failed to start from
work that policy correctly held back. Pure: no board access, so the facade can import it first.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Iterable
from typing import Optional


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass (one board, one tick)."""

    reclaimed: int = 0
    reclaim_phase: Optional[str] = None
    """Set when the reclaim and promotion phase was skipped for a dry run."""
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    reclaim_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, "<step>: <reason>")`` for a reclaim write an installed lifecycle-guard
    trigger refused (or whose Fleet ownership could not be verified) this tick. Each row
    is left unchanged and is NOT counted in the reclaim buckets. Observability only:
    never a trigger to bypass the fence."""
    claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, "claim: <fence message>")`` for each ready/review row whose CLAIM
    write a lifecycle-guard trigger refused this tick. The row stayed unclaimed."""
    foreign_claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """Refusals and retry-backoff skips for a foreign Fleet mirror."""
    held_claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """Refusals and backoff skips for a node-owned card whose canonical Fleet status is
    ``blocked`` with sync still ``pending``, or inside the lease-wait grace: holds,
    not dispatcher failures."""
    eligible_claim_errors: list[tuple[str, str]] = field(default_factory=list)
    """Refusals and retry-backoff skips for a node-owned, dispatch-enabled card whose
    lease wait has exceeded the grace period."""
    spawn_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, str(exc))`` for each eligible card whose worker spawn failed."""
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
    """``(task_id, assignee)`` subset of ``skipped_nonspawnable`` whose owning profile is
    homed here but missing. Operator-actionable; the card carries one deduped
    ``owner_unavailable`` event until it is claimed or reassigned."""
    skipped_placeholder: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, assignee)`` subset of ``skipped_nonspawnable`` assigned to a placeholder
    profile (default, alpha, beta, orch) without dispatch enabled: a non-paging count."""
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
    """Task ids whose workers were refused their profile's active-session slot and were
    released WITHOUT counting a failure, behind a bounded backoff."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    capacity_held: Optional[str] = None
    """Concurrency cap that stopped every spawn this tick, e.g. ``"host cap: 12 running
    of 12"`` (``max_in_progress``, all boards) or ``"board cap: 3 running of 3"``
    (``max_spawn``). Only claimed ``running`` rows count (Fleet mirrors of other nodes'
    cards do not)."""


def describe_suppression(results: Iterable[Optional[DispatchResult]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher (#111910).
    """
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    capacity: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        for name, held in (("rate_limited", res.rate_limited), ("profile_busy", res.profile_busy),
                           ("profile_capped", res.skipped_per_profile_capped),
                           ("lease_held", res.held_claim_errors)):
            if held:
                counts[name] = counts.get(name, 0) + len(held)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        pressure = res.memory_pressure or pressure
        capacity = res.capacity_held or capacity
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    if capacity:
        parts.append(capacity)
    return ", ".join(parts)


def _results_only(results: Iterable[Any]) -> list[DispatchResult]:
    """Tick results from either bare results or the gateway's ``(slug, result)`` pairs."""
    out: list[DispatchResult] = []
    for item in results:
        if isinstance(item, tuple) and len(item) == 2 and (isinstance(item[0], str) or item[0] is None):
            item = item[1]
        if isinstance(item, DispatchResult):
            out.append(item)
    return out


def _has_eligible_claim_errors(res: DispatchResult) -> bool:
    if res.eligible_claim_errors:
        return True
    # Foreign mirrors and lease-held cards are expected fence refusals, never failures.
    excluded = {tid for tid, _ in res.foreign_claim_errors} | {tid for tid, _ in res.held_claim_errors}
    return any(tid not in excluded for tid, _ in res.claim_errors)


def eligible_work_stalled(results: Iterable[Any], ready_spawnable: int) -> bool:
    """True when eligible ready work itself failed to start this tick.

    A spawn failure, an auto-block, or a claim refusal on a node-owned dispatch-enabled
    card is a stall that no other board's progress may mask. Policy holds (respawn guard,
    host/board caps, critical memory pressure, per-profile caps, profile busy, rate limits,
    lease holds) and placeholder/foreign cards are NOT stalls: ``ready_spawnable``
    (``count_spawnable_ready``) already excludes guarded, placeholder and foreign rows.
    """
    res_list = _results_only(results)
    if any(r.auto_blocked or r.spawn_errors for r in res_list):
        return True
    if ready_spawnable <= 0:
        return False
    if any(_has_eligible_claim_errors(r) for r in res_list):
        return True
    if not res_list:
        return True
    if any(r.spawned for r in res_list):
        return False
    if any((r.capacity_held or "").startswith("host cap") or r.memory_pressure == "critical"
           for r in res_list):
        return False
    if all(r.capacity_held or r.skipped_locked for r in res_list):
        return False
    held = sum(
        len(r.skipped_per_profile_capped) + len(r.profile_busy) + len(r.rate_limited)
        + len(r.held_claim_errors)
        for r in res_list if not (r.capacity_held or r.skipped_locked)
    )
    return ready_spawnable > held
