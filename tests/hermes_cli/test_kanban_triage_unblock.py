"""Triage hold release preserves the spec and requires an audited operator action."""

import argparse
from pathlib import Path
import time

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    kb.init_db()
    return home


def _cli(*args):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest='cmd'))
    return cli.kanban_command(parser.parse_args(['kanban', *args]))


def _snapshot(conn, tid):
    return tuple(
        [dict(row) for row in conn.execute(f'SELECT * FROM {table} WHERE {key} = ? ORDER BY id', (tid,))]
        for table, key in [('tasks', 'id'), ('task_comments', 'task_id'), ('task_events', 'task_id')]
    )


def _held_task(conn, kind, *, title='Reviewed spec', body='Keep these instructions'):
    tid = kb.create_task(conn, title=title, body=body, triage=True)
    stamp = int(time.time())
    conn.execute('UPDATE tasks SET created_at = ? WHERE id = ?', (stamp - 60, tid))
    conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'created'", (stamp - 60, tid))
    event_kind, payload = {
        'block_loop_detected': ('block_loop_detected', {'reason': 'recurrence limit'}),
        'sticky_block': ('blocked', {'reason': 'operator hold'}),
        'sticky_gave_up': ('gave_up', {'sticky': True}),
        'needs_input': ('blocked', {'reason': 'needs answer'}),
        'none': (None, None),
    }[kind]
    if kind == 'needs_input':
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (tid,))
    if event_kind:
        kb._append_event(conn, tid, event_kind, payload)
        conn.execute('UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = ?', (stamp, tid, event_kind))
    conn.commit()
    return tid


@pytest.mark.parametrize('kind', ['block_loop_detected', 'sticky_block', 'sticky_gave_up', 'needs_input'])
def test_triage_clear_then_keep_spec(kanban_home, kind, capsys):
    with kbc.connect() as conn:
        tid = _held_task(conn, kind)
        before = _snapshot(conn, tid)
    digest = kb.compute_task_sha256('Reviewed spec', 'Keep these instructions')
    assert not spec.keep_spec_task(tid, expect_sha256=digest, author='owner').ok
    assert _cli('unblock', tid) == 1
    assert 'requires a non-empty actor and reason' in capsys.readouterr().err
    with kbc.connect() as conn:
        assert _snapshot(conn, tid) == before
        for actor, reason in [(None, 'approved'), ('   ', 'approved'), ('owner', None), ('owner', '   ')]:
            with pytest.raises(ValueError, match='non-empty actor and reason'):
                kb.unblock_task(conn, tid, actor=actor, reason=reason)
            assert _snapshot(conn, tid) == before
    assert _cli('unblock', tid, '--reason', 'owner approved release') == 0
    capsys.readouterr()
    with kbc.connect() as conn:
        after = _snapshot(conn, tid)
        assert after[0] == before[0]  # No promotion or metadata/spec mutation.
        events = [e for e in kb.list_events(conn, tid) if e.kind == 'unblocked']
        assert len(events) == 1
        assert events[0].payload == {
            'status': 'triage', 'cleared_hold': 'sticky_block' if kind == 'sticky_gave_up' else kind,
            'actor': cli._profile_author(), 'reason': 'owner approved release',
        }
        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1
        assert comments[0].author == cli._profile_author()
        assert 'owner approved release' in comments[0].body
        assert 'triage' in comments[0].body
    assert _cli('unblock', tid, '--reason', 'duplicate') == 1
    capsys.readouterr()
    with kbc.connect() as conn:
        assert _snapshot(conn, tid) == after
    assert _cli('specify', tid, '--keep-spec', '--expect-sha256', digest, '--author', 'owner') == 0
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == 'ready'
        assert (task.title, task.body) == ('Reviewed spec', 'Keep these instructions')


@pytest.mark.parametrize('refusal', ['no_hold', 'foreign', 'worker', 'delegate', 'audit_failure'])
def test_triage_refusal_or_failure_writes_nothing(kanban_home, refusal, monkeypatch, capsys):
    with kbc.connect() as conn:
        tid = _held_task(conn, 'none' if refusal == 'no_hold' else 'block_loop_detected')
        if refusal == 'foreign':
            conn.executescript("""
                CREATE TABLE fleet_kanban_issue_map (
                    local_task_id TEXT PRIMARY KEY, issue_key TEXT, title TEXT, body TEXT,
                    source_node TEXT, current_node TEXT
                );
                CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN
                    INSERT INTO fleet_kanban_issue_map
                    (local_task_id, issue_key, title, body, source_node, current_node)
                    VALUES (NEW.id, NEW.id, NEW.title, NEW.body, 'local', 'local');
                END;
            """)
            conn.execute(
                'INSERT INTO fleet_kanban_issue_map (local_task_id, source_node, current_node) VALUES (?, ?, ?)',
                (tid, 'other', 'other'),
            )
            conn.commit()
        before = _snapshot(conn, tid)
        if refusal == 'worker':
            monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker-task')
        if refusal == 'audit_failure':
            def fail(*args, **kwargs):
                raise RuntimeError('audit unavailable')
            monkeypatch.setattr(kb, '_insert_comment', fail)
        if refusal == 'delegate':
            from agent.delegation_context import delegated_child_context
            with delegated_child_context():
                assert _cli('unblock', tid, '--reason', 'release') == 1
        else:
            assert _cli('unblock', tid, '--reason', 'release') == 1
        error = capsys.readouterr().err
        expected = {
            'no_hold': 'triage-with-hold', 'foreign': 'foreign fleet mirror',
            'worker': 'orchestrator-only', 'delegate': 'delegate_task child contexts',
            'audit_failure': 'audit unavailable',
        }[refusal]
        assert expected in error
        assert _snapshot(conn, tid) == before
        if refusal == 'delegate':
            with delegated_child_context():
                with pytest.raises(PermissionError):
                    kb.unblock_task(conn, tid, actor='owner', reason='release')
            assert _snapshot(conn, tid) == before


@pytest.mark.parametrize("title, body", [
    ("HOLD FOR TURNER", "Reviewed spec"),
    ("Reviewed spec", "HOLD FOR TURNER"),
])
def test_marker_only_triage_has_no_hold_to_clear(kanban_home, title, body):
    with kbc.connect() as conn:
        tid = _held_task(conn, 'none', title=title, body=body)
        before = _snapshot(conn, tid)
        assert kb.unblock_task(conn, tid, actor='owner', reason='release') is False
        assert _snapshot(conn, tid) == before
