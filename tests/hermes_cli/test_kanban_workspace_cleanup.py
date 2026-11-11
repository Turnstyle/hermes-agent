"""Stock-shaped workspace cleanup: one-level parent sweep, archived-only gc, retained guards.

Behavioral contracts for the return-to-stock rollback; does not exercise aged-done gc,
recursive ancestors, live-cwd deferral, or cleanup-failure telemetry.
"""
import argparse
import json
import os
import stat
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes"))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def scratch(conn, title):
    tid = kb.create_task(conn, title=title)
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    kbw.set_workspace_path(conn, tid, ws)
    (ws / "proof.txt").write_text(title)
    return tid, ws


def link(conn, parent, child):
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, child))


def gc():
    return kanban_ops._cmd_gc(
        argparse.Namespace(event_retention_days=0, log_retention_days=0),
    )


def test_gc_removes_archived_scratch_but_not_done(board):
    with kbc.connect_closing() as conn:
        done_id, done_ws = scratch(conn, "finished scratch")
        archived_id, archived_ws = scratch(conn, "archived scratch")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done', completed_at=1 WHERE id=?", (done_id,))
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (archived_id,))
    assert gc() == 0
    assert done_ws.exists(), "stock gc never sweeps done scratch workspaces"
    assert not archived_ws.exists()


def test_final_leaf_cleans_immediate_parent_only(board):
    with kbc.connect_closing() as conn:
        grandparent, gpws = scratch(conn, "grandparent")
        parent, pws = scratch(conn, "parent")
        leaf, lws = scratch(conn, "leaf")
        link(conn, grandparent, parent)
        link(conn, parent, leaf)
        for tid in (grandparent, parent):
            assert kb.complete_task(conn, tid, result=f"{tid} done")
        assert gpws.exists()
        assert pws.exists()
        assert kb.complete_task(conn, leaf, result="leaf done")
        assert not lws.exists()
        assert not pws.exists(), "one-level parent sweep removes direct parent"
        assert gpws.exists(), "stock does not recurse to grandparent"


def test_deferred_child_cleanup_does_not_sweep_parent_while_grandchild_active(board):
    """Stock returns before ``_try_cleanup_parent_workspaces`` when the child still has live offspring."""
    with kbc.connect_closing() as conn:
        parent, pws = scratch(conn, "parent")
        child, cws = scratch(conn, "child")
        grandchild, _ = scratch(conn, "grandchild")
        link(conn, parent, child)
        link(conn, child, grandchild)
        assert kb.complete_task(conn, parent, result="parent done")
        assert pws.exists()
        assert kb.complete_task(conn, child, result="child done")
        assert cws.exists(), "active grandchild still needs child's workspace"
        assert pws.exists(), "parent sweep runs only after the child's own cleanup path finishes"


def test_missing_persisted_path_does_not_synthesize_scratch_delete(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="no persisted path", workspace_kind="scratch")
        default_ws = kb.workspaces_root() / tid
        default_ws.mkdir(parents=True)
        (default_ws / "orphan.txt").write_text("on disk only")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', workspace_path=NULL WHERE id=?",
                (tid,),
            )
        kbw._cleanup_workspace(conn, tid)
    assert default_ws.exists(), "stock skips cleanup when workspace_path is unset"


@pytest.mark.parametrize("status", ["ready", "running", "blocked"])
def test_non_terminal_task_workspace_not_removed(board, status):
    with kbc.connect_closing() as conn:
        tid, ws = scratch(conn, "still live")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        kbw._cleanup_workspace(conn, tid)
    assert ws.exists()


@pytest.mark.platforms("linux", "macos")
def test_read_only_child_dir_left_without_mode_escalation(board):
    with kbc.connect_closing() as conn:
        tid, ws = scratch(conn, "read-only subtree")
        frozen = ws / "frozen"
        frozen.mkdir()
        (frozen / "note.txt").write_text("keep")
        frozen.chmod(0o555)
        before = frozen.stat().st_mode
        assert kb.complete_task(conn, tid, result="done with frozen tree")
        assert ws.exists()
        assert frozen.exists()
        assert frozen.stat().st_mode == before


def test_tools_complete_removes_real_temp_workspace(board):
    from tools import kanban_tools

    with kbc.connect_closing() as conn:
        tid, ws = scratch(conn, "tool completion")
    response = json.loads(kanban_tools._handle_complete({"task_id": tid, "result": "temp-board proof"}))
    assert response["ok"] is True
    assert not ws.exists()
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
