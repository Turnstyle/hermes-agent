"""Adversarial OFF-mode upload/soft-hide regressions from parent self-review."""
from pathlib import Path
import os

import pytest

from tests.hermes_cli import test_retention_runtime as fixtures
from tests.hermes_cli.test_retention_runtime import task
from hermes_cli import kanban_db as kb, retention_store as store, retention_tombstones as hides
from hermes_cli.retention import RetentionRefused

board = fixtures.board


@pytest.mark.parametrize('substitution', ['destination', 'published-leaf'])
def test_upload_replacement_is_not_registered(board, tmp_path, monkeypatch, substitution):
    tid = task(board, tmp_path / 'source', status='archived')
    destination = store.attachment_destination(board, tid)
    payload = b'expected retained payload'
    real_write, real_link = store._write_exclusive, os.link
    replaced = False

    def replace_destination():
        nonlocal replaced
        if substitution == 'destination' and not replaced:
            replaced = True
            destination.rename(destination.with_name(destination.name + '.retained'))
            destination.mkdir()
            (destination / 'report.txt').write_bytes(b'x' * len(payload))

    def write(parent, name, data):
        result = real_write(parent, name, data)
        if name.startswith('.attachment-'):
            replace_destination()
        elif name == 'report.txt' and substitution == 'published-leaf':
            leaf = destination / name
            leaf.rename(leaf.with_name('original-retained.txt'))
            leaf.write_bytes(b'x' * len(payload))
        return result

    def link(source, target, **kwargs):
        result = real_link(source, target, **kwargs)
        if substitution == 'published-leaf' and target == 'report.txt':
            leaf = destination / target
            leaf.rename(leaf.with_name('original-retained.txt'))
            leaf.write_bytes(b'x' * len(payload))
        return result

    monkeypatch.setattr(store, '_write_exclusive', write)
    monkeypatch.setattr(os, 'link', link)
    with pytest.raises(ValueError, match='identity|digest'):
        kb.store_attachment_bytes(board, tid, 'report.txt', payload)
    assert kb.list_attachments(board, tid) == []
    assert (destination / 'report.txt').read_bytes() == b'x' * len(payload)
    assert (tmp_path / 'source/accepted.patch').read_bytes() == b'accepted bytes\n'


def test_successful_upload_never_unlinks_substituted_temporary(board, tmp_path, monkeypatch):
    tid = task(board, tmp_path / 'source', status='archived')
    destination = store.attachment_destination(board, tid)
    real_write = store._write_exclusive
    temporary = None

    def write(parent, name, data):
        nonlocal temporary
        result = real_write(parent, name, data)
        if name.startswith('.attachment-'):
            temporary = name
        if name.endswith('.registered.json'):
            leaf = destination / temporary
            leaf.rename(leaf.with_name(leaf.name + '.original'))
            leaf.write_bytes(b'unrelated replacement')
        return result

    monkeypatch.setattr(store, '_write_exclusive', write)
    aid = kb.store_attachment_bytes(board, tid, 'report.txt', b'original upload')
    assert Path(kb.get_attachment(board, aid).stored_path).read_bytes() == b'original upload'
    assert (destination / temporary).read_bytes() == b'unrelated replacement'
    assert (destination / (temporary + '.original')).read_bytes() == b'original upload'


@pytest.mark.parametrize('uncertainty', ['worker', 'claim', 'run', 'dangling-run'])
def test_soft_hides_refuse_terminal_dependent_with_uncertain_worker(board, tmp_path, uncertainty):
    parent = task(board, tmp_path / 'parent', status='archived')
    child = task(board, tmp_path / 'child', status='archived')
    aid = kb.store_attachment_bytes(board, parent, 'ordinary.txt', b'ordinary retained')
    with kb.write_txn(board):
        board.execute('INSERT INTO task_links(parent_id,child_id) VALUES(?,?)', (parent, child))
        if uncertainty == 'worker':
            board.execute('UPDATE tasks SET worker_pid=987654 WHERE id=?', (child,))
        elif uncertainty == 'claim':
            board.execute("UPDATE tasks SET claim_lock='held' WHERE id=?", (child,))
        elif uncertainty == 'run':
            board.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES(?,'running',0)", (child,))
        else:
            board.execute('UPDATE tasks SET current_run_id=987654 WHERE id=?', (child,))
    for hide, identity in ((hides.hide_task, parent), (hides.hide_attachment, aid)):
        with pytest.raises(RetentionRefused):
            hide(board, identity, actor='fixture', reason='do not hide live dependencies')
    assert board.execute('SELECT COUNT(*) FROM retention_tombstones').fetchone()[0] == 0
    assert Path(kb.get_attachment(board, aid).stored_path).read_bytes() == b'ordinary retained'
