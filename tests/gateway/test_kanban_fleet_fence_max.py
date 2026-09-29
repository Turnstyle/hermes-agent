"""Max regression: a protected foreign mirror cannot stop the whole tick.

A local SQLite trigger models the Fleet adapter's verified-generation refusal.
This test runs against a temporary Kanban board, never live fleet data.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def test_fenced_mirror_keeps_running_while_local_orphan_requeues(board):
    """One refused row is reported; the eligible sibling still commits."""
    conn = board
    foreign = kb.create_task(conn, title="foreign mirror", assignee="tb-king")
    local = kb.create_task(conn, title="local orphan", assignee="max-cndr")
    for tid in (foreign, local):
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,)
        )
    conn.commit()
    conn.execute(
        "CREATE TRIGGER fleet_kanban_exclusive_lifecycle_generation "
        "BEFORE UPDATE ON tasks "
        f"WHEN OLD.id='{foreign}' AND OLD.status='running' "
        "AND NEW.status!='running' "
        "BEGIN SELECT RAISE(ABORT, "
        "'verified run generation no longer owns lifecycle write'); END"
    )
    reconciled = kbd.reconcile_orphaned_running(conn)
    assert reconciled == [local]
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (foreign,)).fetchone()[0] == "running"
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (local,)).fetchone()[0] == "ready"
