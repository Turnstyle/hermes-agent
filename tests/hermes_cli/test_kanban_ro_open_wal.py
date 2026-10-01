"""Tests for Kanban WAL companion files keepalive and bounded read-only open retry.

Card t_2ed89c39: Fix intermittent read-only Kanban DB open failures
('unable to open database file', SQLite error 14).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.sqlite_safe_read import (
    LiveConnectionError,
    has_live_connection,
    offline_file_access,
    read_header_bytes_preopen,
)


def _setup_wal_board(tmp_path: Path) -> Path:
    """Create a temporary initialized Kanban board in WAL mode with one task."""
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_init', 'initial task', 'todo', 1000)"
        )
    return db_path


def _close_and_verify_side_files_vanished(db_path: Path) -> tuple[Path, Path]:
    """Ensure all connections are closed and -wal and -shm have vanished."""
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    assert not wal.exists(), f"Expected {wal} to be absent"
    assert not shm.exists(), f"Expected {shm} to be absent"
    return wal, shm


def test_r1_wal_keepalive_reproduction_persists_side_files(tmp_path: Path) -> None:
    """(R1) REPRODUCTION: Start keepalive the way the gateway does.

    Assert that mode=ro open + SELECT succeeds and side files persist across writers.
    FAILS on base code because ensure_wal_keepalive API does not exist.
    """
    db_path = _setup_wal_board(tmp_path)
    wal, shm = _close_and_verify_side_files_vanished(db_path)

    try:
        # Start keepalive the way the gateway does:
        # On base commit c42e07ff6de78ac7500075c85ced7cd11340b843, this raises AttributeError.
        kbc.ensure_wal_keepalive(db_path)

        # Open a writer, perform work, and close writer
        with kbc.connect_closing(db_path) as writer:
            writer.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_w1', 'writer 1 task', 'in_progress', 1001)"
            )

        # Writer is closed now. Because keepalive is open, -wal and -shm must persist!
        assert wal.exists(), "WAL file should persist while keepalive is open"
        assert shm.exists(), "SHM file should persist while keepalive is open"

        # Mode=ro open and SELECT must succeed without error 14
        with kbc.connect_readonly_closing(db_path) as ro:
            row = ro.execute("SELECT count(*) FROM tasks").fetchone()
            assert row[0] >= 2

        # Side files still persist
        assert wal.exists()
        assert shm.exists()
    finally:
        if hasattr(kbc, "close_all_wal_keepalives"):
            kbc.close_all_wal_keepalives()


def test_r2_retry_recovers_when_writer_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(R2) RETRY, RED on base by behavior:

    With side files absent and a background thread that opens a read-write connection
    after about 1 s, connect_readonly_closing succeeds on our code and raises on base.
    """
    db_path = _setup_wal_board(tmp_path)
    wal, shm = _close_and_verify_side_files_vanished(db_path)

    orig_connect = sqlite3.connect

    # Simulate the SQLite CANTOPEN condition (error 14) that happens on WAL DBs
    # when side files are absent.
    def mock_connect(database, *args, **kwargs):
        if isinstance(database, str) and "mode=ro" in database:
            if not (wal.exists() and shm.exists()):
                raise sqlite3.OperationalError("unable to open database file")
        return orig_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", mock_connect)
    monkeypatch.setattr("hermes_cli.kanban_db_connect.sqlite3.connect", mock_connect)

    writer_started = threading.Event()
    stop_writer = threading.Event()

    def delayed_writer():
        time.sleep(1.0)
        # Open read-write connection to recreate side files
        w_conn = orig_connect(str(db_path), isolation_level=None)
        w_conn.execute("PRAGMA journal_mode=WAL")
        w_conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_bg', 'bg task', 'todo', 1002)")
        writer_started.set()
        # Keep connection open briefly so side files remain alive for reader
        stop_writer.wait(timeout=5.0)
        w_conn.close()

    bg_thread = threading.Thread(target=delayed_writer, daemon=True)
    bg_thread.start()

    t0 = time.monotonic()
    try:
        # On base code, this immediately calls sqlite3.connect without retry and raises OperationalError.
        # On our code, this retries with backoff until the background writer creates side files, then succeeds.
        with kbc.connect_readonly_closing(db_path) as ro:
            count = ro.execute("SELECT count(*) FROM tasks").fetchone()[0]
            assert count >= 2
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.8, f"Expected retry to wait for writer (~1s), took {elapsed:.2f}s"
    finally:
        stop_writer.set()
        bg_thread.join(timeout=5.0)


def test_r3_retry_timeout_and_non_cantopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(R3) The retry gives up within about 8 s and re-raises when no writer appears;

    non-CANTOPEN errors are NOT retried.
    """
    db_path = _setup_wal_board(tmp_path)
    wal, shm = _close_and_verify_side_files_vanished(db_path)

    # 1. Test timeout after ~8s when no writer appears
    def mock_cantopen(database, *args, **kwargs):
        if isinstance(database, str) and "mode=ro" in database:
            raise sqlite3.OperationalError("unable to open database file")
        return sqlite3.connect(database, *args, **kwargs)

    monkeypatch.setattr("hermes_cli.kanban_db_connect.sqlite3.connect", mock_cantopen)

    t0 = time.monotonic()
    with pytest.raises(sqlite3.OperationalError) as exc_info:
        with kbc.connect_readonly_closing(db_path):
            pass
    elapsed = time.monotonic() - t0
    assert "unable to open database file" in str(exc_info.value).lower()
    assert 7.0 <= elapsed <= 10.0, f"Expected ~8s timeout, took {elapsed:.2f}s"

    # 2. Test that non-CANTOPEN errors are NOT retried (raise immediately)
    def mock_corrupt(database, *args, **kwargs):
        if isinstance(database, str) and "mode=ro" in database:
            raise sqlite3.DatabaseError("file is not a database")
        return sqlite3.connect(database, *args, **kwargs)

    monkeypatch.setattr("hermes_cli.kanban_db_connect.sqlite3.connect", mock_corrupt)

    t1 = time.monotonic()
    with pytest.raises(sqlite3.DatabaseError) as exc_info2:
        with kbc.connect_readonly_closing(db_path):
            pass
    elapsed2 = time.monotonic() - t1
    assert "file is not a database" in str(exc_info2.value)
    assert elapsed2 < 1.0, f"Non-CANTOPEN error should not be retried, took {elapsed2:.2f}s"


def test_r4_checkpoints_progress_with_keepalive_open(tmp_path: Path) -> None:
    """(R4) Checkpoints still progress with keepalive open; no open transaction."""
    db_path = _setup_wal_board(tmp_path)

    try:
        conn = kbc.ensure_wal_keepalive(db_path)
        assert conn is not None

        # Verify keepalive connection itself has no open transaction
        assert not conn.in_transaction, "Keepalive must not hold an open transaction"

        # Write data to generate WAL frames
        with kbc.connect_closing(db_path) as writer:
            writer.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_chk', 'chk task', 'todo', 1003)"
            )

        # Checkpoint PASSIVE should make progress
        with kbc.connect_closing(db_path) as writer:
            row = writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            # row: (busy, log_frames, checkpointed_frames)
            assert row[0] == 0, f"wal_checkpoint(PASSIVE) was blocked: {row}"

        # Checkpoint TRUNCATE should make progress and truncate WAL
        wal = db_path.with_name(db_path.name + "-wal")
        with kbc.connect_closing(db_path) as writer:
            row = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert row[0] == 0, f"wal_checkpoint(TRUNCATE) was blocked: {row}"

        assert wal.exists()
        assert wal.stat().st_size == 0, "wal_checkpoint(TRUNCATE) should truncate WAL to 0 bytes"
        assert not conn.in_transaction, "Keepalive must still have no open transaction"
    finally:
        kbc.close_all_wal_keepalives()


def test_r5_repair_backup_path_closes_keepalive_and_reopens_new_inode(tmp_path: Path) -> None:
    """(R5) The repair/backup path closes keepalive first and reopens on the new file (inode check)."""
    db_path = _setup_wal_board(tmp_path)

    try:
        kbc.ensure_wal_keepalive(db_path)
        assert kbc.is_wal_keepalive_active(db_path)
        ino1 = db_path.stat().st_ino

        # 1. Test repair_db closes keepalive first
        res = kbc.repair_db(db_path)
        assert res.status == "ok"
        # Keepalive must have been closed during repair_db
        assert not kbc.is_wal_keepalive_active(db_path)
        assert not has_live_connection(db_path)

        # 2. Re-establish keepalive
        kbc.ensure_wal_keepalive(db_path)
        assert kbc.is_wal_keepalive_active(db_path)

        # 3. Simulate corrupt board backup: _backup_corrupt_db must close keepalive first
        backup_path = kbc._backup_corrupt_db(db_path)
        assert backup_path is not None, "_backup_corrupt_db should succeed when keepalive closes"
        assert not kbc.is_wal_keepalive_active(db_path)
        assert not has_live_connection(db_path)

        # 4. Simulate file replacement (e.g. board archive/restore or recreating board)
        # Create a new file with different content and inode
        new_file = tmp_path / "replacement.db"
        kbc.init_db(new_file)
        with kbc.connect_closing(new_file) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_new', 'replacement', 'todo', 1004)"
            )
        new_file.replace(db_path)
        ino2 = db_path.stat().st_ino
        assert ino2 != ino1, "Replacement file must have a different inode"

        # ensure_wal_keepalive must reopen on the NEW file
        new_conn = kbc.ensure_wal_keepalive(db_path)
        assert new_conn is not None
        assert kbc.is_wal_keepalive_active(db_path)
        # Querying through keepalive or readonly must see replacement data
        with kbc.connect_readonly_closing(db_path) as ro:
            tasks = [r["id"] for r in ro.execute("SELECT id FROM tasks").fetchall()]
            assert "t_new" in tasks
    finally:
        kbc.close_all_wal_keepalives()


def test_r6_sqlite_safe_read_interaction(tmp_path: Path) -> None:
    """(R6) Verify sqlite_safe_read interaction:

    Keepalive is tracked so byte-level probes and offline file access are refused
    while open to protect POSIX locks; closing keepalive releases tracking cleanly.
    """
    db_path = _setup_wal_board(tmp_path)

    try:
        kbc.ensure_wal_keepalive(db_path)
        assert kbc.is_wal_keepalive_active(db_path)

        # 1. Connection must be tracked in sqlite_safe_read
        assert has_live_connection(db_path)

        # 2. read_header_bytes_preopen must be refused while connection is live
        header = read_header_bytes_preopen(db_path, length=16)
        assert header is None, "read_header_bytes_preopen should refuse while keepalive is live"

        # 3. offline_file_access must raise LiveConnectionError
        with pytest.raises(LiveConnectionError):
            with offline_file_access(db_path):
                pass

        # 4. _validate_sqlite_header should NOT raise (it passes because head is None)
        kbc._validate_sqlite_header(db_path)

        # 5. Close keepalive
        closed = kbc.close_wal_keepalive(db_path)
        assert closed is True
        assert not has_live_connection(db_path)

        # 6. Now read_header_bytes_preopen must succeed
        header = read_header_bytes_preopen(db_path, length=16)
        assert header == b"SQLite format 3\x00"

        # 7. offline_file_access now succeeds
        with offline_file_access(db_path):
            pass
    finally:
        kbc.close_all_wal_keepalives()


def test_r7_opt_out_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(R7) The opt-out env var disables the keepalive."""
    db_path = _setup_wal_board(tmp_path)

    for disable_val in ("0", "false", "no", "off"):
        monkeypatch.setenv("HERMES_KANBAN_WAL_KEEPALIVE", disable_val)
        conn = kbc.ensure_wal_keepalive(db_path)
        assert conn is None, f"Expected keepalive to be disabled for env={disable_val}"
        assert not kbc.is_wal_keepalive_active(db_path)
        assert not has_live_connection(db_path)

    # Re-enabled when unset or 1
    monkeypatch.delenv("HERMES_KANBAN_WAL_KEEPALIVE", raising=False)
    conn = kbc.ensure_wal_keepalive(db_path)
    try:
        assert conn is not None
        assert kbc.is_wal_keepalive_active(db_path)
        assert has_live_connection(db_path)
    finally:
        kbc.close_all_wal_keepalives()


def test_keepalive_does_not_create_missing_board(tmp_path: Path) -> None:
    """1(e): Keepalive does not create a missing board and does not run schema/migration writes."""
    missing_path = tmp_path / "never_created.db"
    conn = kbc.ensure_wal_keepalive(missing_path)
    assert conn is None
    assert not missing_path.exists(), "Missing board file must not be created by ensure_wal_keepalive"
    assert not kbc.is_wal_keepalive_active(missing_path)


def test_keepalive_logs_once_per_board(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """1(d): Logs once per board when it starts, at debug or info level."""
    import logging
    db_path = _setup_wal_board(tmp_path)
    kbc._KEEPALIVE_LOGGED.discard(str(db_path.resolve()))

    try:
        with caplog.at_level(logging.INFO):
            conn1 = kbc.ensure_wal_keepalive(db_path)
            assert conn1 is not None
            # Call again - should be idempotent and not log second time
            conn2 = kbc.ensure_wal_keepalive(db_path)
            assert conn2 is conn1

        matching_records = [
            r for r in caplog.records
            if "kanban WAL keepalive started for board" in r.message
        ]
        assert len(matching_records) == 1, f"Expected exactly 1 log record, got {len(matching_records)}"
    finally:
        kbc.close_all_wal_keepalives()


def test_initialized_paths_discard_closes_keepalive(tmp_path: Path) -> None:
    """1(c): Discarding a board from _INITIALIZED_PATHS (e.g. board removal / rename / reinit) closes keepalive."""
    db_path = _setup_wal_board(tmp_path)
    try:
        kbc.ensure_wal_keepalive(db_path)
        assert kbc.is_wal_keepalive_active(db_path)
        assert has_live_connection(db_path)

        # Simulating board removal / cache invalidation:
        kbc._INITIALIZED_PATHS.discard(str(db_path.resolve()))

        # Keepalive must have been automatically closed
        assert not kbc.is_wal_keepalive_active(db_path)
        assert not has_live_connection(db_path)
    finally:
        kbc.close_all_wal_keepalives()


def test_gateway_dispatcher_tick_ensures_keepalive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that gateway's _KanbanDispatcher.tick_once_for_board ensures WAL keepalive."""
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher

    db_path = _setup_wal_board(tmp_path)
    wal, shm = _close_and_verify_side_files_vanished(db_path)

    # Configure dummy settings
    settings = _DispatcherSettings(
        interval=60.0,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=5,
        stale_timeout_seconds=0,
        reconcile_orphans=False,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )

    class DummyKb:
        DEFAULT_BOARD = "test_board"
        def kanban_db_path(self, slug="test_board"):
            return db_path

    dispatcher = _KanbanDispatcher(DummyKb(), settings)
    monkeypatch.setattr("gateway.kanban_watchers_dispatcher._kbd", lambda: type("DummyKbd", (), {"dispatch_once": lambda conn, **kw: None}))

    try:
        assert not kbc.is_wal_keepalive_active(db_path)
        # Run one tick for the board
        dispatcher.tick_once_for_board("test_board")
        # Keepalive must now be active!
        assert kbc.is_wal_keepalive_active(db_path)
        assert wal.exists()
        assert shm.exists()
    finally:
        kbc.close_all_wal_keepalives()

