"""Stage3 follow-up: real local preservation, active log open and explicit soft hides."""
from concurrent.futures import ThreadPoolExecutor
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
from unittest.mock import Mock

import pytest

from tests.hermes_cli import test_retention_runtime as fixtures
from tests.hermes_cli.test_retention_runtime import task
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_bundle_preserve as bp
from hermes_cli import retention_bundle as bundles, retention_store as store
from hermes_cli import retention_tombstones as hides
from hermes_cli.retention import RetentionRefused

board = fixtures.board


@pytest.mark.parametrize('backups', [0, 1, 3])
def test_actual_worker_log_open_retains_every_generation(board, tmp_path, monkeypatch, backups):
    tid = task(board, tmp_path / 'source')
    logdir = kb.worker_logs_dir(); logdir.mkdir(parents=True, exist_ok=True)
    original = {logdir / f'{tid}.log{suffix}': b'original log '+suffix.encode()
                for suffix in ('', '.1', '.2', '.3')}
    for path, data in original.items():
        path.write_bytes(data)
    monkeypatch.setattr(dispatch, 'worker_log_rotation_config', lambda: (1, backups))
    with monkeypatch.context() as m:
        spies = []
        for module, name in ((os, 'unlink'), (os, 'rename'), (os, 'replace'), (Path, 'unlink'), (Path, 'rename'), (shutil, 'rmtree')):
            spy = Mock(side_effect=AssertionError('rotation mutation '+name))
            m.setattr(module, name, spy); spies.append(spy)
        for _ in range(2):
            with dispatch._open_worker_log(kb.get_task(board, tid), None) as output:
                output.write(b'new append\n')
        for spy in spies:
            spy.assert_not_called()
    for path, data in original.items():
        assert path.read_bytes() == data + (b'new append\n'*2 if path.suffix == '.log' else b'')


@pytest.mark.parametrize('replacement', ['temp-leaf', 'destination-ancestor'])
def test_bundle_substitution_never_reopens_or_truncates_outside(board, tmp_path, monkeypatch, replacement):
    root = tmp_path / 'source'; tid = task(board, root, status='done', result='accepted.patch')
    destination = store.attachment_destination(board, tid)
    external = tmp_path / 'outside'; external.mkdir()
    sentinel = external / 'useful.txt'; sentinel.write_bytes(b'outside original bytes')
    entered, resume = threading.Event(), threading.Event()
    original = bundles._exclusive_file
    observed = {}
    def paused(parent, name):
        stream = original(parent, name)
        if name.endswith('.archive'):
            observed['name'] = name
            entered.set()
            assert resume.wait(10)
        return stream
    monkeypatch.setattr(bundles, '_exclusive_file', paused)
    def preserve():
        with kbc.connect_closing() as conn:
            return bp.preserve_bundle(conn, tid, root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(preserve)
        spies = []
        try:
            assert entered.wait(10)
            if replacement == 'temp-leaf':
                leaf = destination / observed['name']
                leaf.rename(leaf.with_name(leaf.name+'.retained'))
                leaf.symlink_to(sentinel)
            else:
                destination.rename(destination.with_name(destination.name+'.retained'))
                destination.symlink_to(external, target_is_directory=True)
            for module, name in ((os, 'unlink'), (os, 'rename'), (os, 'replace'), (shutil, 'rmtree')):
                spy = Mock(side_effect=AssertionError('failed bundle cleanup '+name))
                monkeypatch.setattr(module, name, spy); spies.append(spy)
        finally:
            resume.set()
        with pytest.raises((ValueError, OSError)):
            future.result(timeout=10)
        for spy in spies:
            spy.assert_not_called()
    assert sentinel.read_bytes() == b'outside original bytes'
    assert sorted(p.name for p in external.iterdir()) == ['useful.txt']
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'
    assert kb.list_attachments(board, tid) == []
    assert list((destination.parent / '.retention/bundles').glob('*.prepared.json'))


@pytest.mark.parametrize('fault', ['checksum-publish', 'registration'])
def test_bundle_failure_retains_operation_and_retry_succeeds(board, tmp_path, monkeypatch, fault):
    root = tmp_path / 'source'; tid = task(board, root, status='done', result='accepted.patch')
    destination = store.attachment_destination(board, tid)
    with monkeypatch.context() as m:
        if fault == 'checksum-publish':
            original = os.link
            def fail(source, target, **kwargs):
                if str(target).endswith('.sha256.json'):
                    raise OSError(errno.EIO, 'injected checksum publish')
                return original(source, target, **kwargs)
            m.setattr(os, 'link', fail)
        else:
            board.execute("CREATE TRIGGER reject_bundle BEFORE INSERT ON task_attachments BEGIN SELECT RAISE(ABORT,'injected registration'); END")
        try:
            with pytest.raises((OSError, sqlite3.Error)):
                bp.preserve_bundle(board, tid, root)
        finally:
            if fault == 'registration':
                board.execute('DROP TRIGGER reject_bundle')
    retained = {p: p.read_bytes() for p in destination.iterdir() if p.is_file()}
    assert retained
    assert kb.list_attachments(board, tid) == []
    journal = destination.parent / '.retention/bundles'
    prepared = list(journal.glob('*.prepared.json')); assert len(prepared) == 1
    result = bp.preserve_bundle(board, tid, root)
    assert len(result['attachments']) == 2
    assert {p: p.read_bytes() for p in retained} == retained
    with kbc.connect_closing() as fresh:
        assert len(kb.list_attachments(fresh, tid)) == 2
        assert bp.preserve_bundle(fresh, tid, root)['attachments'] == result['attachments']
        assert len(kb.list_attachments(fresh, tid)) == 2
    assert len(list(journal.glob('*.prepared.json'))) == 2
    assert len(list(journal.glob('*.registered.json'))) == 1
    if fault == 'registration':
        assert len(list(journal.glob('*.blobs_durable.json'))) == 2
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'


@pytest.mark.parametrize('fault', [False, True])
def test_bundle_syncs_created_parents_before_registration(board, tmp_path, monkeypatch, fault):
    root = tmp_path / 'source'; tid = task(board, root, status='done', result='accepted.patch')
    events = []
    real_mkdir, real_sync = os.mkdir, os.fsync
    def mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        if kwargs.get('dir_fd') is not None:
            events.append(('mkdir', kwargs['dir_fd']))
        return result
    def sync(fd):
        assert not board.in_transaction
        if events and events[-1] == ('mkdir', fd):
            events.append(('parent_sync', fd))
            if fault:
                raise OSError(errno.EIO, 'injected parent fsync')
        return real_sync(fd)
    monkeypatch.setattr(os, 'mkdir', mkdir)
    monkeypatch.setattr(os, 'fsync', sync)
    if fault:
        with pytest.raises(OSError, match='parent fsync'):
            bp.preserve_bundle(board, tid, root)
        assert kb.list_attachments(board, tid) == []
    else:
        bp.preserve_bundle(board, tid, root)
        assert events and all(events[i+1][0] == 'parent_sync' for i,e in enumerate(events) if e[0] == 'mkdir')
        with kbc.connect_closing() as fresh:
            assert len(kb.list_attachments(fresh, tid)) == 2
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'


def test_regular_keep_read_eio_blocks_direct_planning_and_publication(board, tmp_path, monkeypatch):
    root = tmp_path / 'source'; tid = task(board, root, status='done', result='accepted.patch')
    (root / 'KEEP-BUNDLE').write_text('readable regular marker')
    real_open, real_read = os.open, os.read
    marker_fds = set()
    def opened(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path) == 'KEEP-BUNDLE': marker_fds.add(fd)
        return fd
    def read(fd, size):
        if fd in marker_fds: raise OSError(errno.EIO, 'injected regular read EIO')
        return real_read(fd, size)
    monkeypatch.setattr(os, 'open', opened); monkeypatch.setattr(os, 'read', read)
    for operation in (bp.plan_bundle, bp.preserve_bundle):
        with pytest.raises(ValueError, match='keep marker probe failed'):
            operation(board, tid, root)
    assert kb.list_attachments(board, tid) == []
    assert not store.attachment_destination(board, tid).exists()


def test_explicit_soft_hides_are_idempotent_visible_by_identity_and_audited(board, tmp_path):
    root = tmp_path / 'source'; tid = task(board, root, status='archived')
    aid = kb.store_attachment_bytes(board, tid, 'ordinary.txt', b'ordinary retained')
    before = kb.get_task(board, tid)
    attachment = kb.get_attachment(board, aid)
    args = {'actor': 'fixture operator', 'reason': 'hide completed fixture'}
    first = hides.hide_attachment(board, aid, **args)
    assert first == hides.hide_attachment(board, aid, **args)
    assert first['tombstoned'] is True and first['removed'] is False
    assert kb.list_attachments(board, tid) == []
    assert kb.list_attachments(board, tid, include_hidden=True) == [attachment]
    assert kb.get_attachment(board, aid) == attachment
    hidden = hides.hide_task(board, tid, **args)
    assert hidden == hides.hide_task(board, tid, **args)
    assert kb.list_tasks(board, include_archived=True) == []
    assert kb.list_tasks(board, include_archived=True, include_hidden=True) == [before]
    assert kb.get_task(board, tid) == before
    assert Path(attachment.stored_path).read_bytes() == b'ordinary retained'
    assert board.execute('SELECT COUNT(*) FROM retention_audit').fetchone()[0] == 2
    assert hides.hide_task(board, 'missing', **args) is False
    assert hides.hide_attachment(board, -1, **args) is None
    for delete in (kb.delete_task, kb.delete_archived_task):
        with pytest.raises(RetentionRefused): delete(board, tid)
    with pytest.raises(RetentionRefused): kb.delete_attachment(board, aid)
    # Retained audit tables intentionally have no cascading task foreign key.
    with kb.write_txn(board): board.execute('DELETE FROM tasks WHERE id=?', (tid,))
    assert board.execute('SELECT COUNT(*) FROM retention_audit').fetchone()[0] == 2
    assert board.execute('SELECT COUNT(*) FROM retention_tombstones').fetchone()[0] == 2


@pytest.mark.parametrize('state', ['ready', 'running', 'claim', 'worker', 'run'])
def test_soft_task_hide_denies_active_or_uncertain_workers(board, tmp_path, state):
    tid = task(board, tmp_path / 'source', status=state if state in {'ready','running'} else 'archived')
    with kb.write_txn(board):
        if state == 'claim': board.execute("UPDATE tasks SET claim_lock='held' WHERE id=?", (tid,))
        if state == 'worker': board.execute('UPDATE tasks SET worker_pid=987654 WHERE id=?', (tid,))
        if state == 'run': board.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES(?,'running',0)", (tid,))
    before = kb.get_task(board, tid)
    with pytest.raises(RetentionRefused): hides.hide_task(board, tid, actor='fixture', reason='request')
    assert kb.get_task(board, tid) == before
    assert board.execute('SELECT COUNT(*) FROM retention_tombstones').fetchone()[0] == 0


@pytest.mark.parametrize('filename,uploader', [('renamed.txt','workspace_preservation'),('accepted-bundle-legacy.tgz','forged')])
def test_protected_proof_cannot_be_soft_hidden(board, tmp_path, filename, uploader):
    tid = task(board, tmp_path / 'source', status='archived')
    aid = kb.store_attachment_bytes(board, tid, filename, b'protected proof', uploaded_by=uploader)
    original = kb.get_attachment(board, aid)
    with pytest.raises(RetentionRefused, match='protected_attachment'):
        hides.hide_attachment(board, aid, actor='fixture', reason='request')
    assert kb.list_attachments(board, tid) == [original]
    assert Path(original.stored_path).read_bytes() == b'protected proof'


def test_relabelled_registered_proof_cannot_be_hidden(board, tmp_path):
    root = tmp_path / 'source'; tid = task(board, root, status='archived', result='accepted.patch')
    report = bp.preserve_bundle(board, tid, root)
    original = Path(report['attachments'][0])
    # Fixture adversary removes its temp alias and relabels the accepted inode.
    journal = next((original.parent.parent / '.retention/bundles').glob('*.registered.json'))
    record = json.loads(journal.read_text())
    (original.parent / record['temporary']['archive']).unlink()
    replacement = original.with_name('renamed-ordinary.bin')
    original.rename(replacement)
    with kb.write_txn(board):
        board.execute("UPDATE task_attachments SET filename='ordinary.bin',uploaded_by='normal',stored_path=? WHERE stored_path=?", (str(replacement), str(original)))
    attachment = next(a for a in kb.list_attachments(board, tid) if a.stored_path == str(replacement))
    with pytest.raises(RetentionRefused, match='protected_attachment'):
        hides.hide_attachment(board, attachment.id, actor='fixture', reason='request')
    assert replacement.read_bytes()
    assert kb.get_attachment(board, attachment.id) == attachment


def test_soft_hide_http_routes_keep_identity_download_and_physical_refusal(board, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    root = tmp_path / 'source'; tid = task(board, root, status='archived')
    aid = kb.store_attachment_bytes(board, tid, 'ordinary.txt', b'identity download')
    plugin_path = fixtures.ROOT / 'plugins/kanban/dashboard/plugin_api.py'
    spec = importlib.util.spec_from_file_location('stage3_followup_api', plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin; spec.loader.exec_module(plugin)
    app = FastAPI(); app.include_router(plugin.router)
    with TestClient(app) as client:
        body = {'actor': 'fixture operator', 'reason': 'explicit soft hide'}
        for route in (f'/attachments/{aid}/hide', f'/tasks/{tid}/hide'):
            response = client.post(route, json=body)
            assert response.status_code == 200, response.text
            assert response.json()['tombstoned'] is True and response.json()['removed'] is False
            assert client.post(route, json=body).json() == response.json()
        assert client.get(f'/tasks/{tid}').status_code == 200
        assert client.get(f'/attachments/{aid}').content == b'identity download'
        assert client.get(f'/tasks/{tid}/attachments').json()['attachments'] == []
        assert client.get(f'/tasks/{tid}/attachments?include_hidden=true').json()['attachments'][0]['id'] == aid
        default = client.get('/board?include_archived=true').json()
        included = client.get('/board?include_archived=true&include_hidden=true').json()
        assert all(t['id'] != tid for c in default['columns'] for t in c['tasks'])
        assert any(t['id'] == tid for c in included['columns'] for t in c['tasks'])
        assert client.delete(f'/tasks/{tid}').status_code == 409
        assert client.delete(f'/attachments/{aid}').status_code == 409
        assert client.post('/tasks/missing/hide', json=body).status_code == 404
        assert client.post('/attachments/999999/hide', json=body).status_code == 404


def test_reopened_task_and_attachment_return_to_default_views(board, tmp_path):
    tid = task(board, tmp_path / 'source', status='archived')
    aid = kb.store_attachment_bytes(board, tid, 'ordinary.txt', b'reopened retained bytes')
    args = {'actor': 'fixture', 'reason': 'explicit hide'}
    hides.hide_attachment(board, aid, **args)
    hides.hide_task(board, tid, **args)
    with kb.write_txn(board): board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert [t.id for t in kb.list_tasks(board)] == [tid]
    assert [a.id for a in kb.list_attachments(board, tid)] == [aid]
    with pytest.raises(RetentionRefused): hides.hide_task(board, tid, **args)


@pytest.mark.parametrize('operation', ['task', 'attachment'])
def test_soft_hide_rejects_outer_transactions_and_rolls_back_audit_failure(board, tmp_path, operation):
    tid = task(board, tmp_path / 'source', status='archived')
    aid = kb.store_attachment_bytes(board, tid, 'ordinary.txt', b'original bytes')
    hide, identity = (hides.hide_task, tid) if operation == 'task' else (hides.hide_attachment, aid)
    args = {'actor': 'fixture', 'reason': 'explicit hide'}
    board.execute('BEGIN')
    try:
        with pytest.raises(ValueError, match='own commit'): hide(board, identity, **args)
    finally:
        board.rollback()
    board.execute("CREATE TRIGGER reject_hide BEFORE INSERT ON retention_audit BEGIN SELECT RAISE(ABORT,'injected audit'); END")
    with pytest.raises(sqlite3.Error, match='injected audit'): hide(board, identity, **args)
    assert board.execute('SELECT COUNT(*) FROM retention_tombstones').fetchone()[0] == 0
    assert kb.get_task(board, tid).status == 'archived'
    assert len(kb.list_attachments(board, tid)) == 1
    assert Path(kb.get_attachment(board, aid).stored_path).read_bytes() == b'original bytes'
