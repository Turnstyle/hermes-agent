"""Open parents stay binding despite old rehome and PR release evidence."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


@pytest.mark.parametrize('status', ['todo', 'scheduled', 'ready', 'running', 'review', 'triage', 'blocked'])
@pytest.mark.parametrize('event', ['rehomed', 'superseded', 'pr_acceptance'])
def test_open_parent_cannot_be_released_by_history(board, monkeypatch, status, event):
    parent = kb.create_task(board, title='Merge parent', completion_contract='https://github.com/example/project/pull/1')
    successor = kb.create_task(board, title='Successor')
    child = kb.create_task(board, title='Child', parents=[parent])
    board.execute('UPDATE tasks SET status = ? WHERE id = ?', (status, parent))
    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    kb._append_event(board, parent, event, {'successor_id': successor, 'pr_url': 'https://github.com/example/project/pull/1'})
    board.commit()
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='{"state":"MERGED","mergedAt":"2026-09-28T12:00:00Z"}',
    ))
    assert kb.complete_task(board, child, result='Must stay open') is False
    assert kb.get_task(board, child).status == 'ready'
    assert kb.get_task(board, parent).status == status


@pytest.mark.parametrize('terminal_status', ['done', 'archived'])
def test_every_parent_must_close_before_child_completion(board, terminal_status):
    first = kb.create_task(board, title='First parent')
    second = kb.create_task(board, title='Second parent')
    child = kb.create_task(board, title='Child', parents=[first, second])
    board.execute('UPDATE tasks SET status = ? WHERE id = ?', (terminal_status, first))
    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    board.commit()
    assert kb.complete_task(board, child, result='One parent remains') is False
    board.execute('UPDATE tasks SET status = ? WHERE id = ?', (terminal_status, second))
    board.commit()
    assert kb.complete_task(board, child, result='All parents closed') is True
    assert kb.get_task(board, child).status == 'done'
