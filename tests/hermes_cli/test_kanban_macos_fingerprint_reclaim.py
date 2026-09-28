"""macOS worker fingerprint and the per-tick reclaim refusals (t_b0e7b8ed).

Two defects sat behind "the lifecycle fence refuses reclaims every tick" on the
TurnerBook Mac:

1. The composed spawn fingerprint ``"<epoch>|<start>"`` was compared by exact
   string equality. On macOS the epoch is ``""`` by design (no ``/proc``; the
   psutil start time is already wall-clock, so it cannot survive a reboot), but
   the start reading drifts by about 1 s with ``kern.boottime`` (#117505). Every
   sibling owner-liveness check already tolerates that drift; this one did not,
   so a live worker could read as a recycled PID (dead) and be reclaimed.
2. ``reconcile_orphaned_running`` tried to requeue claimless ``running`` MIRROR
   rows of cards other Fleet nodes own. The Fleet lease fence refuses those by
   design, so every tick logged one refusal per foreign card. Ownership now uses
   the same rule as ``recompute_ready`` and the spawnable probes.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gateway import drain_control
from gateway import status as gw_status
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NODE = "turnerbook"
_FENCE = "verified run generation no longer owns lifecycle write"

_MAP_DDL = """CREATE TABLE IF NOT EXISTS fleet_kanban_issue_map (
    local_task_id TEXT PRIMARY KEY,
    issue_id TEXT,
    raw_title TEXT,
    canonical_body TEXT,
    source_node TEXT,
    current_node TEXT,
    source_profile TEXT
)"""

# Shape of the live adapter trigger: the node id is the 5th VALUES literal.
_TRIGGER_DDL = f"""CREATE TRIGGER fleet_kanban_task_insert
    AFTER INSERT ON tasks
    BEGIN
        INSERT INTO fleet_kanban_issue_map(
            local_task_id,issue_id,raw_title,canonical_body,source_node,current_node,source_profile
        ) VALUES(NEW.id,'fk_' || lower(hex(randomblob(4))),NEW.title,NEW.body,'{NODE}',NULL,'p');
    END"""

# Fence shaped like the live one: refuses any lifecycle write on a running row.
_LIFECYCLE_FENCE_DDL = f"""CREATE TRIGGER _lifecycle_fence
    BEFORE UPDATE OF status ON tasks
    WHEN OLD.status = 'running' AND OLD.id IN (SELECT id FROM _fenced_ids)
    BEGIN SELECT RAISE(ABORT, '{_FENCE}'); END"""


@pytest.fixture
def macos_epoch(monkeypatch):
    """The Darwin instantiation epoch: empty (no /proc)."""
    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "")


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conn = kbc.connect(home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _drift(monkeypatch, delta: int) -> int:
    """Make every later start-time read of our own pid ``delta`` centiseconds off."""
    real = gw_status.get_process_start_time(os.getpid())
    assert real is not None
    monkeypatch.setattr(
        gw_status, "get_process_start_time",
        lambda pid: real + delta if pid == os.getpid() else None,
    )
    return real


# --- 1. fingerprint comparison ------------------------------------------------


def test_macos_fingerprint_shape_reads_live(macos_epoch):
    """Pin: the empty macOS epoch is not itself a defect; a live worker reads live."""
    fp = kbd._process_fingerprint(os.getpid())
    assert fp is not None and fp.startswith("|") and int(fp[1:]) > 0
    assert kbd._worker_alive(os.getpid(), fp) is True
    assert kbd._pid_recycled(os.getpid(), fp) is False


@pytest.mark.parametrize("delta", [100, -100, 200, -200])
def test_drifted_start_time_is_still_our_live_worker(macos_epoch, monkeypatch, delta):
    """~1-2 s kern.boottime drift (#117505) must not make a live worker read as recycled."""
    recorded = f"|{gw_status.get_process_start_time(os.getpid())}"
    _drift(monkeypatch, delta)
    assert kbd._pid_recycled(os.getpid(), recorded) is False
    assert kbd._worker_alive(os.getpid(), recorded) is True


@pytest.mark.parametrize("delta", [201, -201, 10_000])
def test_start_time_beyond_tolerance_is_recycled(macos_epoch, monkeypatch, delta):
    recorded = f"|{gw_status.get_process_start_time(os.getpid())}"
    _drift(monkeypatch, delta)
    assert kbd._pid_recycled(os.getpid(), recorded) is True
    assert kbd._worker_alive(os.getpid(), recorded) is False


def test_other_boot_epoch_is_still_recycled_even_with_matching_start(monkeypatch):
    """The boot witness keeps its exact-match authority (Linux reboot case)."""
    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "bootB:7")
    start = gw_status.get_process_start_time(os.getpid())
    assert kbd._pid_recycled(os.getpid(), f"bootA:7|{start}") is True
    assert kbd._pid_recycled(os.getpid(), f"bootB:7|{start}") is False


@pytest.mark.parametrize("recorded", ["|", "|abc", "|0", "|-5"])
def test_unreadable_recorded_start_fails_closed(macos_epoch, recorded):
    """A garbled record never proves identity: it stays foreign (never signalled)."""
    assert kbd._pid_recycled(os.getpid(), recorded) is True


def test_unreadable_current_start_fails_closed(macos_epoch, monkeypatch):
    recorded = f"|{gw_status.get_process_start_time(os.getpid())}"
    monkeypatch.setattr(gw_status, "get_process_start_time", lambda pid: None)
    assert kbd._pid_recycled(os.getpid(), recorded) is True


def test_drifted_live_worker_claim_is_extended_not_released(board, macos_epoch, monkeypatch):
    """End to end: an expired claim beside a live, drifted worker is extended."""
    conn = board
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    recorded = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert recorded.startswith("|")
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (old, tid))
    _drift(monkeypatch, 100)
    killed: list = []
    assert kb.release_stale_claims(conn, signal_fn=lambda p, s: killed.append((p, s))) == 0
    assert killed == []
    assert kb.get_task(conn, tid).status == "running"
    assert "claim_extended" in [e.kind for e in kb.list_events(conn, tid)]


# --- 2. orphan reconcile ownership ---------------------------------------------


def _install_adapter(conn):
    conn.execute(_MAP_DDL)
    conn.execute(_TRIGGER_DDL)
    conn.execute("CREATE TABLE IF NOT EXISTS _fenced_ids (id TEXT PRIMARY KEY)")
    conn.execute(_LIFECYCLE_FENCE_DDL)
    conn.commit()


def _claimless_running(conn, *, owner_source, owner_current, fenced=True) -> str:
    tid = kb.create_task(conn, title="mirror", assignee="shld-king")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL WHERE id = ?", (tid,),
        )
        conn.execute(
            "UPDATE fleet_kanban_issue_map SET source_node = ?, current_node = ? "
            "WHERE local_task_id = ?", (owner_source, owner_current, tid),
        )
        if fenced:
            conn.execute("INSERT INTO _fenced_ids VALUES (?)", (tid,))
    return tid


def _event_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]


@pytest.mark.parametrize("source,current", [(NODE, "sheldon"), ("max", None), ("sheldon", "sheldon")])
def test_foreign_mirror_is_not_reconciled_and_logs_no_refusal(board, source, current):
    """The live shape: claimless running rows homed on Sheldon/Max, fenced here."""
    conn = board
    _install_adapter(conn)
    tid = _claimless_running(conn, owner_source=source, owner_current=current)
    before = _event_count(conn)
    errors: list = []
    assert kbd.reconcile_orphaned_running(conn, errors_out=errors) == []
    assert errors == []
    assert kb.get_task(conn, tid).status == "running"
    assert _event_count(conn) == before


def test_local_claimless_ghost_is_still_attempted_and_refusal_stays_visible(board):
    """A card THIS node owns keeps the old path: the fence refusal is still reported."""
    conn = board
    _install_adapter(conn)
    tid = _claimless_running(conn, owner_source=NODE, owner_current=None)
    errors: list = []
    assert kbd.reconcile_orphaned_running(conn, errors_out=errors) == []
    assert errors == [(tid, f"reconcile_orphaned_running: {_FENCE}")]


def test_local_unfenced_orphan_is_still_requeued(board):
    conn = board
    _install_adapter(conn)
    tid = _claimless_running(conn, owner_source="max", owner_current=NODE, fenced=False)
    assert kbd.reconcile_orphaned_running(conn) == [tid]
    assert kb.get_task(conn, tid).status == "ready"


def test_board_without_fleet_adapter_keeps_legacy_reconcile(board):
    conn = board
    tid = kb.create_task(conn, title="zombie", assignee="w")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = NULL, claim_expires = NULL "
            "WHERE id = ?", (tid,),
        )
    assert kbd.reconcile_orphaned_running(conn) == [tid]
