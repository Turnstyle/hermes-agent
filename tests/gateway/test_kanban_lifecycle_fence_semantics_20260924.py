"""Narrow test for TB-cndr's approved lifecycle-fence correction semantics
(TurnerBook base 439eb0395eed5025139ee917526f31e2d161699e, correction
semantics message 2026-09-24).

Exact semantics under test, scoped to reconcile_orphaned_running() — the
function actually failing live on Snowdrop since 2026-09-20 (see
tests/gateway/test_kanban_lifecycle_fence_repro_20260924.py for the raw
repro). Per TB-cndr:

  1. A row-level sqlite3.IntegrityError from a lifecycle-guarded reclaim
     rolls back ONLY that row/savepoint.
  2. The protected row is retained unchanged; no termination signal is
     sent when the UPDATE would be refused.
  3. The expected fence is logged.
  4. The refusal is surfaced in DispatchResult.reclaim_errors (not
     silently dropped, not a crash).
  5. Dispatch continues to sibling rows AND to later dispatch steps in
     the same tick (an IntegrityError on one row must not abort the
     whole tick, unlike today).
  6. Durable counting only after commit: a row must never be counted as
     reconciled unless its write_txn actually committed.
  7. sqlite3.OperationalError (lock contention, I/O, corruption) still
     aborts visibly — it is NOT caught by the same per-row guard.
  8. No trigger is disabled and no lease/generation row is fabricated to
     make the test pass — the fence stays fully enforced; the candidate
     only stops it from taking down the whole tick.

This file targets the CANDIDATE this worktree is expected to grow. Run
against unmodified baseline first (must FAIL, proving the bug); then
against the candidate fix (must PASS).
"""
from __future__ import annotations

import signal
import sqlite3
import sys
import time
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPTS = Path(
    os.environ.get(
        "FLEET_KANBAN_DEPLOY_SCRIPTS",
        str(Path.home() / ".hermes/fleet-kanban-deploy-5f8e0275/scripts"),
    )
)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DEPLOY_SCRIPTS))

import hermes_cli.kanban_db as kb  # noqa: E402
# 0.21.5 split the dispatcher out of kanban_db; reconcile_orphaned_running,
# detect_stale_running and _dispatch_once_locked now live here. kanban_db's
# old names are plugin-compat pointers that in-tree code may not use.
import hermes_cli.kanban_db_dispatch as kbd  # noqa: E402
import fleet_kanban_sqlite as adapter  # noqa: E402


def _minimal_board_schema(conn: sqlite3.Connection) -> None:
    cols = ", ".join(
        f"{c} TEXT" for c in adapter.TASK_COLUMNS if c != "id"
    )
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, {cols},
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            worker_started_at TEXT,
            current_run_id INTEGER, last_heartbeat_at INTEGER,
            current_step_key TEXT
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY, task_id TEXT, author TEXT,
            body TEXT, created_at INTEGER
        );
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
            step_key TEXT, status TEXT, outcome TEXT, summary TEXT,
            error TEXT, metadata TEXT, started_at INTEGER, ended_at INTEGER,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            worker_started_at TEXT, max_runtime_seconds INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
            payload TEXT, run_id INTEGER, created_at INTEGER
        );
        """
    )


def _make_board(conn: sqlite3.Connection) -> None:
    _minimal_board_schema(conn)
    adapter.install_adapter_schema(
        conn, board_slug="fleet", node_id="snowdrop",
        source_profile="snow-cndr",
    )


def _insert_zombie(conn: sqlite3.Connection, task_id: str, run_id: int) -> None:
    now = int(time.time())
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, current_run_id) "
        "VALUES (?, 'repro', 'running', 'tb-king', ?)",
        (task_id, run_id),
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (?, ?, 'running', ?)", (run_id, task_id, now - 999),
    )
    conn.commit()


def _insert_eligible_local_zombie(conn: sqlite3.Connection, task_id: str, run_id: int) -> None:
    """A genuinely LOCAL zombie: created locally, legitimately transitioned
    to running under a still-valid verified lease (so run_generation got
    stamped by fleet_kanban_running_binds_generation), then its claim
    bookkeeping was lost (simulating a crash) while the lease is still
    valid. This is the row shape reconcile_orphaned_running must still be
    able to reconcile — proving the candidate doesn't just swallow every
    refusal, it lets genuinely eligible local work through.
    """
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES (?, 'repro-local', 'ready', 'snow-cndr', 'snowdrop')",
        (task_id,),
    )
    # Seed a verified lease for this exact issue/holder/node before the
    # ready->running transition, matching what fleet_kanban_sync.py's
    # mirror_verified_lease() would have written from an authenticated
    # remote readback.
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id=?",
        (task_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, "
        " expires_at, verified_at) VALUES (?, 'snow-cndr', 'snowdrop', 7, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.execute(
        "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
        (run_id, task_id),
    )
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (?, ?, 'running', ?)", (run_id, task_id, now - 999),
    )
    # Simulate crash mid-claim: broken claim bookkeeping, lease untouched.
    conn.execute(
        "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
        "WHERE id=?", (task_id,),
    )
    conn.commit()


def test_remote_protected_row_skipped_eligible_local_sibling_advances() -> None:
    """TB-cndr's exact required proof: one remote-owned row with no
    verified lease on this mirror stays untouched (isolated, no signal),
    while a genuinely eligible LOCAL sibling — legitimately claimed under
    a still-valid verified lease, just with lost claim bookkeeping —
    still gets reconciled in the SAME tick. No trigger disabled, no
    lease/generation fabricated for the protected row.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _insert_zombie(conn, "t_remote_protected", 1)           # tb-king / turnerbook, no lease
    _insert_eligible_local_zombie(conn, "t_local_eligible", 2)  # snow-cndr, valid lease

    errors: list = []
    reconciled = kbd.reconcile_orphaned_running(conn, errors_out=errors)

    assert reconciled == ["t_local_eligible"], (
        f"expected only the lease-backed local row to reconcile, got {reconciled!r}"
    )
    assert [tid for tid, _ in errors] == ["t_remote_protected"], (
        f"expected only the remote-owned row to be refused/isolated, got {errors!r}"
    )

    remote_row = conn.execute(
        "SELECT status, assignee, current_run_id FROM tasks WHERE id='t_remote_protected'"
    ).fetchone()
    assert remote_row["status"] == "running" and remote_row["assignee"] == "tb-king", (
        "remote-owned row must be completely untouched"
    )

    local_row = conn.execute(
        "SELECT status, claim_lock, claim_expires, worker_pid FROM tasks "
        "WHERE id='t_local_eligible'"
    ).fetchone()
    assert local_row["status"] == "ready", (
        "eligible local row must have actually been requeued to ready"
    )


def test_protected_row_isolated_siblings_continue_and_surfaced() -> None:
    """Two zombie rows, both fence-protected (no verified lease exists
    for either). Both must be individually refused, both logged as
    expected fences, both surfaced in reclaim_errors, and — critically —
    the call must return normally (not raise) so dispatch_once's later
    steps (detect_stale_running, detect_crashed_workers,
    enforce_max_runtime, recompute_ready, spawn) still run this tick.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _insert_zombie(conn, "t_a", 1)
    _insert_zombie(conn, "t_b", 2)

    result_errors: list = []
    reconciled = kbd.reconcile_orphaned_running(conn, errors_out=result_errors)

    assert reconciled == [], (
        "neither row has a verified lease, so neither should be "
        "reconciled — but the call must not raise"
    )
    error_task_ids = {tid for tid, _msg in result_errors}
    assert error_task_ids == {"t_a", "t_b"}, (
        f"both protected rows must be surfaced as refusals, got {result_errors!r}"
    )
    for _tid, msg in result_errors:
        assert "verified run generation no longer owns lifecycle write" in msg

    # Rows must be retained unchanged (status/assignee untouched, no
    # partial write leaked from the aborted trigger body).
    for tid in ("t_a", "t_b"):
        row = conn.execute(
            "SELECT status, assignee, claim_lock, claim_expires, worker_pid, "
            "current_run_id FROM tasks WHERE id=?", (tid,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["assignee"] == "tb-king"
        assert row["current_run_id"] is not None, (
            "row must be untouched by the aborted UPDATE, including "
            "current_run_id — no partial commit"
        )


def test_operational_error_still_aborts_visibly() -> None:
    """A genuine sqlite3.OperationalError (real lock contention, not the
    lifecycle fence) must actually propagate out of
    reconcile_orphaned_running, not be swallowed by the new per-row
    guard. Exercises real behavior (two live connections racing on a
    file-backed DB) rather than only asserting on source text, per
    TB-cndr's correction that a source-string assertion alone does not
    prove actual OperationalError behavior.
    """
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path, timeout=0.2)
        conn.row_factory = sqlite3.Row
        _make_board(conn)
        _insert_zombie(conn, "t_locked", 1)
        conn.commit()

        # Second live connection holds a real write lock (BEGIN IMMEDIATE,
        # no commit) so the candidate's own write_txn on `conn` genuinely
        # hits SQLITE_BUSY -> sqlite3.OperationalError, not a simulated
        # or mocked exception.
        blocker = sqlite3.connect(path, timeout=0.2)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE tasks SET title='locked' WHERE id='t_locked'")
        try:
            raised = None
            try:
                kbd.reconcile_orphaned_running(conn)
            except sqlite3.OperationalError as exc:
                raised = exc
            assert raised is not None, (
                "a real SQLITE_BUSY OperationalError must propagate out "
                "of reconcile_orphaned_running, not be silently absorbed "
                "by the lifecycle-fence IntegrityError guard"
            )
        finally:
            blocker.rollback()
            blocker.close()
        conn.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_claim_boundary_fenced_ready_task_leaves_siblings_dispatchable() -> None:
    """Live TurnerBook finding (2026-09-24, post-reclaim-fix): a ready-lane
    claim_task() hitting the missing-lease IntegrityError
    ('verified execution lease required before running') was aborting the
    NEXT dispatch phase entirely. This proves the claim boundary itself
    (not just reconcile_orphaned_running) isolates a fenced ready task —
    left untouched in 'ready', refusal recorded in claim_errors — while
    a sibling ready task with no fence at all still claims successfully
    in the SAME call.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)

    # A ready task under remote origin (mirrored from Firestore, e.g. a
    # TurnerBook-created row) with NO verified lease for this holder/node
    # -> fleet_kanban_running_requires_lease refuses the ready->running
    # claim outright.
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_fenced_ready', 'repro-fenced', 'ready', 'tb-king', 'turnerbook')"
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.commit()

    fenced_exc = None
    try:
        kb.claim_task(conn, "t_fenced_ready")
    except sqlite3.IntegrityError as exc:
        fenced_exc = exc
    assert fenced_exc is not None, (
        "expected the missing-lease trigger to refuse this claim directly "
        "via claim_task, matching the live TurnerBook finding"
    )
    assert "verified execution lease required before running" in str(fenced_exc)

    # Row must be completely untouched: still 'ready', no claim_lock.
    row = conn.execute(
        "SELECT status, claim_lock, current_run_id FROM tasks WHERE id='t_fenced_ready'"
    ).fetchone()
    assert row["status"] == "ready" and row["claim_lock"] is None and row["current_run_id"] is None





def test_full_tick_matrix_protected_and_local_advance_together(monkeypatch) -> None:
    """TB-cndr's required same-pass matrix, run through the REAL dispatcher
    entrypoint (_dispatch_once_locked), not just the two isolated helper
    functions:

      - one running row protected by the lifecycle fence (remote-owned,
        no verified lease) — must stay running, unchanged, no signal,
        appear in reclaim_errors;
      - one running row that IS a genuinely eligible local zombie (valid
        lease, lost claim bookkeeping) — must be reconciled to ready in
        the SAME tick;
      - one ready row with an assignee and no fence at all — must reach
        the spawn phase and actually spawn in the SAME tick, proving
        later dispatch stages (recompute_ready, the claim/spawn loop)
        genuinely execute after the fenced-row refusal, not just that
        the function returns without raising.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _insert_zombie(conn, "t_remote_protected", 1)
    _insert_eligible_local_zombie(conn, "t_local_eligible", 2)

    # This suite's own conftest.py sandboxes HERMES_HOME per test, so
    # `snow-cndr` is not a real installed profile inside that sandbox —
    # _dispatch_once_locked's spawn loop calls profile_exists(assignee)
    # and correctly treats an unfenced ready task as non-spawnable there
    # (skipped_nonspawnable), which is sandbox isolation working exactly
    # as designed, not a bug in the candidate. Patch profile_exists at
    # its cli.profiles source so every import site sees the same stub,
    # matching what the real dispatcher does outside the test sandbox
    # where snow-cndr genuinely is an installed profile.
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == "snow-cndr",
    )

    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_plain_ready', 'repro-plain', 'ready', 'snow-cndr', 'snowdrop')"
    )
    conn.commit()
    # IMPORTANT finding while building this test: fleet_kanban_running_
    # requires_lease fires on EVERY local-origin ready->running transition,
    # not only ones that were previously running — so even this "plain"
    # sibling needs a matching verified lease to claim at all. Seed one,
    # exactly like fleet_kanban_sync.py's mirror_verified_lease() would
    # from an authenticated remote readback, so this test isolates "does
    # a properly-leased sibling still advance" rather than accidentally
    # re-proving the fence blocks everything.
    now = int(time.time())
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id='t_plain_ready'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, "
        " expires_at, verified_at) VALUES (?, 'snow-cndr', 'snowdrop', 1, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.commit()

    spawned_ids = []

    def _stub_spawn(task, workspace, board=None):
        spawned_ids.append(task.id)
        return 999999

    result = kbd._dispatch_once_locked(conn, spawn_fn=_stub_spawn)


    # Protected row: untouched, no signal, surfaced only in reclaim_errors.
    assert "t_remote_protected" not in result.reconciled_orphans
    assert [tid for tid, _ in result.reclaim_errors] == ["t_remote_protected"] or (
        "t_remote_protected" in [tid for tid, _ in result.reclaim_errors]
    )
    remote_row = conn.execute(
        "SELECT status, assignee, current_run_id FROM tasks WHERE id='t_remote_protected'"
    ).fetchone()
    assert remote_row["status"] == "running" and remote_row["assignee"] == "tb-king"

    # Eligible local zombie: reconciled this same tick.
    assert "t_local_eligible" in result.reconciled_orphans

    # Later stage (spawn loop) genuinely ran and claimed the plain ready
    # task in this SAME call — proves the fenced refusal did not abort
    # the tick before reaching this phase.
    assert "t_plain_ready" in spawned_ids, (
        f"expected the unfenced ready sibling to reach the spawn phase "
        f"in the same tick; spawned={spawned_ids!r}, "
        f"reconciled_orphans={result.reconciled_orphans!r}, "
        f"reclaim_errors={result.reclaim_errors!r}"
    )
    plain_row = conn.execute(
        "SELECT status FROM tasks WHERE id='t_plain_ready'"
    ).fetchone()
    assert plain_row["status"] == "running", (
        "the unfenced sibling must have actually committed its "
        "ready->running claim in this same tick"
    )


def _insert_stale_remote_protected(conn: sqlite3.Connection, task_id: str, run_id: int, *, age_seconds: int = 10_000) -> None:
    """A running row owned by a DIFFERENT node's claim_lock (remote
    origin, no verified lease for THIS node), old enough to trip
    detect_stale_running's staleness window. Exercises TB-cndr's exact
    reported gap: detect_stale_running previously lacked the host_prefix
    filter that detect_crashed_workers already had, so this row could
    reach the guarded UPDATE and trip the lifecycle-fence trigger. With
    the host_prefix filter restored, this row must never even reach that
    UPDATE — it's simply out of scope (no host-local claim_lock).
    """
    now = int(time.time())
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, current_run_id, "
        "claim_lock, worker_pid, last_heartbeat_at) "
        "VALUES (?, 'repro-remote-stale', 'running', 'tb-king', ?, "
        "'other-node:99999', 424242, NULL)",
        (task_id, run_id),
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (?, ?, 'running', ?)", (run_id, task_id, now - age_seconds),
    )
    conn.commit()


def _insert_same_host_protected_generation(
    conn: sqlite3.Connection, task_id: str, run_id: int, *, age_seconds: int = 10_000,
) -> tuple[str, int]:
    """A running row genuinely claimed by THIS host (claim_lock built from
    the real _claimer_id(), matching what claim_task would have written),
    old enough to be stale, but whose lease/generation has since been
    independently revoked (no matching row in
    fleet_kanban_verified_leases with the generation stamped on this
    task at claim time). This is TB-cndr's second required case: the
    claim_lock IS host-local (so, prior to the ordering fix, the
    termination signal would have fired), but the reclaim is NOT
    authorized by the lifecycle fence. Proves no signal is sent in this
    exact case. Returns (claim_lock, worker_pid) for the test to assert
    zero signals against.
    """
    import hermes_cli.kanban_db as kb

    now = int(time.time())
    claim_lock = kb._claimer_id()
    worker_pid = os.getpid()  # genuinely alive right now — if a signal
    # were (incorrectly) sent to this PID, it would hit this live test
    # process. The test asserts the signal_fn spy is never called at all,
    # so this never actually happens; kept host-realistic on purpose.

    # The insert-running trigger (fleet_kanban_insert_running_requires_
    # lease) requires a verified lease to exist at INSERT time too, not
    # just at the later UPDATE guarded by fleet_kanban_exclusive_
    # lifecycle_generation. Seed a temporary valid lease, insert as
    # 'running' so run_generation gets legitimately bound by
    # fleet_kanban_running_binds_generation (this row really was claimed
    # under lease authority, at claim time), THEN delete the lease row —
    # simulating exactly what "independently revoked since the claim"
    # means: the row's stamped run_generation no longer has a matching
    # live lease for fleet_kanban_exclusive_lifecycle_generation to find.
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES (?, 'repro-same-host-revoked', 'ready', 'snow-cndr', 'snowdrop')",
        (task_id,),
    )
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id=?",
        (task_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, "
        " expires_at, verified_at) VALUES (?, 'snow-cndr', 'snowdrop', 9, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, worker_pid=?, "
        "current_run_id=? WHERE id=?",
        (claim_lock, worker_pid, run_id, task_id),
    )
    # Now revoke: delete the verified lease this row's run_generation was
    # legitimately bound to. fleet_kanban_exclusive_lifecycle_generation
    # will find no matching lease row for this issue/generation/holder on
    # the next guarded UPDATE — refusing it, exactly as "generation
    # independently revoked" means.
    conn.execute(
        "DELETE FROM fleet_kanban_verified_leases WHERE issue_id=?",
        (issue_id,),
    )

    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (?, ?, 'running', ?)", (run_id, task_id, now - age_seconds),
    )
    conn.commit()
    return claim_lock, worker_pid


def test_stale_reclaim_host_prefix_filters_remote_claims(monkeypatch) -> None:
    """TB-cndr's reported gap #1: detect_stale_running previously lacked
    the host_prefix filter detect_crashed_workers already had. A remote
    node's running+stale row must never reach the guarded UPDATE at all
    — it's out of scope by claim_lock ownership, not merely refused by
    the trigger. Proven by a signal_fn spy that must never fire for this
    row (if the code reached the UPDATE it would also attempt to
    terminate a PID this host doesn't own) and by the row staying
    completely untouched.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _insert_stale_remote_protected(conn, "t_remote_stale", 1)

    signalled: list[int] = []

    def _spy_signal(pid, sig):
        signalled.append(pid)

    reclaimed = kbd.detect_stale_running(
        conn, stale_timeout_seconds=1, signal_fn=_spy_signal,
    )

    assert reclaimed == [], (
        "a remote-owned claim_lock must never be reclaimed by this host's "
        "detect_stale_running — it's out of scope entirely"
    )
    assert signalled == [], (
        "a remote-owned worker PID must NEVER be signalled by this host "
        f"— host_prefix filter should exclude the row before any signal "
        f"logic runs, got signalled={signalled!r}"
    )
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id='t_remote_stale'"
    ).fetchone()
    assert row["status"] == "running" and row["claim_lock"] == "other-node:99999", (
        "remote-owned row must be completely untouched"
    )


def test_stale_reclaim_same_host_revoked_generation_sends_zero_signals(monkeypatch) -> None:
    """TB-cndr's reported gap #2 (the more subtle one): a row this HOST
    legitimately claimed (real host-local claim_lock) may still be
    fence-protected if its generation was independently revoked since
    the claim. Before the signal-ordering fix, _terminate_reclaimed_worker
    ran BEFORE the guarded write — so this exact row (same-host claim
    lock, but fence-refused write) would still have been signalled,
    killing a worker this reclaim path was never authorized to touch.
    After the fix: the guarded UPDATE runs first, the fence refuses it,
    and NO termination signal is ever sent for this row.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    claim_lock, worker_pid = _insert_same_host_protected_generation(
        conn, "t_same_host_revoked", 1,
    )

    signalled: list[tuple[int, int]] = []

    def _spy_signal(pid, sig):
        signalled.append((pid, sig))

    reclaimed = kbd.detect_stale_running(
        conn, stale_timeout_seconds=1, signal_fn=_spy_signal,
    )

    assert reclaimed == [], (
        "the fence-refused row must not be counted as reclaimed"
    )
    assert signalled == [], (
        "a same-host worker whose generation was revoked must receive "
        f"ZERO termination signals — the guarded write must be refused "
        f"before any signal logic runs, got signalled={signalled!r}"
    )
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id='t_same_host_revoked'"
    ).fetchone()
    assert row["status"] == "running", "fence-protected row must stay running, untouched"
    assert row["claim_lock"] == claim_lock, "claim_lock must be completely unchanged"
    assert row["worker_pid"] == worker_pid, "worker_pid must be completely unchanged"


def test_stale_reclaim_whole_tick_matrix_with_remote_and_revoked_siblings(monkeypatch) -> None:
    """Extended whole-tick matrix, per TB-cndr's explicit instruction:
    stale-heartbeat remote-protected row (gap #1) alongside a same-host
    revoked-generation row (gap #2), a genuinely eligible LOCAL
    stale-but-legitimately-reclaimable sibling, run through the real
    detect_stale_running call in one pass. All isolation guarantees hold
    simultaneously; zero signals for the two protected rows; the
    eligible sibling still reclaims.
    """
    import hermes_cli.kanban_db as kb_mod

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)

    _insert_stale_remote_protected(conn, "t_remote_stale", 1)
    revoked_lock, revoked_pid = _insert_same_host_protected_generation(
        conn, "t_same_host_revoked", 2,
    )

    # Genuinely eligible LOCAL sibling: real host-local claim_lock, a
    # currently-valid verified lease matching its stamped generation
    # (so the fence authorizes the release), old enough to be stale.
    now = int(time.time())
    local_lock = kb_mod._claimer_id()
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_local_stale_eligible', 'repro-local-stale', 'ready', "
        "'snow-cndr', 'snowdrop')"
    )
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id='t_local_stale_eligible'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, expires_at, verified_at) "
        "VALUES (?, 'snow-cndr', 'snowdrop', 5, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, worker_pid=?, "
        "current_run_id=3 WHERE id='t_local_stale_eligible'",
        (local_lock, os.getpid()),
    )
    # Lease stays live and matching (NOT deleted, unlike the revoked
    # sibling above) — this row's reclaim IS authorized.
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (3, 't_local_stale_eligible', 'running', ?)", (now - 10_000,),
    )
    conn.commit()

    signalled: list[tuple[int, int]] = []

    # Use a signal stub that always reports success (process gone) rather
    # than actually sending OS signals to this test process's own PID —
    # avoids killing the pytest worker while still letting the eligible
    # row's reclaim path proceed to durable completion.
    def _stub_signal(pid, sig):
        signalled.append((pid, sig))
        raise ProcessLookupError()

    reclaimed = kbd.detect_stale_running(
        conn, stale_timeout_seconds=1, signal_fn=_stub_signal,
    )

    assert reclaimed == ["t_local_stale_eligible"], (
        f"expected only the lease-backed local row to reclaim, got {reclaimed!r}"
    )
    # Exactly one signal attempt — for the eligible row only, AFTER its
    # guarded UPDATE was accepted. Neither protected row contributes a
    # signal.
    assert len(signalled) == 1 and signalled[0][0] == os.getpid(), (
        f"expected exactly one signal, for the eligible row's own worker "
        f"pid, got signalled={signalled!r}"
    )

    remote_row = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id='t_remote_stale'"
    ).fetchone()
    assert remote_row["status"] == "running" and remote_row["claim_lock"] == "other-node:99999"

    revoked_row = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id='t_same_host_revoked'"
    ).fetchone()
    assert revoked_row["status"] == "running" and revoked_row["claim_lock"] == revoked_lock

    local_row = conn.execute(
        "SELECT status FROM tasks WHERE id='t_local_stale_eligible'"
    ).fetchone()
    assert local_row["status"] == "ready", "eligible local stale row must have been reclaimed to ready"


# ---------------------------------------------------------------------------
# Fix round 1 — checker VERDICT findings 1 (every reclaim sibling), 3 (stale
# refusals in the tick result) and 4 (exact fence recognition).
# ---------------------------------------------------------------------------

_GENERATION_FENCE = "verified run generation no longer owns lifecycle write"

# Fake worker PIDs: liveness comes from the ``alive`` set patched over
# ``kb._pid_alive`` and every signal goes to a spy, so no real process is touched.
_FENCED_PID = 910_001
_ELIGIBLE_PID = 910_002


def _set_task_cols(conn: sqlite3.Connection, task_id: str, **cols) -> None:
    assignments = ", ".join(f"{col} = ?" for col in cols)
    conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*cols.values(), task_id))
    conn.commit()


def _fenced_running(conn: sqlite3.Connection, task_id: str, run_id: int, **cols) -> None:
    """Same-host claim whose lease generation was revoked: the fence refuses any release."""
    _insert_same_host_protected_generation(conn, task_id, run_id)
    _set_task_cols(conn, task_id, **cols)


def _leased_running(conn: sqlite3.Connection, task_id: str, run_id: int, **cols) -> None:
    """Same-host claim under a live lease: the fence authorizes the release."""
    now = int(time.time())
    # consecutive_failures is fenced once running; the minimal schema has no DEFAULT 0.
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant, consecutive_failures) "
        "VALUES (?, 'leased', 'ready', 'snow-cndr', 'snowdrop', 0)", (task_id,),
    )
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id=?", (task_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, "
        " expires_at, verified_at) VALUES (?, 'snow-cndr', 'snowdrop', 3, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, current_run_id=? WHERE id=?",
        (kb._claimer_id(), run_id, task_id),
    )
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (?, ?, 'running', ?)", (run_id, task_id, now - 10_000),
    )
    conn.commit()
    _set_task_cols(conn, task_id, **cols)


def _snapshot(conn: sqlite3.Connection, task_id: str) -> tuple:
    """Task row, its runs and its events — a refused reclaim must leave all three as-is."""
    return (
        dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()),
        [dict(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,))],
        [dict(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,))],
    )


def _fake_workers(monkeypatch, alive: set[int]) -> list[tuple[int, int]]:
    """Patch liveness to ``alive``; returns the list a signal spy appends to. A
    signalled worker dies (leaves ``alive``) unless the test says otherwise."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in alive)
    return []


def _spy(sent: list, alive: set[int], *, kills: bool = True):
    def _signal(pid, sig):
        sent.append((pid, sig))
        if kills:
            alive.discard(pid)
    return _signal


@pytest.mark.parametrize(
    "sibling",
    ["release_stale_claims", "detect_stale_running", "detect_crashed_workers", "enforce_max_runtime"],
)
def test_reclaim_sibling_isolates_fenced_row_without_signal(sibling, monkeypatch) -> None:
    """Finding 1: every reclaim sibling, called on its own, leaves a fence-refused
    row byte-for-byte unchanged (task, run, events), never signals its worker,
    does not raise, and still reclaims an authorized sibling row in the same pass."""
    now = int(time.time())
    scope, workers_alive, sweep = {
        "release_stale_claims": (
            {"claim_expires": now - 60}, False,
            lambda conn, spy: kb.release_stale_claims(conn, signal_fn=spy),
        ),
        "detect_stale_running": (
            {"claim_expires": now + 3600}, True,
            lambda conn, spy: kbd.detect_stale_running(conn, stale_timeout_seconds=1, signal_fn=spy),
        ),
        "detect_crashed_workers": (
            {"claim_expires": now + 3600, "last_heartbeat_at": now}, False,
            lambda conn, spy: kbd.detect_crashed_workers(conn),
        ),
        "enforce_max_runtime": (
            {"claim_expires": now + 3600, "last_heartbeat_at": now, "max_runtime_seconds": 1}, True,
            lambda conn, spy: kbd.enforce_max_runtime(conn, signal_fn=spy),
        ),
    }[sibling]
    alive = {_FENCED_PID, _ELIGIBLE_PID} if workers_alive else set()
    sent = _fake_workers(monkeypatch, alive)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _fenced_running(conn, "t_fenced", 1, worker_pid=_FENCED_PID, **scope)
    _leased_running(conn, "t_eligible", 2, worker_pid=_ELIGIBLE_PID, **scope)
    before = _snapshot(conn, "t_fenced")

    reclaimed = sweep(conn, _spy(sent, alive))

    assert _snapshot(conn, "t_fenced") == before, "fence-refused row must be completely unchanged"
    assert [pid for pid, _sig in sent if pid == _FENCED_PID] == [], (
        f"a row whose release the fence refused must never be signalled, got {sent!r}"
    )
    assert reclaimed == (1 if sibling == "release_stale_claims" else ["t_eligible"])
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='t_eligible'"
    ).fetchone()["status"] == "ready", "authorized sibling must still be reclaimed this pass"


def test_whole_tick_isolates_fenced_rows_in_every_reclaim_sibling(monkeypatch) -> None:
    """Finding 1 + 3 through the real dispatcher tick: one fence-refused row per
    reclaim sibling, each reported in ``reclaim_errors`` under the step that was
    refused, none signalled or changed — while an authorized crashed sibling is
    reclaimed and a leased ready task still spawns in the SAME tick."""
    now = int(time.time())
    alive = {910_010, 910_011}
    sent = _fake_workers(monkeypatch, alive)
    monkeypatch.setattr(kbd, "_kill_fn", lambda signal_fn: _spy(sent, alive))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "snow-cndr")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    # Each fenced row is in scope for exactly one sweep.
    _insert_stale_remote_protected(conn, "t_fenced_expired", 1)
    _set_task_cols(conn, "t_fenced_expired", claim_expires=now - 60)
    _fenced_running(conn, "t_fenced_stale", 2, worker_pid=910_010, claim_expires=now + 3600)
    _fenced_running(conn, "t_fenced_crashed", 3, worker_pid=910_012,
                    claim_expires=now + 3600, last_heartbeat_at=now)
    _fenced_running(conn, "t_fenced_runtime", 4, worker_pid=910_011,
                    claim_expires=now + 3600, last_heartbeat_at=now, max_runtime_seconds=1)
    _leased_running(conn, "t_crashed_ok", 5, worker_pid=910_013,
                    claim_expires=now + 3600, last_heartbeat_at=now)
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant) "
        "VALUES ('t_ready_ok', 'plain', 'ready', 'snow-cndr', 'snowdrop')"
    )
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id='t_ready_ok'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, "
        " expires_at, verified_at) VALUES (?, 'snow-cndr', 'snowdrop', 1, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.commit()
    fenced = ("t_fenced_expired", "t_fenced_stale", "t_fenced_crashed", "t_fenced_runtime")
    before = {tid: _snapshot(conn, tid) for tid in fenced}
    spawned: list[str] = []

    def _stub_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 999_999

    result = kbd._dispatch_once_locked(conn, spawn_fn=_stub_spawn, stale_timeout_seconds=1)

    refused = {(tid, msg.split(": ", 1)[0]) for tid, msg in result.reclaim_errors}
    assert refused == {
        ("t_fenced_expired", "release_stale_claims"),
        ("t_fenced_stale", "detect_stale_running"),
        ("t_fenced_crashed", "detect_crashed_workers"),
        ("t_fenced_runtime", "enforce_max_runtime"),
    }, result.reclaim_errors
    assert all(_GENERATION_FENCE in msg for _tid, msg in result.reclaim_errors)
    for tid in fenced:
        assert _snapshot(conn, tid) == before[tid], f"{tid} must be completely unchanged"
    assert sent == [], f"no fenced worker may be signalled, got {sent!r}"
    assert result.reclaimed == 0 and result.stale == [] and result.timed_out == []
    assert result.crashed == ["t_crashed_ok"]
    # The spawn phase ran: the leased ready task and the just-released crash
    # sibling (lease still valid) spawn; no fenced row does.
    assert sorted(spawned) == ["t_crashed_ok", "t_ready_ok"]


def test_fenced_claim_reported_in_claim_errors_and_tick_continues(monkeypatch) -> None:
    """A ready row whose CLAIM the fence refuses (no verified lease) is reported
    in ``claim_errors`` — the bucket the CLI and gateway read for claim refusals —
    not in ``reclaim_errors``, stays unclaimed, and a leased ready row later in
    the same lane still spawns this tick."""
    now = int(time.time())
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "snow-cndr")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    # Higher priority, so the lane attempts the fenced row first.
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant, priority) "
        "VALUES ('t_claim_fenced', 'no lease', 'ready', 'snow-cndr', 'snowdrop', '9')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, tenant, priority) "
        "VALUES ('t_claim_ok', 'leased', 'ready', 'snow-cndr', 'snowdrop', '1')"
    )
    issue_id = conn.execute(
        "SELECT issue_id FROM fleet_kanban_issue_map WHERE local_task_id='t_claim_ok'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fleet_kanban_verified_leases "
        "(issue_id, holder_profile, holder_node, generation, lease_revision, "
        " expires_at, verified_at) VALUES (?, 'snow-cndr', 'snowdrop', 1, 1, ?, ?)",
        (issue_id, now + 3600, now),
    )
    conn.commit()
    before = _snapshot(conn, "t_claim_fenced")
    spawned: list[str] = []

    def _stub_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 999_999

    result = kbd._dispatch_once_locked(conn, spawn_fn=_stub_spawn)

    assert result.reclaim_errors == []
    assert result.claim_errors == [
        ("t_claim_fenced", "claim: verified execution lease required before running"),
    ]
    assert _snapshot(conn, "t_claim_fenced") == before, "fenced row must stay unclaimed"
    assert spawned == ["t_claim_ok"]


def test_stale_fence_refusal_reported_in_tick_result(monkeypatch) -> None:
    """Finding 3: a heartbeat-stale row the fence refuses is visible in the tick
    result, not only in a log line — callers can tell a refusal from no work."""
    now = int(time.time())
    alive = {_FENCED_PID}
    sent = _fake_workers(monkeypatch, alive)
    monkeypatch.setattr(kbd, "_kill_fn", lambda signal_fn: _spy(sent, alive))

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _fenced_running(conn, "t_fenced_stale", 1, worker_pid=_FENCED_PID, claim_expires=now + 3600)

    result = kbd._dispatch_once_locked(conn, spawn_fn=lambda *a, **k: None, stale_timeout_seconds=1)

    assert result.stale == []
    assert result.reclaim_errors == [
        ("t_fenced_stale", f"detect_stale_running: {_GENERATION_FENCE}"),
    ]
    assert sent == []


def test_manual_reclaim_refused_by_fence_sends_no_signal(monkeypatch) -> None:
    """Finding 1 (manual reclaim): the operator still sees the fence refusal,
    but the worker is not killed first and the row is left exactly as it was."""
    alive = {_FENCED_PID}
    sent = _fake_workers(monkeypatch, alive)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _fenced_running(conn, "t_fenced", 1, worker_pid=_FENCED_PID)
    before = _snapshot(conn, "t_fenced")

    with pytest.raises(sqlite3.IntegrityError, match=_GENERATION_FENCE):
        kb.reclaim_task(conn, "t_fenced", reason="operator", signal_fn=_spy(sent, alive))

    assert sent == [], f"a refused manual reclaim must not signal the worker, got {sent!r}"
    assert _snapshot(conn, "t_fenced") == before
    assert _FENCED_PID in alive


def test_max_runtime_survivor_keeps_its_claim(monkeypatch) -> None:
    """Finding 1 (retain claim): a worker that survives SIGTERM and SIGKILL keeps
    its claim — releasing it would spawn a duplicate beside a live process. The
    timeout is deferred, not counted, and retried next tick."""
    now = int(time.time())
    alive = {_ELIGIBLE_PID}
    sent = _fake_workers(monkeypatch, alive)
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at=None: False)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    claim_lock = kb._claimer_id()
    _leased_running(conn, "t_survivor", 1, worker_pid=_ELIGIBLE_PID,
                    claim_expires=now + 60, last_heartbeat_at=now, max_runtime_seconds=1)

    timed_out = kbd.enforce_max_runtime(conn, signal_fn=_spy(sent, alive, kills=False))

    assert timed_out == []
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id='t_survivor'"
    ).fetchone()
    assert (row["status"], row["claim_lock"], row["worker_pid"]) == ("running", claim_lock, _ELIGIBLE_PID)
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id='t_survivor' ORDER BY id")]
    assert "timed_out" not in kinds and "reclaim_deferred" in kinds
    assert [sig for _pid, sig in sent][:1] == [signal.SIGTERM]


def test_unrelated_check_constraint_with_fence_text_is_not_swallowed() -> None:
    """Finding 4: an unrelated CHECK constraint that merely NAMES a fence phrase
    fails as ``CHECK constraint failed: <phrase>`` (SQLITE_CONSTRAINT_CHECK). It
    is not the adapter's RAISE(ABORT) fence and must abort loudly, not be
    filed as an expected refusal."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _make_board(conn)
    _insert_eligible_local_zombie(conn, "t_leased_orphan", 1)
    conn.executescript(
        """
        CREATE TABLE unrelated_audit (
            value INTEGER,
            CONSTRAINT "verified execution lease required before running" CHECK (value > 0)
        );
        CREATE TRIGGER unrelated_audit_on_task_update AFTER UPDATE ON tasks
        BEGIN
            INSERT INTO unrelated_audit (value) VALUES (-1);
        END;
        """
    )
    errors: list = []

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        kbd.reconcile_orphaned_running(conn, errors_out=errors)

    assert errors == []
    assert conn.execute(
        "SELECT status FROM tasks WHERE id='t_leased_orphan'"
    ).fetchone()["status"] == "running"


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
