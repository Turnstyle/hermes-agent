"""Accepted bundle survival and fail-closed cleanup tests (isolated boards)."""
import hashlib
import json
from pathlib import Path
import tarfile

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as ws
from hermes_cli import kanban_bundle_preserve as bp


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ws, "_cleanup_worker_tmux", lambda *a: None)
    monkeypatch.setattr(ws, "release_lsp_clients", lambda *a: None)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def task(conn, marker=True, result=None, status="done"):
    tid = kb.create_task(conn, title="fixture", workspace_kind="scratch")
    root = kb.workspaces_root() / tid
    root.mkdir(parents=True)
    (root / "accepted.patch").write_text("diff accepted\n")
    if marker:
        (root / "KEEP-BUNDLE").touch()
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=?,workspace_path=?,result=? WHERE id=?", (status, str(root), result, tid))
    return tid, root


def contents(conn, tid):
    attachments = kb.list_attachments(conn, tid)
    assert len(attachments) == 2
    archive = next(Path(a.stored_path) for a in attachments if a.filename.endswith(".tgz"))
    manifest = next(Path(a.stored_path) for a in attachments if a.filename.endswith(".json"))
    data = json.loads(manifest.read_text())
    with tarfile.open(archive) as tar:
        for entry in data:
            member = tar.getmember(entry["path"])
            assert member.isfile()
            payload = tar.extractfile(member).read()
            assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        return tar.getnames()


def test_real_completion_marker(board):
    tid, root = task(board, status="ready")
    assert kb.claim_task(board, tid, claimer="fixture-worker")
    assert kb.complete_task(board, tid, summary="accepted marker bundle")
    assert not root.exists()
    assert "accepted.patch" in contents(board, tid)


def test_result_reference_and_idempotence(board):
    tid, root = task(board, marker=False)
    board.execute("UPDATE tasks SET result=? WHERE id=?", (f"Accepted `{root}/accepted.patch`", tid))
    board.commit()
    bp.preserve_bundle(board, tid, root)
    bp.preserve_bundle(board, tid, root)
    assert contents(board, tid) == ["accepted.patch"]


def test_nested_metadata_reference(board):
    tid, root = task(board, marker=False)
    board.execute("INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)", (tid, json.dumps({"nested": {"bundle": str(root / "accepted.patch")}})))
    board.commit()
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert contents(board, tid) == ["accepted.patch"]


def test_nonterminal_and_deferred_parent(board):
    parent, root = task(board, status="running")
    child, child_root = task(board)
    board.execute("INSERT INTO task_links(parent_id,child_id) VALUES(?,?)", (parent, child))
    board.commit()
    ws._try_cleanup_parent_workspaces(board, child)
    assert root.exists()
    board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    board.commit()
    ws._try_cleanup_parent_workspaces(board, child)
    assert not root.exists()
    assert "accepted.patch" in contents(board, parent)


def test_active_child_defers(board):
    parent, root = task(board)
    child, _ = task(board, status="ready")
    board.execute("INSERT INTO task_links(parent_id,child_id) VALUES(?,?)", (parent, child))
    board.commit()
    ws._cleanup_workspace(board, parent)
    assert root.exists()
    assert kb.list_attachments(board, parent) == []


@pytest.mark.parametrize("name", [".env", "api-key.json", "credentials.json", "token.txt", "link.patch"])
def test_exclusions_and_external_symlink(board, name):
    tid, root = task(board)
    if name == "link.patch":
        (root / name).symlink_to(root / "accepted.patch")
    else:
        (root / name).write_text("excluded fixture")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("excluded")
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert kb.list_attachments(board, tid) == []
    assert board.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchone()


@pytest.mark.parametrize("kind", ["missing", "secret", "linked", "oversize", "copy", "db", "corrupt"])
def test_fail_closed(board, monkeypatch, kind):
    tid, root = task(board)
    if kind in {"missing", "secret", "linked"}:
        p = root / {"missing": "gone.patch", "secret": "secrets.json", "linked": "link.patch"}[kind]
        if kind == "secret":
            p.write_text("excluded fixture")
        if kind == "linked":
            p.symlink_to(root / "accepted.patch")
        board.execute("UPDATE tasks SET result=? WHERE id=?", (str(p), tid))
        board.commit()
    if kind == "oversize":
        original = bp.plan_bundle
        monkeypatch.setattr(bp, "plan_bundle", lambda *a, **kw: original(*a, **{**kw, "max_bytes": 1}))
    if kind == "copy":
        monkeypatch.setattr(bp.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("fixture copy failure")))
    if kind == "db":
        board.execute("CREATE TRIGGER reject_attachment BEFORE INSERT ON task_attachments BEGIN SELECT RAISE(ABORT, 'fixture DB failure'); END")
        board.commit()
    if kind == "corrupt":
        original = bp.tarfile.open
        def corrupt(name, mode, *a, **kw):
            if mode == "r:gz":
                Path(name).write_bytes(b"invalid fixture archive")
            return original(name, mode, *a, **kw)
        monkeypatch.setattr(bp.tarfile, "open", corrupt)
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert kb.list_attachments(board, tid) == []


def test_root_link_and_external_path(board):
    tid, root = task(board)
    link = root.parent / "alias"
    link.symlink_to(root, target_is_directory=True)
    assert not bp.preservation_gate(tid, link, conn=board)
    external = root.parent.parent / "unmanaged"
    external.mkdir()
    board.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(external), tid))
    board.commit()
    ws._cleanup_workspace(board, tid)
    assert external.exists()


def test_archived_gc_gate(board, monkeypatch):
    from argparse import Namespace
    from hermes_cli import kanban_ops
    tid, root = task(board, status="archived")
    assert kanban_ops._cmd_gc(Namespace(event_retention_days=0, log_retention_days=0)) == 0
    assert not root.exists()
    assert "accepted.patch" in contents(board, tid)


def test_no_artifact_path_unchanged(board):
    tid, root = task(board, marker=False)
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert kb.list_attachments(board, tid) == []


def test_directory_reference(board):
    tid, root = task(board, marker=False)
    directory = root / 'bundle'
    directory.mkdir()
    (directory / 'report.md').write_text('accepted report')
    board.execute('UPDATE tasks SET result=? WHERE id=?', (str(directory), tid))
    board.commit()
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert contents(board, tid) == ['bundle/report.md']


def test_gc_failure_keeps_workspace(board):
    from argparse import Namespace
    from hermes_cli import kanban_ops
    tid, root = task(board, status='archived')
    (root / 'token.json').write_text('excluded fixture')
    kanban_ops._cmd_gc(Namespace(event_retention_days=0, log_retention_days=0))
    assert root.exists()
    assert not kb.list_attachments(board, tid)
    assert board.execute('SELECT body FROM task_comments WHERE task_id=?', (tid,)).fetchone()
