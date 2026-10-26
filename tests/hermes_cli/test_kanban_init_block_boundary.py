"""A sync-style SQLite writer must not release a new hold during initialization."""

import contextlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def old_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "board.db"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(kb.SCHEMA_SQL)
        for name in ("insert", "update", "delete"):
            conn.execute(f"DROP TRIGGER task_block_entry_{name}")
        conn.execute("DROP TABLE task_block_entries")
        conn.execute(
            "INSERT INTO tasks(id,title,status,created_by,created_at,block_kind) "
            "VALUES('held','Imported hold','blocked','sync',1,'needs_input')"
        )
        conn.commit()
    yield path
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))


@pytest.mark.parametrize("pause_at", [
    "CREATE TRIGGER IF NOT EXISTS task_block_entry_insert",
    "INSERT OR IGNORE INTO task_block_entries",
    "PRAGMA table_info(tasks)",
])
def test_initialization_preserves_concurrent_imported_hold(old_board, monkeypatch, pause_at):
    attempted = False
    locked = False
    errors = []

    def import_hold():
        with contextlib.closing(sqlite3.connect(old_board, timeout=0)) as writer:
            # Separate commits model a release followed by a new imported hold.
            with writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO task_events(task_id,kind,created_at) "
                    "VALUES('held','unblocked',2)"
                )
                writer.execute("UPDATE tasks SET status='ready' WHERE id='held'")
            with writer:
                writer.execute(
                    "UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id='held'"
                )

    original_connect = kbc._sqlite_connect

    def connect_with_writer(path):
        conn = original_connect(path)

        def trace(sql):
            nonlocal attempted, locked
            if attempted or pause_at not in sql:
                return
            attempted = True
            try:
                import_hold()
            except sqlite3.OperationalError as exc:
                if "database is locked" in str(exc):
                    locked = True
                else:
                    errors.append(exc)
            except Exception as exc:
                # SQLite suppresses callback exceptions. Assert them in the caller.
                errors.append(exc)

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(kbc, "_sqlite_connect", connect_with_writer)
    with kbc.connect_closing(old_board) as conn:
        assert attempted and not errors
        if locked:
            import_hold()
        assert kb.get_task(conn, "held").status == "blocked"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, "held").status == "blocked"
        # Schema writers wait; after schema commit an importer can finish while
        # connect() is still running migrations, protected by the new triggers.
        assert locked == (pause_at != "PRAGMA table_info(tasks)")
        # Retrying after initialization is not a permanent hold: a newer release
        # still unlocks the card through the real recovery path.
        conn.execute(
            "INSERT INTO task_events(task_id,kind,created_at) VALUES('held','unblocked',3)"
        )
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, "held").status == "ready"


def test_failed_initialization_rolls_back_boundary_and_can_retry(old_board, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(kb, "SCHEMA_SQL", kb.SCHEMA_SQL + "\nSELECT missing_init_column;\n")
        with pytest.raises(sqlite3.OperationalError, match="missing_init_column"):
            kbc.connect(old_board)
    assert str(old_board.resolve()) not in kb._INITIALIZED_PATHS
    with contextlib.closing(sqlite3.connect(old_board)) as observer:
        assert observer.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'task_block_entr%'"
        ).fetchall() == []
        assert observer.execute("SELECT status FROM tasks WHERE id='held'").fetchone()[0] == "blocked"
    with kbc.connect_closing(old_board) as conn:
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, "held").status == "blocked"
