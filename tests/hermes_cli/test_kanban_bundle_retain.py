"""Retain-by-default cleanup: empty plans, markers, and real removal paths."""
import os
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from hermes_cli import kanban_ops
from tests.hermes_cli.test_kanban_bundle_preserve import board, contents, task


def _comment(conn, tid) -> str:
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC", (tid,),
    ).fetchone()
    assert row
    return row["body"]


def test_empty_plan_with_loose_file_retains(board):
    tid, root = task(board, marker=False)
    (root / "loose.txt").write_text("keep the loose file\n", encoding="utf-8")
    assert kb.list_attachments(board, tid) == []
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert (root / "loose.txt").read_text(encoding="utf-8") == "keep the loose file\n"
    assert (root / "accepted.patch").exists()
    assert kb.list_attachments(board, tid) == []
    body = _comment(board, tid)
    assert "empty preservation plan" in body
    assert "keep the loose file" not in body


def test_preexisting_attachment_is_not_proof(board):
    tid, root = task(board, marker=False)
    outside = root.parent / "unrelated.txt"
    outside.write_text("not a bundle\n", encoding="utf-8")
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
            (tid, "unrelated.txt", str(outside), "text/plain", outside.stat().st_size, "fixture"),
        )
    assert len(kb.list_attachments(board, tid)) == 1
    assert bp.preservation_gate(tid, root, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert [item.filename for item in kb.list_attachments(board, tid)] == ["unrelated.txt"]


def test_hold_wins_over_runner_marker_and_keep_before_planning(board, monkeypatch):
    tid, root = task(board)
    (root / bp.HOLD_MARKER).write_text("hold\n", encoding="utf-8")
    (root / "DISPOSABLE-WORKSPACE").write_text(
        '{"version":1,"task":"%s","workspace":"fixture","issuer":"runner"}\n' % tid,
        encoding="utf-8",
    )
    planned = {"n": 0}

    def boom(*_args, **_kwargs):
        planned["n"] += 1
        raise AssertionError("planned despite hold")

    monkeypatch.setattr(bp, "plan_bundle", boom)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert planned["n"] == 0
    ws._cleanup_workspace(board, tid)
    assert (root / "accepted.patch").read_text(encoding="utf-8") == "diff accepted\n"
    assert kb.list_attachments(board, tid) == []
    assert "hold marker" in _comment(board, tid)


@pytest.mark.parametrize("setup", ["linked", "directory"])
def test_hold_presence_retains_before_planning(board, setup, monkeypatch):
    tid, root = task(board, marker=False, result="ship `accepted.patch`")
    planned = {"n": 0}

    def boom(*_args, **_kwargs):
        planned["n"] += 1
        raise AssertionError("planned despite hold")

    monkeypatch.setattr(bp, "plan_bundle", boom)
    marker = root / bp.HOLD_MARKER
    if setup == "linked":
        marker.symlink_to(root / "accepted.patch")
    else:
        marker.mkdir()
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert planned["n"] == 0
    ws._cleanup_workspace(board, tid)
    assert (root / "accepted.patch").read_text(encoding="utf-8") == "diff accepted\n"
    body = _comment(board, tid)
    assert "hold marker" in body
    assert "diff accepted" not in body


@pytest.mark.parametrize("kind", ["lstat", "open"])
def test_hold_probe_error_retains_before_planning(board, kind, monkeypatch):
    tid, root = task(board, marker=False, result="ship `accepted.patch`")
    (root / bp.HOLD_MARKER).write_text("hold\n", encoding="utf-8")
    planned = {"n": 0}

    def boom(*_args, **_kwargs):
        planned["n"] += 1
        raise AssertionError("planned despite probe error")

    monkeypatch.setattr(bp, "plan_bundle", boom)
    if kind == "lstat":
        real_lstat = os.lstat

        def denied(path, *args, **kwargs):
            if str(path).endswith(bp.HOLD_MARKER):
                raise OSError(13, "denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", denied)
    else:
        real_open = os.open

        def denied(path, flags, *args, **kwargs):
            if not isinstance(path, int) and str(path).endswith(bp.HOLD_MARKER):
                raise OSError(13, "denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert planned["n"] == 0
    assert (root / "accepted.patch").read_text(encoding="utf-8") == "diff accepted\n"
    body = _comment(board, tid)
    assert "hold probe failed" in body
    assert "diff accepted" not in body


def test_hold_dry_run_writes_nothing(board):
    tid, root = task(board, marker=False, result="`accepted.patch`")
    (root / bp.HOLD_MARKER).write_text("hold\n", encoding="utf-8")
    changes = board.total_changes
    with pytest.raises(ValueError, match="hold marker"):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert board.total_changes == changes
    assert root.is_dir()
    assert kb.list_attachments(board, tid) == []


@pytest.mark.parametrize("prose", [
    "accepted.patch",
    "`accepted.patch`",
    '"accepted.patch"',
    "accepted.patch:12",
])
def test_referenced_forms_survive_cleanup(board, prose):
    tid, root = task(board, marker=False, result=prose)
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert contents(board, tid) == ["accepted.patch"]


def test_plain_prose_without_a_path_retains_loose_file(board):
    tid, root = task(board, marker=False, result="See the notes for context")
    (root / "loose.txt").write_text("still here\n", encoding="utf-8")
    ws._cleanup_workspace(board, tid)
    assert (root / "loose.txt").read_text(encoding="utf-8") == "still here\n"
    assert kb.list_attachments(board, tid) == []


def test_complete_and_archive_keep_durable_bytes(board):
    ready, ready_root = task(board, marker=False, status="ready")
    assert kb.list_attachments(board, ready) == []
    assert kb.claim_task(board, ready, claimer="fixture-worker")
    assert kb.complete_task(board, ready, result="ship `accepted.patch`")
    assert not ready_root.exists()
    assert contents(board, ready) == ["accepted.patch"]

    archived, archived_root = task(board, result="ship `accepted.patch`")
    (archived_root / "token.txt").write_text("excluded fixture\n", encoding="utf-8")
    assert kb.archive_task(board, archived)
    assert archived_root.is_dir()
    assert not kb.list_attachments(board, archived)
    assert "excluded fixture" not in _comment(board, archived)

    clean, clean_root = task(board, marker=False, result="ship `accepted.patch`")
    assert kb.archive_task(board, clean)
    assert not clean_root.exists()
    assert contents(board, clean) == ["accepted.patch"]


def test_deferred_parent_and_gc_share_the_gate(board):
    parent, root = task(board, marker=False)
    child, _child_root = task(board, marker=False)
    board.execute(
        "INSERT INTO task_links(parent_id, child_id) VALUES(?, ?)", (parent, child),
    )
    board.commit()
    ws._try_cleanup_parent_workspaces(board, child)
    assert (root / "accepted.patch").exists()
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?", ("ship `accepted.patch`", parent),
    )
    board.commit()
    ws._try_cleanup_parent_workspaces(board, child)
    assert not root.exists()
    assert contents(board, parent) == ["accepted.patch"]

    loose, loose_root = task(board, marker=False, status="archived")
    (loose_root / "loose.txt").write_text("gc keep\n", encoding="utf-8")
    assert kanban_ops._cmd_gc(Namespace(event_retention_days=0, log_retention_days=0)) == 0
    assert (loose_root / "loose.txt").read_text(encoding="utf-8") == "gc keep\n"
    board.execute(
        "UPDATE tasks SET result=? WHERE id=?", ("`loose.txt`", loose),
    )
    board.commit()
    assert kanban_ops._cmd_gc(Namespace(event_retention_days=0, log_retention_days=0)) == 0
    assert not loose_root.exists()
    names = contents(board, loose)
    assert names == ["loose.txt"]


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_completion_and_archive_stay_committed_when_retained(board):
    ready, ready_root = task(board, marker=False, status="ready")
    (ready_root / "loose.txt").write_text("keep the loose file\n", encoding="utf-8")
    (ready_root / "DISPOSABLE-WORKSPACE").write_text(
        '{"version":1,"task":"%s","workspace":"fixture","issuer":"runner"}\n' % ready,
        encoding="utf-8",
    )
    assert kb.list_attachments(board, ready) == []
    assert kb.claim_task(board, ready, claimer="fixture-worker")
    assert kb.complete_task(board, ready, result="finished without a bundle path")
    assert kb.get_task(board, ready).status == "done"
    assert (ready_root / "loose.txt").read_text(encoding="utf-8") == "keep the loose file\n"
    assert kb.list_attachments(board, ready) == []

    archived, archived_root = task(board, marker=False)
    outside = archived_root.parent / "unrelated.txt"
    outside.write_text("not a bundle\n", encoding="utf-8")
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
            (archived, "unrelated.txt", str(outside), "text/plain", outside.stat().st_size, "fixture"),
        )
    (archived_root / "DISPOSABLE-WORKSPACE").write_text(
        '{"version":1,"task":"%s","workspace":"fixture","issuer":"control-plane"}\n' % archived,
        encoding="utf-8",
    )
    assert len(kb.list_attachments(board, archived)) == 1
    assert kb.archive_task(board, archived)
    assert kb.get_task(board, archived).status == "archived"
    assert archived_root.is_dir()
    assert [item.filename for item in kb.list_attachments(board, archived)] == ["unrelated.txt"]


def test_linked_worktree_removal_uses_the_same_gate(board, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    project = tmp_path / "project"
    subprocess.run(
        ["git", "clone", str(origin), str(project)], check=True, capture_output=True,
    )
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "init")
    _git(project, "push", "origin", "HEAD")
    tid = kb.create_task(board, title="worktree fixture", workspace_kind="worktree")
    wt = project / ".worktrees" / tid
    ws._ensure_git_worktree(project, wt, f"wt/{tid}")
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET status='done', workspace_kind='worktree', "
            "workspace_path=?, branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    ws._cleanup_worktree_workspace(tid, str(wt), f"wt/{tid}", conn=board)
    assert (wt / "README.md").read_text(encoding="utf-8") == "hello\n"
    marker = '{"version":1,"task":"%s","workspace":"fixture","issuer":"runner"}\n' % tid
    (wt / "DISPOSABLE-WORKSPACE").write_text(marker, encoding="utf-8")
    ws._cleanup_worktree_workspace(tid, str(wt), f"wt/{tid}", conn=board)
    assert (wt / "README.md").exists()
    _git(wt, "add", "DISPOSABLE-WORKSPACE")
    _git(wt, "commit", "-m", "runner marker")
    _git(wt, "push", "origin", "HEAD")
    ws._cleanup_worktree_workspace(tid, str(wt), f"wt/{tid}", conn=board)
    assert (wt / "README.md").read_text(encoding="utf-8") == "hello\n"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET result=? WHERE id=?", ("ship `README.md`", tid))
    ws._cleanup_worktree_workspace(tid, str(wt), f"wt/{tid}", conn=board)
    assert not wt.exists()


def test_symlink_root_retains_without_secret_text(board):
    tid, root = task(board, marker=False, result="ship `accepted.patch`")
    alias = root.parent / "alias-root"
    alias.symlink_to(root, target_is_directory=True)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(alias), tid))
    assert bp.preservation_gate(tid, alias, conn=board) is False
    ws._cleanup_workspace(board, tid)
    assert (root / "accepted.patch").read_text(encoding="utf-8") == "diff accepted\n"
    assert kb.list_attachments(board, tid) == []
    body = _comment(board, tid)
    assert "workspace root is missing or linked" in body
    assert "diff accepted" not in body
