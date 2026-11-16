"""Retention bounds for ``kanban gc``: a negative window builds a future cutoff
that matches every row; zero disables the sweep rather than deleting all."""
import argparse
import json
import os
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path

def _done_task_with_old_event(conn):
    tid = kb.create_task(conn, title="finished")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=0 WHERE task_id=?", (tid,))
    return tid

def _event_rows(conn, tid):
    return conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id=?", (tid,)
    ).fetchone()[0]

def _old_log_file() -> Path:
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "worker-1.log"
    p.write_text("log line")
    os.utime(p, (0, 0))
    return p

def _args(event_days=30, log_days=30):
    return argparse.Namespace(event_retention_days=event_days,
                              log_retention_days=log_days)

def test_gc_events_rejects_negative_window(board):
    with kbc.connect_closing() as conn:
        tid = _done_task_with_old_event(conn)
        with pytest.raises(ValueError, match="older_than_seconds"):
            kb.gc_events(conn, older_than_seconds=-86400)
        assert _event_rows(conn, tid) > 0

def test_gc_worker_logs_rejects_negative_window(board):
    log = _old_log_file()
    with pytest.raises(ValueError, match="older_than_seconds"):
        kb.gc_worker_logs(older_than_seconds=-86400)
    assert log.exists()

def _archived_scratch_workspace(conn) -> Path:
    tid = kb.create_task(conn, title="archived with workspace")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch' WHERE id=?",
            (tid,),
        )
    ws = kb.workspaces_root() / tid
    ws.mkdir(parents=True)
    (ws / "scratch.txt").write_text("keep me")
    return ws

@pytest.mark.parametrize(
    ("days", "expect_rc", "expect_kept"),
    [
        pytest.param(-1, 2, True, id="negative-refuses"),
        pytest.param(0, 0, True, id="zero-disables"),
        pytest.param(30, 0, False, id="positive-collects"),
    ],
)
def test_cmd_gc_retention_bounds(board, days, expect_rc, expect_kept):
    """Invalid retention must refuse before ANY sweep: the (unconditional)
    workspace collection runs first in the command body, so the archived
    scratch workspace surviving the negative case proves ordering, not just
    event/log preservation. Valid values (0 or positive) let it run."""
    with kbc.connect_closing() as conn:
        tid = _done_task_with_old_event(conn)
        ws = _archived_scratch_workspace(conn)
    log = _old_log_file()
    assert kanban_ops._cmd_gc(_args(event_days=days, log_days=days)) == expect_rc
    with kbc.connect_closing() as conn:
        assert (_event_rows(conn, tid) > 0) is expect_kept
    assert log.exists() is expect_kept
    assert (ws / "scratch.txt").exists() is (expect_rc != 0)

@pytest.mark.parametrize(
    ("days", "expected"),
    [
        pytest.param("-1", "must be >= 0", id="negative-blocked"),
        pytest.param("0", "GC complete", id="zero-disables"),
    ],
)
def test_slash_kanban_gc_retention_bounds(board, days, expected):
    """``/kanban gc`` from a chat session uses the same argparse type as the
    shell command: ``-1`` is rejected by the parser type before ``_cmd_gc``
    runs (usage error); ``0`` parses, reaches ``_cmd_gc``, and disables the
    sweep."""
    from hermes_cli import kanban
    with kbc.connect_closing() as conn:
        tid = _done_task_with_old_event(conn)
    log = _old_log_file()
    out = kanban.run_slash(f"gc --event-retention-days {days} --log-retention-days {days}")
    assert expected in out
    with kbc.connect_closing() as conn:
        assert _event_rows(conn, tid) > 0
    assert log.exists()


def test_cmd_gc_never_removes_the_workspaces_root_itself(board):
    # A scratch task whose workspace_path is the managed root itself (reachable via
    # kanban_create) must never make gc wipe every other task's scratch dir.
    root = kb.workspaces_root()
    sibling = root / "t_other"
    sibling.mkdir(parents=True)
    (sibling / "work.txt").write_text("another task's scratch", encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="archived scratch")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='archived', workspace_kind='scratch', "
                "workspace_path=? WHERE id=?",
                (str(root), tid),
            )
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (sibling / "work.txt").exists()


def _shared_scratch(conn) -> tuple[str, str, Path]:
    archived = kb.create_task(conn, title="archived sharer")
    live = kb.create_task(conn, title="live sharer")
    shared = kb.workspaces_root() / "shared-scratch"
    shared.mkdir(parents=True)
    (shared / "note.txt").write_text("still needed", encoding="utf-8")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(shared), archived),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(shared), live),
        )
    return archived, live, shared


def _deferred_reasons(conn, task_id: str) -> list:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, "workspace_cleanup_deferred_shared"),
    ).fetchall()
    return [json.loads(r["payload"])["reason"] for r in rows]


def test_cmd_gc_keeps_a_scratch_dir_a_live_task_still_uses(board):
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect_closing() as conn:
        archived, _live, shared = _shared_scratch(conn)
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (shared / "note.txt").read_text(encoding="utf-8") == "still needed"
    with kbc.connect_closing() as conn:
        assert _deferred_reasons(conn, archived) == ["shared"]
    # Once the dir is gone there is nothing to defer: no scan, no new event.
    shutil.rmtree(shared)
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
        assert _deferred_reasons(conn, archived) == ["shared"]


def test_cleanup_workspace_keeps_scratch_when_other_boards_cannot_be_listed(board, monkeypatch):
    """A named board's live task uses the dir; if boards cannot be listed, refuse the delete."""
    from hermes_cli import kanban_db_workspace as kbw

    kb.create_board("other")
    shared = kb.workspaces_root() / "cross-board"
    shared.mkdir(parents=True)
    (shared / "note.txt").write_text("still needed", encoding="utf-8")
    with kbc.connect_closing() as conn:
        archived = kb.create_task(conn, title="archived on default")
    with kbc.connect_closing(board="other") as other:
        live = kb.create_task(other, title="live on other")
    for board_slug, task_id, status in ((None, archived, "archived"), ("other", live, "ready")):
        with kbc.connect_closing(board=board_slug) as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status=?, workspace_kind='scratch', workspace_path=? WHERE id=?",
                (status, str(shared), task_id),
            )
    # Real scan: the other board's live task holds the dir, under a different
    # id and under the very id being cleaned up (boards mint ids independently).
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
    assert (shared / "note.txt").exists()
    with kbc.connect_closing(board="other") as other, kb.write_txn(other):
        other.execute("UPDATE tasks SET id=? WHERE id=?", (archived, live))
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
        assert _deferred_reasons(conn, archived) == ["shared", "shared"]
    assert (shared / "note.txt").exists()

    # Nobody holds it any more, but the boards cannot be listed: fail closed.
    with kbc.connect_closing(board="other") as other, kb.write_txn(other):
        other.execute("UPDATE tasks SET status='done' WHERE id=?", (archived,))
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "boards":
            raise PermissionError("boards dir unreadable")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
        assert _deferred_reasons(conn, archived) == ["shared", "shared", "unknown"]
    assert (shared / "note.txt").read_text(encoding="utf-8") == "still needed"


@pytest.mark.parametrize("sibling_kind", ["empty", "unrelated", "locked", "corrupt", "bad-tasks"])
def test_cleanup_distinguishes_absent_tasks_from_unreadable_board(board, monkeypatch, sibling_kind):
    import sqlite3
    from hermes_cli import kanban_db_workspace as kbw

    # No worker is spawned by this fixture; exercise only filesystem cleanup.
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *args: None)
    sibling = kb.boards_root() / "other" / "kanban.db"
    sibling.parent.mkdir(parents=True)
    sibling.touch()
    blocker = None
    if sibling_kind in {"unrelated", "locked", "bad-tasks"}:
        blocker = sqlite3.connect(sibling)
        blocker.execute("CREATE TABLE unrelated (id TEXT)")
        if sibling_kind == "bad-tasks":
            blocker.execute("CREATE TABLE tasks (id TEXT)")
        blocker.commit()
        if sibling_kind == "locked":
            blocker.execute("BEGIN EXCLUSIVE")
    elif sibling_kind == "corrupt":
        sibling.write_bytes(b"not a sqlite database")
    try:
        before = sibling.read_bytes()
        with kbc.connect_closing() as conn:
            ws = _archived_scratch_workspace(conn)
            tid = ws.name
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), tid))
            kbw._cleanup_workspace(conn, tid)
            unreadable = sibling_kind in {"locked", "corrupt", "bad-tasks"}
            assert ws.exists() is unreadable
            assert _deferred_reasons(conn, tid) == (["unknown"] if unreadable else [])
        # The sibling is inspected read-only, never initialized or repaired.
        assert sibling.read_bytes() == before
    finally:
        if blocker is not None:
            blocker.close()


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_gc_retries_deferred_terminal_scratch_without_losing_safety(board, status, capsys):
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect_closing() as conn:
        tid, live, ws = _shared_scratch(conn)
        child = kb.create_task(conn, title="still needs parent artifacts")
        unmarked = kb.create_task(conn, title="not deferred")
        active = kb.create_task(conn, title="not terminal")
        dir_task = kb.create_task(conn, title="user directory")
        outside_task = kb.create_task(conn, title="outside scratch")
        kept = kb.workspaces_root() / unmarked
        kept.mkdir()
        active_ws = kb.workspaces_root() / active
        active_ws.mkdir()
        user_dir = kb.workspaces_root() / dir_task
        user_dir.mkdir()
        outside = board / "user-source"
        outside.mkdir()
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            for task, kind, path in [(unmarked, "scratch", kept), (dir_task, "dir", user_dir),
                                     (outside_task, "scratch", outside)]:
                conn.execute("UPDATE tasks SET status=?, workspace_kind=?, workspace_path=? WHERE id=?",
                             (status, kind, str(path), task))
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(active_ws), active))
            for task in (dir_task, outside_task, active):
                kb._append_event(conn, task, "workspace_cleanup_deferred_shared", {"reason": "unknown"})
            assert kbw._defer_shared_workspace_cleanup(conn, tid, ws)
    # A prior event is not permission to delete a still-shared workspace.
    assert kanban_ops._cmd_gc(_args(0, 0)) == 0
    assert ws.exists()
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (live,))
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (tid, child))
    assert kanban_ops._cmd_gc(_args(0, 0)) == 0
    assert ws.exists()  # live children still need the terminal parent's artifacts
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
    capsys.readouterr()
    assert kanban_ops._cmd_gc(_args(0, 0)) == 0
    assert not ws.exists()
    assert "1 workspace(s)" in capsys.readouterr().out
    assert kept.exists() and user_dir.exists() and outside.exists() and active_ws.exists()
    # A historical event must not count or recreate a workspace already removed.
    assert kanban_ops._cmd_gc(_args(0, 0)) == 0
    assert "0 workspace(s)" in capsys.readouterr().out


def test_gc_retries_unknown_after_sibling_becomes_empty(board):
    sibling = kb.boards_root() / "other" / "kanban.db"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"not a sqlite database")
    with kbc.connect_closing() as conn:
        tid, live, ws = _shared_scratch(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (live,))
        # The first GC attempt records a real unreadable-board deferral.
    assert kanban_ops._cmd_gc(_args(0, 0)) == 0
    with kbc.connect_closing() as conn:
        assert _deferred_reasons(conn, tid) == ["unknown"]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    assert ws.exists()
    sibling.write_bytes(b"")
    assert kanban_ops._cmd_gc(_args(0, 0)) == 0
    assert not ws.exists()
    assert sibling.read_bytes() == b""


@pytest.mark.parametrize("wal", [False, True])
def test_cleanup_preserves_workspace_used_by_readable_sibling(board, monkeypatch, wal):
    import sqlite3
    from hermes_cli import kanban_db_workspace as kbw

    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *args: None)
    sibling = kb.boards_root() / "other" / "kanban.db"
    sibling.parent.mkdir(parents=True)
    with kbc.connect_closing() as conn:
        ws = _archived_scratch_workspace(conn)
        tid = ws.name
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), tid))
        other = sqlite3.connect(sibling)
        try:
            if wal:
                assert other.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            other.execute("CREATE TABLE tasks (id TEXT, status TEXT, workspace_kind TEXT, workspace_path TEXT)")
            # IDs can collide across boards; the sibling row must not be excluded.
            other.execute("INSERT INTO tasks VALUES (?, 'running', 'dir', ?)", (tid, str(ws)))
            other.commit()
            kbw._cleanup_workspace(conn, tid)
            assert ws.exists()
            assert _deferred_reasons(conn, tid) == ["shared"]
        finally:
            other.close()
