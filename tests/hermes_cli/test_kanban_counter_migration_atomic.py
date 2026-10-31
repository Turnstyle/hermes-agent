"""Saved-data regression: a failed legacy rename must be safe to retry."""
import sqlite3

import pytest
from hermes_cli import kanban_db_connect as db


def test_legacy_counter_failure_rolls_back_then_retry_preserves_new_writes(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                status TEXT NOT NULL, created_at INTEGER NOT NULL,
                spawn_failures INTEGER, last_spawn_error TEXT);
            INSERT INTO tasks VALUES ('legacy', 'saved task', 'ready', 1, 7, 'old error');
        """)
    original = db._add_column_if_missing

    def fail_after_add(conn, table, column, ddl):
        added = original(conn, table, column, ddl)
        if column == "consecutive_failures":
            raise RuntimeError("injected after counter ALTER before copy")
        return added

    with monkeypatch.context() as patch:
        patch.setattr(db, "_add_column_if_missing", fail_after_add)
        with pytest.raises(RuntimeError, match="injected after counter ALTER"):
            db.init_db(path)
    assert str(path.resolve()) not in db._INITIALIZED_PATHS
    with sqlite3.connect(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "consecutive_failures" not in columns, "failed migration persisted its ALTER"
        assert conn.execute("SELECT spawn_failures, last_spawn_error FROM tasks").fetchone() == (7, "old error")
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT consecutive_failures, last_failure_error FROM tasks").fetchone() == (7, "old error")
        conn.execute("UPDATE tasks SET consecutive_failures=8, last_failure_error='new error'")
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT consecutive_failures, last_failure_error FROM tasks").fetchone() == (8, "new error")
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
