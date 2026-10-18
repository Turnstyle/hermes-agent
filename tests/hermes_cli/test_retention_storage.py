"""Disposable storage publication, imported provenance, failures and native child crashes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import tarfile
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest
from tests.hermes_cli import test_retention_runtime as fixtures
from tests.hermes_cli.test_retention_runtime import task, ROOT
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_bundle_preserve as bp, retention_store as store
from hermes_cli.retention import RetentionRefused

board = fixtures.board


def test_same_name_concurrent_store_never_overwrites(board, tmp_path, monkeypatch):
    tid = task(board, tmp_path / 'source', status='done')
    barrier = threading.Barrier(2)
    real_write = store._write_exclusive
    def contested_write(parent, name, data):
        if name == 'report.txt':
            barrier.wait(timeout=10)
        return real_write(parent, name, data)
    monkeypatch.setattr(store, '_write_exclusive', contested_write)
    def writer(data):
        with kbc.connect_closing() as conn:
            return kb.store_attachment_bytes(conn, tid, 'report.txt', data)
    with ThreadPoolExecutor(max_workers=2) as workers:
        ids = list(workers.map(writer, [b'first payload', b'second payload']))
    assert len(set(ids)) == 2
    attachments = [kb.get_attachment(board, aid) for aid in ids]
    assert {Path(a.stored_path).read_bytes() for a in attachments} == {b'first payload', b'second payload'}
    assert len({a.stored_path for a in attachments}) == 2
    assert all(a.filename in {'report.txt', 'report (1).txt'} for a in attachments)
    with kbc.connect_closing() as reopened:
        assert all(kb.get_attachment(reopened, aid) is not None for aid in ids)


def test_row_insert_failure_retains_published_bytes_and_detached_intent(board, tmp_path):
    tid = task(board, tmp_path / 'source', status='done')
    board.execute("CREATE TRIGGER reject_attachment BEFORE INSERT ON task_attachments BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='fixture insert failure'):
        kb.store_attachment_bytes(board, tid, 'report.txt', b'recoverable bytes')
    destination = store.attachment_destination(board, tid)
    assert (destination / 'report.txt').read_bytes() == b'recoverable bytes'
    intents = list((destination.parent / '.retention').glob('*.published.json'))
    assert len(intents) == 1
    assert json.loads(intents[0].read_text())['sha256'] == hashlib.sha256(b'recoverable bytes').hexdigest()
    assert kb.list_attachments(board, tid) == []
    assert (tmp_path / 'source/accepted.patch').read_bytes() == b'accepted bytes\n'


@pytest.mark.parametrize('failure', ['file-sync', 'directory-sync'])
def test_storage_sync_failure_keeps_source_and_no_registration(board, tmp_path, monkeypatch, failure):
    import stat
    tid = task(board, tmp_path / 'source', status='done')
    destination = store.attachment_destination(board, tid)
    # Existing parents keep the failure focused on this operation's syncs.
    destination.mkdir(parents=True)
    (destination.parent / '.retention').mkdir()
    real_sync = os.fsync
    def fail_sync(fd):
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        if regular == (failure == 'file-sync'):
            raise OSError(5, 'fixture sync failure')
        return real_sync(fd)
    monkeypatch.setattr(os, 'fsync', fail_sync)
    with pytest.raises(OSError, match='fixture sync failure'):
        kb.store_attachment_bytes(board, tid, 'report.txt', b'original payload')
    assert kb.list_attachments(board, tid) == []
    assert (tmp_path / 'source/accepted.patch').read_bytes() == b'accepted bytes\n'
    assert list((destination.parent / '.retention').iterdir())


def test_existing_symlink_and_board_mismatch_do_not_touch_target(board, tmp_path):
    tid = task(board, tmp_path / 'source', status='done')
    target = tmp_path / 'outside'; target.write_bytes(b'outside bytes')
    destination = store.attachment_destination(board, tid)
    destination.mkdir(parents=True)
    (destination / 'report.txt').symlink_to(target)
    aid = kb.store_attachment_bytes(board, tid, 'report.txt', b'inside bytes')
    assert kb.get_attachment(board, aid).filename == 'report (1).txt'
    assert target.read_bytes() == b'outside bytes'
    with pytest.raises(ValueError, match='owner board mismatch'):
        kb.store_attachment_bytes(board, tid, 'report.txt', b'wrong board', board='other')
    board.execute('BEGIN')
    try:
        with pytest.raises(ValueError, match='own commit'):
            kb.store_attachment_bytes(board, tid, 'report.txt', b'outer transaction')
    finally:
        board.rollback()


def test_symlink_destination_ancestor_is_refused(board, tmp_path):
    tid = task(board, tmp_path / 'source', status='done')
    destination = store.attachment_destination(board, tid)
    outside = tmp_path / 'outside'; outside.mkdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        kb.store_attachment_bytes(board, tid, 'report.txt', b'never outside')
    assert list(outside.iterdir()) == []
    assert kb.list_attachments(board, tid) == []


def test_missing_import_preserves_provenance_and_source_is_not_local_proof(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_transfer as transfer
    tid = task(board, tmp_path / 'source', status='archived')
    aid = kb.add_attachment(board, tid, filename='accepted-bundle-proof.tgz',
                            stored_path='/old-host/accepted-bundle-proof.tgz', uploaded_by='workspace_preservation')
    root = tmp_path / 'imported-attachments'
    monkeypatch.setattr(kb, 'attachments_root', lambda *a, **kw: root)
    _, warnings = transfer._relocate_imported_rows(board, 'imported')
    imported = kb.get_attachment(board, aid)
    assert imported is not None and imported.uploaded_by == 'workspace_preservation'
    assert imported.stored_path == str(root / tid / 'accepted-bundle-proof.tgz')
    rows = board.execute("SELECT payload FROM task_events WHERE kind='attachment_missing_on_import'").fetchall()
    assert json.loads(rows[0]['payload'])['destination_local_proof'] is False
    assert 'missing_on_import' in warnings[0]
    with pytest.raises(RetentionRefused):
        kb.delete_attachment(board, aid)


def test_repeated_preservation_reuses_verified_bytes_and_retains_after_collision(board, tmp_path):
    root = tmp_path / 'source'
    tid = task(board, root, status='done', result='accepted.patch')
    first = bp.preserve_bundle(board, tid, root)
    bytes_before = {p: Path(p).read_bytes() for p in first['attachments']}
    second = bp.preserve_bundle(board, tid, root)
    assert second['attachments'] == first['attachments']
    assert {p: Path(p).read_bytes() for p in first['attachments']} == bytes_before
    archive = Path(first['attachments'][0]); archive.write_bytes(b'corrupted fixture')
    with pytest.raises((ValueError, tarfile.TarError)):
        bp.preserve_bundle(board, tid, root)
    assert archive.read_bytes() == b'corrupted fixture'
    assert (root / 'accepted.patch').read_bytes() == b'accepted bytes\n'


@pytest.mark.parametrize('boundary', ['prepared', 'temp', 'published', 'registered'])
def test_native_store_child_crash_reopen_retains_recovery(board, tmp_path, boundary):
    tid = task(board, tmp_path / 'source', status='done')
    db_file = board.execute('PRAGMA database_list').fetchone()[2]
    source = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, retention_store as store
assert Path(kb.__file__).resolve().is_relative_to(Path(sys.argv[1]))
real_write = store._write_exclusive
boundary = sys.argv[4]
def crash_write(parent, name, data):
    result = real_write(parent, name, data)
    crash = ((boundary == 'prepared' and name.endswith('.json') and '.published.' not in name and '.registered.' not in name)
             or (boundary == 'temp' and name.startswith('.attachment-'))
             or (boundary == 'published' and name.endswith('.published.json'))
             or (boundary == 'registered' and name.endswith('.registered.json')))
    if crash:
        os._exit(23)
    return result
store._write_exclusive = crash_write
with kbc.connect_closing() as conn:
    kb.store_attachment_bytes(conn, sys.argv[3], 'report.txt', b'child recovery payload')
raise AssertionError('crash boundary not reached')
'''
    env = {'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'HOME': str(tmp_path),
           'HERMES_HOME': str(Path(db_file).parent), 'HERMES_KANBAN_DB': db_file,
           'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.Popen([sys.executable, '-B', '-c', source, str(ROOT), db_file, tid, boundary],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, error = proc.communicate(timeout=20)
        assert proc.returncode == 23, output + error
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    assert (tmp_path / 'source/accepted.patch').read_bytes() == b'accepted bytes\n'
    destination = store.attachment_destination(board, tid)
    journals = list((destination.parent / '.retention').glob('*.json'))
    assert journals
    if boundary in {'published', 'registered'}:
        assert (destination / 'report.txt').read_bytes() == b'child recovery payload'
    with kbc.connect_closing() as reopened:
        attachments = kb.list_attachments(reopened, tid)
        assert bool(attachments) is (boundary == 'registered')
        if attachments:
            with pytest.raises(RetentionRefused):
                kb.delete_attachment(reopened, attachments[0].id)
    assert kb.gc_worker_logs(older_than_seconds=0) == 0


def test_store_sync_publication_precedes_short_row_transaction(board, tmp_path, monkeypatch):
    tid = task(board, tmp_path / 'source', status='done')
    events = []
    real_write, real_sync = store._write_exclusive, os.fsync
    def write(parent, name, data):
        assert not board.in_transaction, 'filesystem work inside SQLite transaction'
        events.append('write:' + ('registered' if '.registered.' in name else 'published' if '.published.' in name
                                  else 'temp' if name.startswith('.attachment-') else 'prepared'))
        if name == 'report.txt':
            events.append('publish')
        return real_write(parent, name, data)
    def sync(fd):
        import stat
        assert not board.in_transaction
        events.append('sync-file' if stat.S_ISREG(os.fstat(fd).st_mode) else 'sync-directory')
        return real_sync(fd)
    def trace(sql):
        if sql.startswith('BEGIN'):
            events.append('begin')
        elif sql == 'COMMIT':
            events.append('commit')
    monkeypatch.setattr(store, '_write_exclusive', write)
    monkeypatch.setattr(os, 'fsync', sync)
    board.set_trace_callback(trace)
    try:
        aid = kb.store_attachment_bytes(board, tid, 'report.txt', b'ordered bytes')
    finally:
        board.set_trace_callback(None)
    assert events.index('write:prepared') < events.index('write:temp') < events.index('publish')
    assert events.index('publish') < events.index('write:published') < events.index('begin')
    assert events.index('begin') < events.index('commit') < events.index('write:registered')
    assert 'sync-directory' in events[events.index('publish'):events.index('begin')]
    with kbc.connect_closing() as reopened:
        attachment = kb.get_attachment(reopened, aid)
        assert Path(attachment.stored_path).read_bytes() == b'ordered bytes'
