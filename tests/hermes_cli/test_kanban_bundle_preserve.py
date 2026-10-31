"""Accepted bundle survival and fail-closed cleanup tests (isolated boards).

The reference policy under test is the Sheldon contract documented in
``hermes_cli.kanban_bundle_preserve``: required (exact-path) references refuse
cleanup when missing, inferred prose references are filed in a durable
``bundle_unresolved_refs`` event, and every failure retains the workspace.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
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
    for key in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT"):
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


def unresolved_events(conn, tid):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='bundle_unresolved_refs'", (tid,),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_real_completion_marker(board):
    tid, root = task(board, status="ready")
    claimed = kb.claim_task(board, tid, claimer="fixture-worker")
    assert claimed
    assert kb.complete_task(board, tid, summary="accepted marker bundle", expected_run_id=claimed.current_run_id)
    assert not root.exists()
    assert "accepted.patch" in contents(board, tid)


def test_archive_task_preserves_bundle_before_removal(board):
    tid, root = task(board, status="ready")
    assert kb.archive_task(board, tid)
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


def test_uncommitted_receipt_loses_no_workspace_or_archive_bytes(board):
    """Registration COMMIT failing after the archive landed keeps both sides."""
    tid, root = task(board)
    workspace_bytes = (root / "accepted.patch").read_bytes()
    board.set_authorizer(
        lambda action, arg1, *rest: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
        else sqlite3.SQLITE_OK
    )
    try:
        assert not bp.preservation_gate(tid, root, conn=board)
        ws._cleanup_workspace(board, tid)
    finally:
        board.set_authorizer(None)
    assert not board.in_transaction
    assert (root / "accepted.patch").read_bytes() == workspace_bytes
    assert kb.list_attachments(board, tid) == []
    archives = sorted(kb.task_attachments_dir(tid).glob("accepted-bundle-*.tgz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as tar:
        assert tar.extractfile("accepted.patch").read() == workspace_bytes
    # The retained bytes are content-addressed: a retry registers them once.
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert "accepted.patch" in contents(board, tid)
    assert sorted(kb.task_attachments_dir(tid).glob("accepted-bundle-*.tgz")) == archives


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
    assert unresolved_events(board, tid) == [{"unresolved_refs": ["SKILL.md", "config.yaml", "kanban.db"]}]


@pytest.mark.parametrize("reference", ["./gone.patch", "See ./gone.patch", "absolute", "See absolute"])
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
    if name == "dangling.patch":
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
    assert bp.preservation_gate(tid, root, conn=board)
    assert not board.in_transaction
    ws._cleanup_workspace(board, tid)
    assert not root.exists()
    assert not kb.list_attachments(board, tid)
    with kbc.connect_closing() as reopened:
        assert unresolved_events(reopened, tid) == [{"unresolved_refs": ["config.yaml", "kanban.db"]}]


@pytest.mark.parametrize("artifact", [False, True])
def test_unresolved_dry_run_is_read_only(board, artifact):
    tid, root = task(board, marker=False, result="Read kanban.db and accepted.patch for details")
    if not artifact:
        (root / "accepted.patch").unlink()
    changes = board.total_changes
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert report["unresolved_refs"] == (["kanban.db"] if artifact else ["accepted.patch", "kanban.db"])
    assert board.total_changes == changes
    assert unresolved_events(board, tid) == []
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
    assert unresolved_events(board, tid) == []
    assert not kb.list_attachments(board, tid)


def test_unresolved_record_rejects_uncommitted_board_ops_scope(board):
    from hermes_cli.kanban_board_ops_transactions import compose_task_transition

    tid, root = task(board, marker=False, result="Read kanban.db for details")
    with kb.write_txn(board):
        with compose_task_transition(board, tid):
            assert not bp.preservation_gate(tid, root, conn=board)
            assert unresolved_events(board, tid) == []
    assert root.exists()
    assert not kb.list_attachments(board, tid)


# --- Node-variant replays against the Sheldon policy -------------------------
# M/R/N refused cleanup for ANY missing reference; TurnerBook filed every
# missing path (even exact ones) as non-durable "unresolved". Each row pins
# what the chosen policy does with that variant's case.


@pytest.mark.parametrize("origin,result,metadata,cleaned,unresolved", [
    ("M/R/N inferred prose name", "Read gone.patch for details", None, True, ["gone.patch"]),
    ("M/R/N explicit absolute path", "{root}/gone.patch", None, False, None),
    ("TurnerBook bare result name", "gone.patch", None, True, ["gone.patch"]),
    ("TurnerBook exact metadata path", None, {"nested": ["missing/report.md"]}, False, None),
])
def test_node_variant_missing_reference_replay(board, origin, result, metadata, cleaned, unresolved):
    tid, root = task(board)
    if result is not None:
        board.execute("UPDATE tasks SET result=? WHERE id=?", (result.format(root=root), tid))
        board.commit()
    if metadata is not None:
        board.execute("INSERT INTO task_runs(task_id,status,started_at,metadata) VALUES(?,'completed',0,?)",
                      (tid, json.dumps(metadata)))
        board.commit()
    ws._cleanup_workspace(board, tid)
    assert root.exists() is not cleaned, origin
    if cleaned:
        assert "accepted.patch" in contents(board, tid)
        assert unresolved_events(board, tid) == [{"unresolved_refs": unresolved}]
    else:
        assert kb.list_attachments(board, tid) == []
        assert unresolved_events(board, tid) == []
        assert board.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchone()


def test_inferred_prose_refs_skip_while_real_reference_is_archived(board):
    tid, root = task(board, marker=False)
    repo = root / 'source/tests/hermes_cli'
    repo.mkdir(parents=True)
    (repo / 'test_example.py').write_text('not explicitly referenced')
    summary = ('Branch fix/t_example-cooldown; version 5.5; '
               'tests/hermes_cli/test_example.py; agy/claude-sonnet-5-5-high; '
               f'accepted `{root}/accepted.patch`.')
    board.execute("INSERT INTO task_runs(task_id,status,started_at,summary) VALUES(?,'completed',0,?)",
                  (tid, summary))
    board.commit()
    report = bp.preserve_bundle(board, tid, root, dry_run=True)
    assert [e['path'] for e in report['files']] == ['accepted.patch']
    assert {'tests/hermes_cli/test_example.py', '5.5'} <= set(report['unresolved_refs'])
    assert report['unresolved_refs'] == sorted(set(report['unresolved_refs']))
    assert kb.list_attachments(board, tid) == []
    bp.preserve_bundle(board, tid, root)
    assert contents(board, tid) == ['accepted.patch']


def test_missing_reference_is_reported_with_keep_bundle(board):
    tid, root = task(board, result='gone.patch')
    report = bp.preserve_bundle(board, tid, root)
    assert report['unresolved_refs'] == ['gone.patch']
    assert 'accepted.patch' in contents(board, tid)
    assert unresolved_events(board, tid) == [{"unresolved_refs": ["gone.patch"]}]


@pytest.mark.parametrize('kind', ['dangling', 'linked-parent', 'missing-secret', 'fifo'])
def test_missing_skip_does_not_hide_unsafe_reference(board, kind):
    tid, root = task(board, marker=False)
    p = root / 'unsafe.patch'
    if kind == 'dangling':
        p.symlink_to(root / 'gone.patch')
    elif kind == 'linked-parent':
        p.mkdir()
        alias = root / 'alias'
        alias.symlink_to(p, target_is_directory=True)
        p = alias / 'gone.patch'
    elif kind == 'missing-secret':
        p = root / 'credentials.json'
    else:
        os.mkfifo(p)
    board.execute('UPDATE tasks SET result=? WHERE id=?', (str(p), tid))
    board.commit()
    with pytest.raises(ValueError):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    ws._cleanup_workspace(board, tid)
    assert root.exists()
    assert kb.list_attachments(board, tid) == []


def test_change_while_hashing_still_fails_closed(board, monkeypatch):
    tid, root = task(board, marker=False, result='accepted.patch')
    original = bp.os.fstat
    calls = 0
    def changed(fd):
        nonlocal calls
        calls += 1
        if calls == 3:
            with (root / 'accepted.patch').open('a') as out:
                out.write('changed')
        return original(fd)
    monkeypatch.setattr(bp.os, 'fstat', changed)
    with pytest.raises(ValueError, match='changed while hashing'):
        bp.preserve_bundle(board, tid, root, dry_run=True)
    assert root.exists()
    assert kb.list_attachments(board, tid) == []


def test_member_disappearing_after_archive_write_fails_closed(board, monkeypatch):
    tid, root = task(board, marker=False, result='accepted.patch')
    member = root / 'accepted.patch'
    original = bp.tarfile.TarFile.addfile
    removed = False

    def addfile(archive, info, fileobj=None):
        nonlocal removed
        result = original(archive, info, fileobj)
        if info.name == 'accepted.patch':
            member.unlink()
            removed = True
        return result

    monkeypatch.setattr(bp.tarfile.TarFile, 'addfile', addfile)
    with pytest.raises(ValueError, match='inventory changed while preserving'):
        bp.preserve_bundle(board, tid, root)
    assert removed
    assert not member.exists()
    assert root.is_dir()
    assert kb.list_attachments(board, tid) == []


# --- Linked worktree gate ------------------------------------------------------
# Git itself is faked at the module seams: the gate must run after the clean /
# pushed predicates and before ``git worktree remove``, so a refusal never
# reaches git.


@pytest.fixture
def worktree_task(board, tmp_path, monkeypatch):
    from hermes_cli import worktree_ops

    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    tid = kb.create_task(board, title="worktree fixture", workspace_kind="worktree")
    wt = repo / ".worktrees" / tid
    wt.mkdir(parents=True)
    (wt / "README.md").write_text("hello\n")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done', workspace_path=? WHERE id=?", (str(wt), tid))
    calls = []

    def fake_git(repo_root, *args, timeout):
        calls.append(args)
        if args[:2] == ("worktree", "remove"):
            import shutil
            shutil.rmtree(args[2])
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(ws, "_git_common_dir", lambda path: repo / ".git")
    monkeypatch.setattr(worktree_ops, "_worktree_is_dirty", lambda *a: False)
    monkeypatch.setattr(worktree_ops, "_worktree_has_unpushed_commits", lambda *a: False)
    monkeypatch.setattr(ws, "_git", fake_git)
    return tid, wt, calls


def test_worktree_gate_refusal_never_reaches_git_remove(board, worktree_task):
    tid, wt, calls = worktree_task
    board.execute("UPDATE tasks SET result=? WHERE id=?", (str(wt / "secrets.json"), tid))
    board.commit()
    ws._cleanup_workspace(board, tid)
    assert wt.is_dir()
    assert not [c for c in calls if c[:2] == ("worktree", "remove")]
    assert board.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchone()


def test_worktree_referenced_file_archived_before_removal(board, worktree_task):
    tid, wt, calls = worktree_task
    board.execute("UPDATE tasks SET result=? WHERE id=?", (f"Accepted {wt / 'README.md'}", tid))
    board.commit()
    ws._cleanup_workspace(board, tid)
    assert not wt.exists()
    assert ("worktree", "remove", str(wt)) in calls
    assert contents(board, tid) == ["README.md"]


def test_worktree_cleanup_for_unknown_task_is_retained(board, worktree_task):
    """No terminal task row means no accepted-bundle owner: fail closed."""
    _, wt, calls = worktree_task
    ws._cleanup_worktree_workspace("t_no_such_row", str(wt), conn=board)
    assert wt.is_dir()
    assert not [c for c in calls if c[:2] == ("worktree", "remove")]
