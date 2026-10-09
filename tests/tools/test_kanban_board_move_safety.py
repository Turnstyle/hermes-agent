"""Native board moves preserve review gates and bound durable model text."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools import kanban_tools as kt
from tools.registry import registry
from tests.tools.test_kanban_worker_board_moves import board, call, target, snapshot, args


@pytest.fixture
def orchestrator(board, monkeypatch):
    for name in ('TASK', 'RUN_ID', 'CLAIM_LOCK'):
        monkeypatch.delenv('HERMES_KANBAN_' + name)
    return board


@pytest.mark.parametrize('status', ['blocked', 'review', 'scheduled', 'running', 'ready', 'done', 'triage'])
def test_native_promote_accepts_only_todo(orchestrator, status):
    conn = orchestrator[0]
    tid = target(orchestrator, status)
    before = snapshot(conn, tid)
    assert 'error' in call('promote', tid)
    assert snapshot(conn, tid) == before


def test_native_promote_preserves_review_provenance(orchestrator):
    conn = orchestrator[0]
    tid = target(orchestrator, 'todo')
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'unblocked', {'status': 'todo', 'resume_status': 'review'})
    before = snapshot(conn, tid)
    assert 'error' in call('promote', tid)
    assert snapshot(conn, tid) == before


def test_native_unblock_restores_review_and_cli_promote_unchanged(orchestrator):
    conn = orchestrator[0]
    first = target(orchestrator)
    second = target(orchestrator)
    with kb.write_txn(conn):
        for tid in (first, second):
            kb._append_event(conn, tid, 'blocked', {'resume_status': 'review'})
    assert call('unblock', first)['status'] == 'review'
    before = snapshot(conn, second)
    assert 'error' in call('promote', second)
    assert snapshot(conn, second) == before
    # Existing human CLI DB operation deliberately retains blocked promotion semantics.
    assert kb.promote_task(conn, second, actor='human') == (True, None)
    assert kb.get_task(conn, second).status == 'ready'


@pytest.mark.parametrize('hold', ['claim_lock', 'current_run_id', 'open_run', 'live_pid'])
def test_native_promote_refuses_inconsistent_live_target(orchestrator, hold):
    conn = orchestrator[0]
    tid = target(orchestrator, 'todo')
    with kb.write_txn(conn):
        if hold == 'open_run':
            conn.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)", (tid,))
        elif hold == 'live_pid':
            from hermes_cli.kanban_db_dispatch import _process_fingerprint
            conn.execute('UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?',
                         (os.getpid(), _process_fingerprint(os.getpid()), tid))
        else:
            conn.execute(f'UPDATE tasks SET {hold} = ? WHERE id = ?',
                         ('claim' if hold == 'claim_lock' else 99, tid))
    before = snapshot(conn, tid)
    assert 'error' in call('promote', tid)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('change', ['parent_reopen', 'target_claim'])
def test_native_promote_transactional_recheck(orchestrator, monkeypatch, change):
    conn, _, _, path, _ = orchestrator
    tid = target(orchestrator, 'todo')
    parent = kb.create_task(conn, title='parent', assignee='peer')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.execute('INSERT INTO task_links VALUES (?,?)', (parent, tid))
    other = connect(path)
    real = kb.write_txn
    @contextmanager
    def intervening(c, **kwargs):
        with real(other):
            if change == 'parent_reopen':
                other.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            else:
                other.execute('UPDATE tasks SET claim_lock = ? WHERE id = ?', ('new claim', tid))
        with real(c, **kwargs):
            yield
    monkeypatch.setattr(kb, 'write_txn', intervening)
    try:
        assert 'error' in call('promote', tid)
        assert kb.get_task(conn, tid).status == 'todo'
        assert not [e for e in kb.list_events(conn, tid) if e.kind == 'promoted_manual']
    finally:
        other.close()


@pytest.mark.parametrize('pin', ['TASK', 'RUN_ID', 'CLAIM_LOCK'])
@pytest.mark.parametrize('move', ['promote', 'unblock'])
def test_partial_worker_identity_denies_moves(orchestrator, monkeypatch, pin, move):
    conn = orchestrator[0]
    tid = target(orchestrator, 'todo' if move == 'promote' else 'blocked')
    before = snapshot(conn, tid)
    monkeypatch.setenv('HERMES_KANBAN_' + pin, 'residual')
    assert not kt._check_kanban_board_moves()
    assert 'error' in call(move, tid, **args(conn, tid, move))
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move,field', [('promote', 'reason'), ('unblock', 'evidence')])
@pytest.mark.parametrize('value', [{'text': 'bad'}, ['bad'], 42, True, 'x' * 4001])
def test_text_types_and_caps_at_handler_and_helper(board, move, field, value):
    from hermes_cli.kanban_db_moves import worker_board_move
    conn = board[0]
    tid = target(board, 'todo' if move == 'promote' else 'blocked')
    kw = args(conn, tid, move)
    kw[field] = value
    before = snapshot(conn, tid)
    assert 'error' in call(move, tid, **kw)
    with pytest.raises(ValueError):
        worker_board_move(conn, tid, move=move, **kw)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('reason', [{'bad': 'type'}, ['bad'], True, 3, 'x' * 4001])
def test_orchestrator_reason_caps_at_handler_and_helper(orchestrator, reason):
    from hermes_cli.kanban_db_moves import promote_todo_task
    conn = orchestrator[0]
    tid = target(orchestrator, 'todo')
    before = snapshot(conn, tid)
    assert 'error' in call('promote', tid, reason=reason)
    with pytest.raises(ValueError):
        promote_todo_task(conn, tid, actor='orchestrator', reason=reason)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['promote', 'unblock'])
def test_exact_text_cap_is_accepted(board, move):
    conn = board[0]
    tid = target(board, 'todo' if move == 'promote' else 'blocked')
    kw = args(conn, tid, move)
    field = 'reason' if move == 'promote' else 'evidence'
    kw[field] = 'x' * 4000
    assert call(move, tid, **kw)['ok']
    assert kb.list_events(conn, tid)[-1].payload[field] == 'x' * 4000


@pytest.mark.parametrize('evidence', [None, '', '   '])
def test_worker_unblock_still_requires_nonempty_evidence(board, evidence):
    conn = board[0]
    tid = target(board)
    before = snapshot(conn, tid)
    kw = args(conn, tid, 'unblock')
    kw['evidence'] = evidence
    assert 'error' in call('unblock', tid, **kw)
    assert snapshot(conn, tid) == before


def test_orchestrator_reason_none_and_string(orchestrator):
    conn = orchestrator[0]
    for reason in (None, '', 'x' * 4000):
        tid = target(orchestrator, 'todo')
        assert call('promote', tid, reason=reason)['ok']
        assert kb.list_events(conn, tid)[-1].payload == {'actor': 'worker', 'reason': reason}


def test_text_schema_caps():
    for name, field in [('kanban_promote', 'reason'), ('kanban_unblock', 'evidence')]:
        prop = registry.get_schema(name)['parameters']['properties'][field]
        assert prop['type'] == 'string'
        assert prop['maxLength'] == 4000


def test_direct_handler_text_validation(orchestrator):
    conn = orchestrator[0]
    tid = target(orchestrator, 'todo')
    before = snapshot(conn, tid)
    assert 'error' in json.loads(kt._handle_promote({'task_id': tid, 'reason': {'bad': 'type'}}))
    assert snapshot(conn, tid) == before
    blocked = target(orchestrator)
    before = snapshot(conn, blocked)
    assert 'error' in json.loads(kt._handle_unblock({'task_id': blocked, 'evidence': 'x' * 4001}))
    assert snapshot(conn, blocked) == before
