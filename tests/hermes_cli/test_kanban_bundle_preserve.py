"""Accepted bundle survival and fail-closed cleanup tests (isolated boards)."""
import hashlib
import json
from pathlib import Path
import sqlite3
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
    (root / "DISPOSABLE-WORKSPACE").write_text(
        '{"version":1,"issuer":"runner","task":"fixture"}\n', encoding="utf-8",
    )
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert (root / "accepted.patch").exists()
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


@pytest.mark.parametrize("location", ["result", "summary", "metadata"])
def test_missing_prose_reference_records_unresolved_and_keeps_artifact(board, location):
    tid, root = task(board, marker=False)
    (root / "blocked_all_parents_done.tsv").write_text("task\tparent\n")
    prose = "Read kanban.db, config.yaml and SKILL.md; see blocked_all_parents_done.tsv."
    if location == "result":
        board.execute("UPDATE tasks SET result=? WHERE id=?", (prose, tid))
    else:
        board.execute(
            "INSERT INTO task_runs(task_id,status,started_at,summary,metadata) VALUES(?,'completed',0,?,?)",
            (tid, prose if location == "summary" else None,
             json.dumps({"notes": prose}) if location == "metadata" else None),
        )
    board.commit()
    report = bp.preserve_bundle(board, tid, root)
    assert [entry["path"] for entry in report["files"]] == ["blocked_all_parents_done.tsv"]
    assert report["unresolved_refs"] == ["SKILL.md", "config.yaml", "kanban.db"]
    assert contents(board, tid) == ["blocked_all_parents_done.tsv"]
    assert root.exists()
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert contents(board, tid) == ["blocked_all_parents_done.tsv"]
    events = board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'",
        (tid,),
    ).fetchall()
    assert [json.loads(row["payload"]) for row in events] == [
        {"unresolved_refs": ["SKILL.md", "config.yaml", "kanban.db"]}
    ]


@pytest.mark.parametrize("reference", ["absolute", "See absolute"])
def test_missing_explicit_references_still_fail_closed(board, reference):
    tid, root = task(board, marker=False)
    reference = reference.replace("absolute", str(root / "gone.patch"))
    board.execute("UPDATE tasks SET result=? WHERE id=?", (reference, tid))
    board.commit()
    with pytest.raises(ValueError, match="referenced bundle member is missing"):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert root.exists()
    assert not kb.list_attachments(board, tid)


@pytest.mark.parametrize("name", ["secrets.json", "dangling.patch", "linked.patch"])
def test_prose_reference_does_not_bypass_security_checks(board, name):
    tid, root = task(board, marker=False)
    if name == "secrets.json":
        (root / name).write_text("excluded fixture")
    elif name == "dangling.patch":
        (root / name).symlink_to(root / "gone.patch")
    elif name == "linked.patch":
        (root / name).symlink_to(root / "accepted.patch")
    board.execute("UPDATE tasks SET result=? WHERE id=?", (f"Read {name} for details", tid))
    board.commit()
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert root.exists()
    assert not kb.list_attachments(board, tid)


def test_empty_bundle_records_unresolved_before_cleanup(board):
    tid, root = task(board, marker=False, result="Read config.yaml and kanban.db; kanban.db is external.")
    (root / "DISPOSABLE-WORKSPACE").write_text(
        '{"version":1,"issuer":"runner","workspace":"fixture"}\n', encoding="utf-8",
    )
    assert bp.preservation_gate(tid, root, conn=board) is False
    assert not board.in_transaction
    ws._cleanup_workspace(board, tid)
    assert root.is_dir()
    assert not kb.list_attachments(board, tid)
    with kbc.connect_closing() as reopened:
        events = reopened.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'",
            (tid,),
        ).fetchall()
    assert [json.loads(row["payload"]) for row in events] == [
        {"unresolved_refs": ["config.yaml", "kanban.db"]}
    ]


@pytest.mark.parametrize("artifact", [False, True])
def test_unresolved_dry_run_is_read_only(board, artifact):
    tid, root = task(board, marker=False, result="Read kanban.db and accepted.patch for details")
    if not artifact:
        (root / "accepted.patch").unlink()
    changes = board.total_changes
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["unresolved_refs"] == (["kanban.db"] if artifact else ["accepted.patch", "kanban.db"])
    assert board.total_changes == changes
    assert not board.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'", (tid,),
    ).fetchone()
    assert not kb.list_attachments(board, tid)
    assert root.exists()


@pytest.mark.parametrize("failure", ["insert", "ignored", "commit"])
def test_unresolved_record_failure_retains_workspace(board, failure):
    tid, root = task(board, marker=False, result="Read kanban.db for details")
    if failure == "commit":
        board.set_authorizer(
            lambda action, arg1, *rest: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
            else sqlite3.SQLITE_OK
        )
    else:
        operation = "ABORT, 'fixture record failure'" if failure == "insert" else "IGNORE"
        board.execute(
            "CREATE TRIGGER reject_unresolved BEFORE INSERT ON task_events "
            "WHEN NEW.kind='bundle_unresolved_refs' BEGIN SELECT RAISE(" + operation + "); END"
        )
        board.commit()
    try:
        ws._cleanup_workspace(board, tid)
    finally:
        board.set_authorizer(None)
    assert root.exists()
    assert not board.in_transaction
    assert not board.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'", (tid,),
    ).fetchone()
    assert not kb.list_attachments(board, tid)


def test_unresolved_record_rejects_uncommitted_board_ops_scope(board):
    """A caller-owned write transaction is not durable cleanup proof."""
    tid, root = task(board, marker=False, result="Read kanban.db for details")
    with kb.write_txn(board):
        assert board.in_transaction
        assert not bp.preservation_gate(tid, root, conn=board)
        assert not board.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'", (tid,),
        ).fetchone()
    assert root.exists()
    assert not kb.list_attachments(board, tid)
