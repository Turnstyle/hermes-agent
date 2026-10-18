"""Runtime retention contracts for t_3d320127, using disposable Git and SQLite."""
from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest

import hermes_cli
ROOT = Path(__file__).resolve().parents[2]
assert Path(hermes_cli.__file__).resolve().is_relative_to(ROOT)
# Some installed editable finders register this module individually. Load this
# checkout explicitly before the facade can capture a candidate's workflow.
spec = importlib.util.spec_from_file_location('hermes_cli.kanban_workflow', ROOT / 'hermes_cli/kanban_workflow.py')
workflow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = workflow
spec.loader.exec_module(workflow)
hermes_cli.kanban_workflow = workflow
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as ws
from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import kanban_ops as ops
from hermes_cli import worktree_ops as wt
from hermes_cli import worktree_gc as gc
from hermes_cli.retention import RetentionRefused
from hermes_cli.retention_markers import MarkerStatus, RetainAll, inspect_keep_bundle
from tools import subagent_worktree as child


@pytest.fixture
def board(tmp_path, monkeypatch):
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_HOME', 'HERMES_KANBAN_BOARD',
                'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_KANBAN_ATTACHMENTS_ROOT'):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('network forbidden')))
    for module in (kb, kbc, ws, bp, ops, wt, gc, child, workflow):
        assert Path(module.__file__).resolve().is_relative_to(ROOT), module.__file__
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def task(conn, root, kind='scratch', status='ready', result='accepted handoff'):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'accepted.patch').write_bytes(b'accepted bytes\n')
    (root / 'notes.ignored').write_bytes(b'ignored bytes\n')
    tid = kb.create_task(conn, title='disposable retention fixture', workspace_kind=kind)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET workspace_path=?,status=?,result=?,branch_name=? WHERE id=?',
                     (str(root), status, result, 'wt/fixture', tid))
    return tid


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd), *args],
                          check=True, capture_output=True, text=True, timeout=10,
                          env={'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'HOME': str(cwd.parent),
                               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}).stdout.strip()


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    (repo / '.gitignore').write_text('.worktrees/\n*.ignored\n')
    git(repo, 'add', '.gitignore')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'base')
    path = repo / '.worktrees/custom-retained'
    git(repo, 'worktree', 'add', '-b', 'wt/fixture', str(path))
    git(repo, 'worktree', 'lock', '--reason', 'HOLD manual owner', str(path))
    return repo, path


@contextmanager
def forbidden(monkeypatch):
    """Assert attempted mutation as well as surviving bytes at real boundaries."""
    with monkeypatch.context() as m:
        spies = []
        for target, attr in ((shutil, 'rmtree'), (os, 'unlink'), (os, 'remove'), (os, 'rename'),
                             (os, 'replace'), (ws, 'release_lsp_clients'), (wt, 'release_lsp_clients'),
                             (ws, '_cleanup_worker_tmux'), (ws, '_git'), (wt, '_git'),
                             (wt, '_git_quiet'), (os, 'kill')):
            spy = Mock(side_effect=AssertionError('forbidden ' + attr))
            m.setattr(target, attr, spy)
            spies.append(spy)
        yield
        for spy in spies:
            spy.assert_not_called()


@pytest.mark.parametrize('kind', ['scratch', 'worktree'])
@pytest.mark.parametrize('permit', [None, '{malformed', '{"schema":"hermes.retention.authorization/v1","signature":"valid-looking"}'])
@pytest.mark.parametrize('proof', [False, True])
def test_complete_parent_direct_and_gc_retain(board, tmp_path, tree, monkeypatch, capsys, kind, permit, proof):
    repo, worktree = tree
    root = worktree if kind == 'worktree' else kb.workspaces_root() / 'retention-fixture'
    tid = task(board, root, kind=kind, result=f'Accepted `{root}/accepted.patch`')
    if permit is not None:
        (root / 'destruction-permit.json').write_text(permit)
    if proof:
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        report = bp.preserve_bundle(board, tid, root)
        assert report['attachments']
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    before_files = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
    original_inode = root.stat().st_ino
    oid = git(repo, 'rev-parse', 'wt/fixture')
    registration = git(repo, 'worktree', 'list', '--porcelain')
    attachments = kb.list_attachments(board, tid)
    artifact_bytes = {a.stored_path: Path(a.stored_path).read_bytes() for a in attachments}
    kid_root = tmp_path / 'child'
    kid = task(board, kid_root, kind='dir')
    with kb.write_txn(board):
        board.execute('INSERT INTO task_links(parent_id,child_id) VALUES(?,?)', (tid, kid))
    # A dir child reaches deferred-parent cleanup without recursive completion.
    with forbidden(monkeypatch):
        assert kb.complete_task(board, tid, summary='finished review', fire_lifecycle_hook=False)
        assert kb.complete_task(board, kid, summary='finished child', fire_lifecycle_hook=False)
        for _ in range(2):
            ws._cleanup_workspace(board, tid)
            ws._try_cleanup_parent_workspaces(board, kid)
            ws._cleanup_worktree_workspace(tid, str(root), 'wt/fixture', conn=board)
            with kb.write_txn(board):
                board.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
            assert ops._cmd_gc(Namespace(event_retention_days=30, log_retention_days=30)) == 0
        assert bp.preservation_gate(tid, root, conn=board) is False
    assert '0 workspace(s), 0 event row(s), 0 log file(s) removed' in capsys.readouterr().out
    assert root.stat().st_ino == original_inode
    assert {p: p.read_bytes() for p in before_files} == before_files
    assert git(repo, 'rev-parse', 'wt/fixture') == oid
    assert git(repo, 'worktree', 'list', '--porcelain') == registration
    assert kb.list_attachments(board, tid) == attachments
    assert {p: Path(p).read_bytes() for p in artifact_bytes} == artifact_bytes
    assert board.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?', (tid, kid)).fetchone()
    assert kb.get_task(board, kid).status == 'done'


@pytest.mark.parametrize('status', ['archived', 'done', 'running', 'ready'])
def test_task_and_attachment_apis_retain_lineage(board, tmp_path, monkeypatch, status):
    tid = task(board, tmp_path / 'source', status=status)
    attachment = tmp_path / 'accepted-bundle-retained.tgz'
    attachment.write_bytes(b'protected accepted bytes')
    aid = kb.add_attachment(board, tid, filename=attachment.name, stored_path=str(attachment),
                            uploaded_by='workspace_preservation')
    kb.add_comment(board, tid, author='fixture', body='audit evidence')
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET worker_pid=987654,claim_lock=? WHERE id=?', ('fixture-live-claim', tid))
        board.execute("INSERT INTO task_runs(task_id,status,started_at,worker_pid) VALUES(?,'running',0,987654)", (tid,))
    tables = ('tasks', 'task_comments', 'task_runs', 'task_events', 'task_attachments', 'task_links')
    before = {t: [tuple(row) for row in board.execute('SELECT * FROM '+t)] for t in tables}
    with forbidden(monkeypatch):
        for _ in range(2):
            for deleter in (kb.delete_task, kb.delete_archived_task, kb._delete_task_relations):
                with pytest.raises(RetentionRefused, match='disabled'):
                    deleter(board, tid)
            with pytest.raises(RetentionRefused):
                kb.delete_attachment(board, aid)
        assert kb.delete_task(board, 'missing') is False
        assert kb.delete_archived_task(board, 'missing') is False
        assert kb.delete_attachment(board, -1) is None
    assert {t: [tuple(row) for row in board.execute('SELECT * FROM '+t)] for t in tables} == before
    assert attachment.read_bytes() == b'protected accepted bytes'


@pytest.mark.parametrize('age', [0, 86400, 10**12])
def test_gc_retains_events_logs_and_validates_age(board, tmp_path, age):
    tid = task(board, tmp_path / 'source', status='done')
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('handoff recovery evidence')
    os.utime(log, (1, 1))
    kb.add_comment(board, tid, author='fixture', body='history')
    with kb.write_txn(board):
        board.execute('UPDATE task_events SET created_at=0')
    events = kb.list_events(board, tid)
    assert kb.gc_events(board, older_than_seconds=age) == 0
    assert kb.gc_worker_logs(older_than_seconds=age) == 0
    assert log.read_text() == 'handoff recovery evidence'
    assert kb.list_events(board, tid) == events
    with pytest.raises(ValueError):
        kb.gc_events(board, older_than_seconds=-1)
    with pytest.raises(ValueError):
        kb.gc_worker_logs(older_than_seconds=-1)


@pytest.mark.parametrize('form', ['absent', 'regular', 'directory', 'link', 'broken', 'unreadable', 'oversized'])
def test_marker_taxonomy_before_selection(board, tmp_path, monkeypatch, form):
    root = tmp_path / 'workspace'
    tid = task(board, root, status='done', result='accepted.patch')
    marker = root / 'KEEP-BUNDLE'
    if form == 'regular':
        marker.write_text('presence only; content must never be read')
    elif form == 'directory':
        marker.mkdir()
    elif form in ('link', 'broken'):
        marker.symlink_to(root / ('accepted.patch' if form == 'link' else 'missing'))
    elif form == 'unreadable':
        marker.touch(); marker.chmod(0)
    elif form == 'oversized':
        marker.write_bytes(b'x' * 4097)
    real_read = os.read
    reads = []
    def observed_read(fd, size):
        reads.append(size)
        return real_read(fd, size)
    with monkeypatch.context() as m:
        m.setattr(os, 'read', observed_read)
        result = inspect_keep_bundle(root)
    assert reads == ([4097] if form == 'regular' else [])
    if form in ('absent', 'regular'):
        expected = MarkerStatus.ABSENT if form == 'absent' else MarkerStatus.PRESENT
        assert result.status is expected
        assert bp.plan_bundle(board, tid, root)['keep_bundle'] is (form == 'regular')
    else:
        assert result.status is MarkerStatus.UNSAFE
        with pytest.raises(RetainAll):
            bp.preserve_bundle(board, tid, root)
    with forbidden(monkeypatch):
        ws._cleanup_workspace(board, tid)
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('special', ['fifo', 'socket'])
def test_marker_special_never_opened(board, tmp_path, monkeypatch, special):
    root = tmp_path / 'workspace'
    tid = task(board, root, status='done', result='accepted.patch')
    marker = root / 'KEEP-BUNDLE'
    sock = None
    if special == 'fifo':
        os.mkfifo(marker)
    else:
        sock = socket.socket(socket.AF_UNIX)
        # AF_UNIX limits the supplied name, even when the directory itself is
        # valid. Bind relative to the disposable workspace on macOS.
        monkeypatch.chdir(root)
        try:
            sock.bind('KEEP-BUNDLE')
        except PermissionError:
            sock.close()
            pytest.skip('Native AF_UNIX bind denied by local sandbox; injected socket taxonomy is separate')
    real_open = os.open
    def guarded_open(path, flags, *args, **kwargs):
        assert str(path) != 'KEEP-BUNDLE'
        return real_open(path, flags, *args, **kwargs)
    try:
        with monkeypatch.context() as m:
            m.setattr(os, 'open', guarded_open)
            assert inspect_keep_bundle(root).status is MarkerStatus.UNSAFE
            with pytest.raises(RetainAll):
                bp.preserve_bundle(board, tid, root)
    finally:
        if sock:
            sock.close()


@pytest.mark.parametrize('phase', ['stat', 'open', 'parent'])
@pytest.mark.parametrize('error', [PermissionError(13, 'denied'), OSError(5, 'I/O'), OSError(1, 'denied')])
def test_marker_inspection_errors_retain_all(board, tmp_path, monkeypatch, phase, error):
    root = tmp_path / 'workspace'
    tid = task(board, root, status='done', result='accepted.patch')
    (root / 'KEEP-BUNDLE').touch()
    real_stat, real_open = os.stat, os.open
    def bad_stat(path, *args, **kwargs):
        if phase == 'stat' and str(path) == 'KEEP-BUNDLE':
            raise error
        return real_stat(path, *args, **kwargs)
    def bad_open(path, *args, **kwargs):
        if (phase == 'open' and str(path) == 'KEEP-BUNDLE') or (phase == 'parent' and str(path) == 'workspace'):
            raise error
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', bad_stat)
    monkeypatch.setattr(os, 'open', bad_open)
    assert inspect_keep_bundle(root).status is MarkerStatus.UNSAFE
    with pytest.raises(RetainAll):
        bp.preserve_bundle(board, tid, root)
    assert kb.list_attachments(board, tid) == []


def test_unknown_marker_status_and_outer_transaction_refuse(board, tmp_path, monkeypatch):
    import hermes_cli.retention_markers as markers
    root = tmp_path / 'workspace'
    tid = task(board, root, status='done', result='accepted.patch')
    monkeypatch.setattr(markers, 'inspect_keep_bundle', lambda *a, **kw: markers.MarkerInspection('unknown', 'unknown enum'))
    with pytest.raises(RetainAll):
        bp.preserve_bundle(board, tid, root)
    board.execute('BEGIN')
    try:
        with pytest.raises(ValueError, match='own commit'):
            bp.preserve_bundle(board, tid, root)
        assert kb.list_attachments(board, tid) == []
    finally:
        board.rollback()


def test_lower_reapers_and_cli_exit_keep_real_git(board, tree, tmp_path, monkeypatch):
    repo, path = tree
    task(board, path, kind='worktree', status='done')
    import cli
    assert Path(cli.__file__).resolve().is_relative_to(ROOT)
    before = git(repo, 'worktree', 'list', '--porcelain')
    oid = git(repo, 'rev-parse', 'wt/fixture')
    rec = gc.TreeRecord(path.name, str(path), 'wt/fixture', 100000, 1, 'reap', 'stale', ['notes.ignored'])
    with forbidden(monkeypatch):
        for _ in range(2):
            cli._cleanup_worktree({'path': str(path), 'branch': 'wt/fixture', 'repo_root': str(repo)})
            wt._cleanup_failed_worktree_add(str(repo), path, 'wt/fixture')
            assert gc.reclaim_worktrees(str(repo), records=[rec]) == [f'retained {path.name} (destructive_authority_off)']
            assert gc.reclaim_branches(str(repo), records=[gc.BranchRecord('wt/fixture', 'delete', 'merged')]) == ['retained branch wt/fixture (destructive_authority_off)']
            wt._reap_prune_verdicts(str(repo), [(path, 0, True, 'reap', 'dead')], 100000)
            wt._prune_orphaned_branches(str(repo))
            wt._reclaim_orphan_install_states()
            wt._prune_stale_worktrees(str(repo), max_age_hours=-999)
            wt._prune_stale_worktrees(str(tmp_path / 'no-trees'))
    assert git(repo, 'worktree', 'list', '--porcelain') == before
    assert git(repo, 'rev-parse', 'wt/fixture') == oid
    assert (path / 'accepted.patch').read_bytes() == b'accepted bytes\n'


def test_subagent_payload_reports_retained_not_pruned(board, tree):
    repo, path = tree
    info = {'path': str(path), 'branch': 'wt/fixture', 'repo_root': str(repo),
            'base_commit': git(repo, 'rev-parse', 'HEAD')}
    payload = child.finalize_subagent_worktree(info)
    assert payload['pruned'] is False
    assert payload['retained'] is True
    assert payload['commits'] == 0
    assert payload['dirty'] is False
    assert path.is_dir()
    assert child.finalize_subagent_worktree({**info, 'path': str(repo / 'missing')})['pruned'] is False


def test_http_and_cli_refuse_existing_and_keep_not_found(board, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    plugin_path = ROOT / 'plugins/kanban/dashboard/plugin_api.py'
    spec = importlib.util.spec_from_file_location('stage3_retention_plugin', plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    app = FastAPI(); app.include_router(plugin.router)
    tid = task(board, tmp_path / 'workspace', status='archived')
    blob = tmp_path / 'ordinary.txt'; blob.write_bytes(b'bytes')
    aid = kb.add_attachment(board, tid, filename=blob.name, stored_path=str(blob))
    with TestClient(app) as client:
        for route in (f'/tasks/{tid}', f'/attachments/{aid}'):
            for _ in range(2):
                response = client.delete(route)
                assert response.status_code == 409
                assert response.json() == {'detail': 'Retained by default: destructive authority is disabled. Preservation does not authorize removal.',
                                           'reason': 'destructive_authority_off', 'retained': True, 'removed': False}
        assert client.delete('/tasks/missing').status_code == 404
        assert client.delete('/attachments/999999').status_code == 404
    from hermes_cli import kanban
    assert kanban._cmd_archive(Namespace(task_ids=[], purge_ids=[tid])) != 0
    assert kanban._cmd_attach_rm(Namespace(attachment_id=aid)) != 0
    assert kb.get_task(board, tid).status == 'archived'
    assert kb.get_attachment(board, aid) is not None
    assert blob.read_bytes() == b'bytes'


def test_registered_selected_bundle_never_authorizes_cleanup(board, tmp_path):
    root = tmp_path / 'source'
    tid = task(board, root, status='done', result='accepted.patch')
    proof = bp.preserve_bundle(board, tid, root)
    assert len(proof['attachments']) == 2
    assert bp.preservation_gate(tid, root, conn=board) is False
    before = {p: Path(p).read_bytes() for p in proof['attachments']}
    for deleter in (kb.delete_task, kb.delete_archived_task):
        with pytest.raises(RetentionRefused):
            deleter(board, tid)
    assert {p: Path(p).read_bytes() for p in before} == before
    assert len(kb.list_attachments(board, tid)) == 2
    assert root.is_dir()


@pytest.mark.parametrize('filename,stored_name,uploader', [
    ('renamed.txt', 'ordinary.bin', 'workspace_preservation'),
    ('accepted-bundle-pretend.tgz', 'ordinary.bin', ''),
    ('ordinary.txt', 'accepted-bundle-pretend.tgz', 'forged'),
    ('ordinary.txt', 'ordinary.bin', 'normal'),
])
def test_attachment_refusal_does_not_trust_mutable_labels(board, tmp_path, monkeypatch, filename, stored_name, uploader):
    tid = task(board, tmp_path / 'source', status='done')
    blob = tmp_path / stored_name
    blob.write_bytes(b'immutable retained payload')
    aid = kb.add_attachment(board, tid, filename=filename, stored_path=str(blob), uploaded_by=uploader)
    before = kb.get_attachment(board, aid)
    with forbidden(monkeypatch):
        with pytest.raises(RetentionRefused):
            kb.delete_attachment(board, aid)
    assert kb.get_attachment(board, aid) == before
    assert blob.read_bytes() == b'immutable retained payload'


@pytest.mark.parametrize('relationship', ['equal', 'ancestor', 'descendant'])
def test_overlapping_workspace_users_and_holds_survive(board, tmp_path, monkeypatch, relationship):
    root = kb.workspaces_root() / 'shared'
    tid = task(board, root, status='archived', result='accepted.patch')
    other = root if relationship == 'equal' else root.parent if relationship == 'ancestor' else root / 'nested'
    sharer = task(board, other, kind='dir')
    (root / 'HOLD-WORKSPACE').mkdir()
    with forbidden(monkeypatch):
        ws._cleanup_workspace(board, tid)
        assert ops._cmd_gc(Namespace(event_retention_days=0, log_retention_days=0)) == 0
    assert root.is_dir() and (root / 'HOLD-WORKSPACE').is_dir()
    assert kb.get_task(board, sharer).status == 'ready'


def test_preservation_paused_while_hard_delete_refuses(board, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    root = kb.workspaces_root() / 'race-fixture'
    tid = task(board, root, status='done', result='accepted.patch')
    entered, resume = threading.Event(), threading.Event()
    real_plan = bp.plan_bundle
    def paused_plan(*args, **kwargs):
        report = real_plan(*args, **kwargs)
        entered.set()
        assert resume.wait(10)
        return report
    monkeypatch.setattr(bp, 'plan_bundle', paused_plan)
    def preserver():
        with kbc.connect_closing() as conn:
            return bp.preserve_bundle(conn, tid, root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(preserver)
        try:
            assert entered.wait(10)
            assert not board.in_transaction
            with forbidden(monkeypatch):
                with pytest.raises(RetentionRefused):
                    kb.delete_task(board, tid)
                ws._cleanup_workspace(board, tid)
            assert root.is_dir()
        finally:
            resume.set()
        proof = future.result(timeout=10)
    assert len(proof['attachments']) == 2
    assert kb.get_task(board, tid) is not None
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'
