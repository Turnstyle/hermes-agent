"""Narrow reproduction: fleet_kanban_exclusive_lifecycle_generation trigger
unconditionally blocks reconcile_orphaned_running (and any other local
status-out-of-'running' write) once installed, because it requires a
matching row in fleet_kanban_verified_leases that nothing on this node
ever populates.

Root cause, exact:
  - Trigger source: /home/snowdrop/.hermes/fleet-kanban-deploy-5f8e0275/
    scripts/fleet_kanban_sqlite.py:324-349
    (fleet_kanban_sqlite.install_adapter_schema, merged PR #459,
    "Fleet Kanban Firestore adapter, source-only; flag off",
    hermes-agent commit 5f8e02754b77668b52d0c4bae79f8c0b9634b862)
  - Failing local helper: reconcile_orphaned_running() in
    hermes_cli/kanban_db.py:8691 (called every gateway dispatcher tick
    from gateway/kanban_watchers.py:1566 -> kanban_db.dispatch_once ->
    _dispatch_once_locked:9961)
  - Live DB: /home/snowdrop/.hermes/kanban/boards/fleet/kanban.db
  - Live affected task rows (confirmed via read-only query 2026-09-24):
      t_fk_c3e727d8 (issue fk_e9b05874-7890-6bd7-357d-eb009bcdc7a3)
      t_fk_a47163e9 (issue fk_ddca393c-f4c1-4f49-bcb4-6865236cc8d3)
    both status='running', claim_lock/claim_expires/worker_pid all NULL
    (classic "broken claim bookkeeping" case reconcile_orphaned_running
    exists to fix), assignee='tb-king'.
  - fleet_kanban_verified_leases has 0 rows fleet-wide on this board.
    fleet_kanban_issue_map has 0 rows with run_generation IS NOT NULL.
  - The trigger's WHEN clause fires whenever the local write is
    "origin='local'" AND OLD.status='running' AND any of
    (status,result,completed_at,assignee,tenant,consecutive_failures,
    last_failure_error) changes. Its body ABORTs unless it finds a
    fleet_kanban_issue_map row for the task joined to a
    fleet_kanban_verified_leases row with matching generation, holder,
    and expires_at > now. Since no lease rows exist at all, that EXISTS
    check can never be satisfied — every local transition out of
    'running' (reconciliation, normal completion, failure, reclaim)
    fails with sqlite3.IntegrityError, not just the two zombie tasks.

  Gateway impact observed live: gateway.log for profile snow-cndr shows
  2,907+ occurrences of this exact IntegrityError since first hit
  2026-09-20 15:36:16 UTC, with "kanban dispatcher stuck: ready queue
  non-empty for 5246+ consecutive ticks but 0 workers spawned" — the
  dispatcher's per-tick reconcile_orphaned_running() call throws before
  dispatch logic for the ready queue ever runs, so the tick fails and
  nothing gets dispatched.

This file reproduces the exact ABORT in isolation, against a throwaway
in-memory DB carrying only the columns the trigger and helper touch —
no live data, no fix applied. Run: PYTHONPATH=<repo> python3 <this file>
"""
from __future__ import annotations

import sqlite3
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPTS = Path(
    os.environ.get(
        "FLEET_KANBAN_DEPLOY_SCRIPTS",
        str(Path.home() / ".hermes/fleet-kanban-deploy-5f8e0275/scripts"),
    )
)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DEPLOY_SCRIPTS))

import hermes_cli.kanban_db as kb  # noqa: E402  (schema/board helpers)
# 0.21.5: reconcile_orphaned_running moved to hermes_cli.kanban_db_dispatch.
import hermes_cli.kanban_db_dispatch as kbd  # noqa: E402
import fleet_kanban_sqlite as adapter  # noqa: E402


def _minimal_board_schema(conn: sqlite3.Connection) -> None:
    """Create just enough of the real Hermes board schema for the
    adapter's `_require_board_schema` guard and the trigger body to run.
    Column set matches adapter.TASK_COLUMNS + the extra columns
    reconcile_orphaned_running reads/writes.
    """
    cols = ", ".join(
        f"{c} TEXT" for c in adapter.TASK_COLUMNS if c != "id"
    )
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, {cols},
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            worker_started_at TEXT,
            current_run_id INTEGER, last_heartbeat_at INTEGER
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY, task_id TEXT, author TEXT,
            body TEXT, created_at INTEGER
        );
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT,
            status TEXT, outcome TEXT, summary TEXT, error TEXT,
            metadata TEXT, started_at INTEGER, ended_at INTEGER,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
            payload TEXT, run_id INTEGER, created_at INTEGER
        );
        """
    )


def reproduce() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _minimal_board_schema(conn)
    adapter.install_adapter_schema(
        conn, board_slug="fleet", node_id="snowdrop",
        source_profile="snow-cndr",
    )
    # Exactly the live shape: a zombie 'running' task, no claim bookkeeping,
    # no worker_pid, and (matching production) zero verified-lease rows.
    now = int(time.time())
    # The live zombie rows were written with origin='remote' (Firestore
    # sync path), which the insert-time trigger allows without a lease.
    # Flip apply_context to 'remote' to match, then flip back to 'local'
    # before calling reconcile_orphaned_running (a purely local operation,
    # exactly how the real gateway dispatcher invokes it).
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='remote' WHERE singleton=1")
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, current_run_id) "
        "VALUES ('t_repro_zombie', 'repro', 'running', 'tb-king', 1)"
    )
    conn.execute("UPDATE fleet_kanban_apply_context SET origin='local' WHERE singleton=1")
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) "
        "VALUES (1, 't_repro_zombie', 'running', ?)", (now - 999,),
    )
    conn.commit()

    try:
        reconciled = kbd.reconcile_orphaned_running(conn)
        print(
            "Candidate fix applied: reconcile_orphaned_running no longer "
            "raises — the fence is caught per-row and the call returns "
            f"normally. reconciled={reconciled!r} (empty is correct: no "
            "verified lease exists for this row, so it stays protected "
            "and unreclaimed, exactly as intended)."
        )
        assert reconciled == [], (
            "the protected zombie row must NOT be reconciled without a "
            "verified lease"
        )
        print("PASS (post-fix behavior): fence enforced, no crash.")
    except sqlite3.IntegrityError as exc:
        print("REPRODUCED exact live failure (baseline, unfixed):", exc)
        assert "verified run generation no longer owns lifecycle write" in str(exc)
        print(
            "Confirmed: fleet_kanban_exclusive_lifecycle_generation trigger "
            "blocks reconcile_orphaned_running() because "
            "fleet_kanban_verified_leases has no row for this task and "
            "none can ever be inserted by this code path."
        )


if __name__ == "__main__":
    reproduce()
