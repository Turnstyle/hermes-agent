"""Worker board authority uses real SQLite, dispatcher identity and registry ingress."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent import delegation_context as dc
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_db_dispatch import adopt_worker_pid
from tools import kanban_tools as kt
from tools.registry import registry


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_PROFILE', 'worker')
    for name in ('HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB',
                 'HERMES_KANBAN_BOARD', 'HERMES_PROFILE_NAME', 'HERMES_KANBAN_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (tmp_path / 'config.yaml').write_text('kanban:\n  worker_board_moves: true\n', encoding='utf-8')
    path = kb.kanban_db_path()
    conn = connect(path)
    source = kb.create_task(conn, title='source', assignee='worker')
    assert kb.claim_task(conn, source)
    run = kb.get_task(conn, source).current_run_id
    assert adopt_worker_pid(conn, source, run, os.getpid())
    lock = kb.get_task(conn, source).claim_lock
    for key, value in {'TASK': source, 'RUN_ID': str(run), 'CLAIM_LOCK': lock,
                       'DB': str(path), 'BOARD': 'default'}.items():
        monkeypatch.setenv('HERMES_KANBAN_' + key, value)
    yield conn, source, run, path, tmp_path
    conn.close()


def call(move, tid, **kw):
    return json.loads(registry.dispatch('kanban_' + move, {'task_id': tid, **kw}))


def target(board, status='blocked'):
    conn = board[0]
    tid = kb.create_task(conn, title='target', assignee='peer')
    if status == 'blocked':
        assert kb.block_task(conn, tid, reason='fixture', kind='needs_input')
    else:
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status = ? WHERE id = ?', (status, tid))
    return tid


def token(conn, tid):
    return conn.execute("SELECT max(id) FROM task_events WHERE task_id = ? AND kind = 'blocked'",
                        (tid,)).fetchone()[0]


def args(conn, tid, move):
    return {'expected_blocked_event': token(conn, tid), 'evidence': 'resolved'} if move == 'unblock' else {}


def snapshot(conn, tid):
    return (dict(conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()),
            [tuple(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id = ?', (tid,))])


@pytest.mark.parametrize('move', ['unblock', 'promote'])
def test_valid_move_and_audit(board, move):
    conn, source, run, *_ = board
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    kw = args(conn, tid, move)
    assert call(move, tid, board='default', **kw) == {'ok': True, 'task_id': tid, 'status': 'ready'}
    assert kt._check_kanban_board_moves()
    assert not kt._check_kanban_orchestrator_mode()
    event = kb.list_events(conn, tid)[-1]
    assert event.kind == ('unblocked' if move == 'unblock' else 'promoted_manual')
    assert event.payload['actor_profile'] == 'worker'
    assert event.payload['source_task_id'] == source
    assert event.payload['source_run_id'] == run
    if move == 'unblock':
        assert event.payload['expected_blocked_event'] == kw['expected_blocked_event']
        assert event.payload['evidence'] == 'resolved'
    assert kb.get_task(conn, source).status == 'running'


@pytest.mark.parametrize('flag', ['false', '"true"', '1', 'null'])
@pytest.mark.parametrize('move', ['unblock', 'promote'])
def test_strict_opt_in(board, flag, move):
    conn, _, _, _, home = board
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG['kanban']['worker_board_moves'] is False
    (home / 'config.yaml').write_text(f'kanban:\n  worker_board_moves: {flag}\n', encoding='utf-8')
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    before = snapshot(conn, tid)
    assert not kt._check_kanban_board_moves()
    from toolsets import resolve_toolset
    names = {item['function']['name'] for item in registry.get_definitions(
        set(resolve_toolset('kanban')), quiet=True)}
    assert not {'kanban_unblock', 'kanban_promote'} & names
    assert 'error' in call(move, tid, **args(conn, tid, move))
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['unblock', 'promote'])
@pytest.mark.parametrize('pin', ['TASK', 'RUN_ID', 'CLAIM_LOCK', 'DB', 'BOARD'])
def test_missing_pins(board, monkeypatch, move, pin):
    conn = board[0]
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    kw = args(conn, tid, move)
    before = snapshot(conn, tid)
    monkeypatch.delenv('HERMES_KANBAN_' + pin)
    assert 'error' in call(move, tid, **kw)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['unblock', 'promote'])
@pytest.mark.parametrize('field,value,table', [
    ('status', 'review', 'tasks'), ('assignee', 'other', 'tasks'),
    ('current_run_id', None, 'tasks'), ('claim_lock', 'foreign', 'tasks'),
    ('claim_expires', 0, 'tasks'), ('worker_pid', 999999, 'tasks'),
    ('worker_started_at', 'unverified', 'tasks'), ('worker_started_at', None, 'tasks'),
    ('status', 'done', 'task_runs'), ('profile', 'other', 'task_runs'),
    ('task_id', 'other', 'task_runs'), ('ended_at', 1, 'task_runs'),
    ('claim_lock', 'foreign', 'task_runs'), ('claim_expires', 0, 'task_runs'),
    ('worker_pid', 999999, 'task_runs'), ('worker_started_at', 'wrong', 'task_runs'),
])
def test_source_revocation(board, move, field, value, table):
    conn, source, run, *_ = board
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    kw = args(conn, tid, move)
    before = snapshot(conn, tid)
    with kb.write_txn(conn):
        conn.execute(f'UPDATE {table} SET {field} = ? WHERE id = ?',
                     (value, source if table == 'tasks' else run))
    assert not kt._check_kanban_board_moves()
    assert 'error' in call(move, tid, **kw)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['unblock', 'promote'])
def test_self_cross_board_and_forged_audit(board, move):
    conn, source, *_ = board
    assert 'error' in call(move, source, **args(conn, source, move))
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    before = snapshot(conn, tid)
    kw = args(conn, tid, move)
    assert 'error' in call(move, tid, board='foreign', **kw)
    assert 'error' in call(move, tid, actor_profile='admin', **kw)
    assert 'error' in call(move, tid, source_task_id='forged', **kw)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('bad', [None, True, False, '1', 1.0, -1, 999999])
def test_stale_or_invalid_event(board, bad):
    conn = board[0]
    tid = target(board)
    before = snapshot(conn, tid)
    assert 'error' in call('unblock', tid, expected_blocked_event=bad, evidence='fixed')
    assert snapshot(conn, tid) == before


def test_schema_and_show_event_id(board):
    conn = board[0]
    tid = target(board)
    schema = registry.get_schema('kanban_unblock')
    assert schema['parameters']['properties']['expected_blocked_event']['type'] == 'integer'
    shown = json.loads(registry.dispatch('kanban_show', {'task_id': tid}))
    blocked = [e for e in shown['events'] if e['kind'] == 'blocked'][-1]
    assert type(blocked['id']) is int
    assert blocked['id'] == token(conn, tid)
    from toolsets import resolve_toolset
    assert 'kanban_promote' in resolve_toolset('kanban')
    names = {item['function']['name'] for item in registry.get_definitions(
        set(resolve_toolset('kanban')), quiet=True)}
    assert {'kanban_unblock', 'kanban_promote'} <= names
    assert 'kanban_list' not in names


@pytest.mark.parametrize('move', ['unblock', 'promote'])
@pytest.mark.parametrize('context', ['child', 'cron', 'descendant', 'predicate_error'])
def test_context_fences(board, monkeypatch, move, context):
    conn = board[0]
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    kw = args(conn, tid, move)
    before = snapshot(conn, tid)
    @contextmanager
    def fence():
        if context == 'child':
            with dc.delegated_child_context():
                yield
        elif context == 'cron':
            with dc.non_dispatcher_owned_context():
                yield
        else:
            if context == 'descendant':
                monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, '1')
            else:
                def boom():
                    raise RuntimeError('predicate unavailable')
                monkeypatch.setattr(dc, 'is_dispatcher_owned_worker_context', boom)
            yield
    with fence():
        assert not kt._check_kanban_board_moves()
        assert 'error' in call(move, tid, **kw)
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['unblock', 'promote'])
def test_spawned_process_has_no_parent_authority(board, move):
    conn = board[0]
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    before = snapshot(conn, tid)
    code = ('import json; from tools import kanban_tools; from tools.registry import registry; '
            f'print(registry.dispatch("kanban_{move}", {dict(task_id=tid, **args(conn, tid, move))!r}))')
    for env in (dict(os.environ), dc.scrub_kanban_env(os.environ)):
        proc = subprocess.run([sys.executable, '-c', code], env=env,
                              capture_output=True, text=True, timeout=45)
        assert proc.returncode == 0, proc.stderr
        assert 'error' in json.loads(proc.stdout.strip().splitlines()[-1])
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['unblock', 'promote'])
@pytest.mark.parametrize('claim', ['lock', 'open_run', 'pointer', 'live_pid'])
def test_inconsistent_target_claim_is_refused(board, move, claim):
    conn = board[0]
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    with kb.write_txn(conn):
        if claim == 'lock':
            conn.execute('UPDATE tasks SET claim_lock = ? WHERE id = ?', ('live', tid))
        elif claim == 'live_pid':
            from hermes_cli.kanban_db_dispatch import _process_fingerprint
            conn.execute('UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?',
                         (os.getpid(), _process_fingerprint(os.getpid()), tid))
        elif claim == 'pointer':
            conn.execute('UPDATE tasks SET current_run_id = ? WHERE id = ?', (987, tid))
        else:
            conn.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)", (tid,))
    before = snapshot(conn, tid)
    assert 'error' in call(move, tid, **args(conn, tid, move))
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('status', ['blocked', 'review', 'scheduled', 'triage', 'running', 'ready', 'done'])
def test_worker_promote_never_expands_statuses(board, status):
    conn = board[0]
    tid = target(board, status)
    before = snapshot(conn, tid)
    assert 'error' in call('promote', tid)
    assert snapshot(conn, tid) == before


def test_missing_evidence_and_intervening_claim(board):
    conn = board[0]
    tid = target(board)
    event = token(conn, tid)
    assert 'error' in call('unblock', tid, expected_blocked_event=event)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'claimed', {'source_status': 'review'})
    before = snapshot(conn, tid)
    assert 'error' in call('unblock', tid, expected_blocked_event=event, evidence='fixed')
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('review', [False, True])
@pytest.mark.parametrize('parent_open', [False, True])
def test_unblock_resumption_and_parent_gating(board, review, parent_open):
    conn = board[0]
    tid = target(board)
    if parent_open:
        parent = kb.create_task(conn, title='parent', assignee='peer')
        with kb.write_txn(conn):
            conn.execute('INSERT INTO task_links VALUES (?,?)', (parent, tid))
    if review:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'blocked', {'resume_status': 'review'})
    expected = 'todo' if parent_open else ('review' if review else 'ready')
    assert call('unblock', tid, **args(conn, tid, 'unblock'))['status'] == expected
    if review and parent_open:
        assert 'error' in call('promote', tid)


@pytest.mark.parametrize('change', ['source_revoke', 'parent_reopen', 'target_claim', 'target_reblock'])
def test_second_connection_changes_before_write_lock(board, monkeypatch, change):
    conn, source, _, path, _ = board
    move = 'promote' if change == 'parent_reopen' else 'unblock'
    tid = target(board, 'todo' if move == 'promote' else 'blocked')
    kw = args(conn, tid, move)
    parent = kb.create_task(conn, title='parent', assignee='peer')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.execute('INSERT INTO task_links VALUES (?,?)', (parent, tid))
    other = connect(path)
    real_txn = kb.write_txn
    @contextmanager
    def intervening(c, **kwargs):
        with real_txn(other):
            if change == 'source_revoke':
                other.execute("UPDATE tasks SET assignee = 'other' WHERE id = ?", (source,))
            elif change == 'parent_reopen':
                other.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            elif change == 'target_claim':
                other.execute('UPDATE tasks SET claim_lock = ? WHERE id = ?', ('claimed', tid))
                kb._append_event(other, tid, 'claimed')
            else:
                kb._append_event(other, tid, 'blocked', {'reason': 'new block'})
        with real_txn(c, **kwargs):
            yield
    monkeypatch.setattr(kb, 'write_txn', intervening)
    try:
        assert 'error' in call(move, tid, **kw)
        assert kb.get_task(conn, tid).status == ('todo' if move == 'promote' else 'blocked')
        assert not [e for e in kb.list_events(conn, tid) if e.kind in ('unblocked', 'promoted_manual')]
    finally:
        other.close()


def test_duplicate_unblock_across_connections(board):
    from hermes_cli.kanban_db_moves import worker_board_move
    conn, _, _, path, _ = board
    tid = target(board)
    kw = args(conn, tid, 'unblock')
    other = connect(path)
    try:
        assert worker_board_move(other, tid, move='unblock', **kw) == 'ready'
        with pytest.raises(ValueError, match='blocked status'):
            worker_board_move(conn, tid, move='unblock', **kw)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == 'unblocked']) == 1
    finally:
        other.close()


def test_visibility_does_not_create_db(board, monkeypatch):
    missing = board[4] / 'absent.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(missing))
    assert not kt._check_kanban_board_moves()
    assert not missing.exists()


def test_orchestrator_retains_ordinary_moves(board, monkeypatch):
    conn = board[0]
    first = target(board)
    second = target(board)
    for name in ('TASK', 'RUN_ID', 'CLAIM_LOCK'):
        monkeypatch.delenv('HERMES_KANBAN_' + name)
    assert call('unblock', first) == {'ok': True, 'task_id': first, 'status': 'ready'}
    assert call('promote', second)['status'] == 'ready'
    assert kb.list_events(conn, first)[-1].payload is None
    assert kb.list_events(conn, second)[-1].payload == {'actor': 'worker', 'reason': None}


def test_other_lifecycle_ownership_unchanged(board):
    conn = board[0]
    tid = target(board)
    before = snapshot(conn, tid)
    for name in ('kanban_complete', 'kanban_block', 'kanban_heartbeat'):
        assert 'error' in json.loads(registry.dispatch(name, {'task_id': tid}))
    assert snapshot(conn, tid) == before


def test_readonly_visibility_does_not_migrate(board, monkeypatch):
    import sqlite3
    blank = board[4] / 'blank.db'
    with sqlite3.connect(blank) as conn:
        conn.execute('CREATE TABLE sentinel(value TEXT)')
    before = blank.read_bytes()
    monkeypatch.setenv('HERMES_KANBAN_DB', str(blank))
    assert not kt._check_kanban_board_moves()
    assert blank.read_bytes() == before
    with sqlite3.connect(blank) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ['sentinel']


def test_worker_review_run_can_move_other_card(board):
    conn, source, run, *_ = board
    with kb.write_txn(conn):
        kb._append_event(conn, source, 'claimed', {'source_status': 'review'}, run_id=run)
    tid = target(board)
    assert call('unblock', tid, **args(conn, tid, 'unblock'))['status'] == 'ready'


def test_exact_connection_and_process_probe_fail_closed(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_moves import worker_board_move
    conn = board[0]
    tid = target(board)
    before = snapshot(conn, tid)
    monkeypatch.setattr(dispatch, '_process_fingerprint', lambda pid: None)
    assert not kt._check_kanban_board_moves()
    assert 'error' in call('unblock', tid, **args(conn, tid, 'unblock'))
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('move', ['unblock', 'promote'])
def test_audit_text_is_redacted(board, move):
    conn = board[0]
    tid = target(board, 'blocked' if move == 'unblock' else 'todo')
    secret = 'Bearer sk-abcdefghijklmnopqrstuvwxyz1234567890'
    kw = args(conn, tid, move)
    kw['evidence' if move == 'unblock' else 'reason'] = secret
    assert call(move, tid, **kw)['ok']
    text = kb.list_events(conn, tid)[-1].payload['evidence' if move == 'unblock' else 'reason']
    assert 'sk-abcdefghijklmnopqrstuvwxyz1234567890' not in text


def test_worker_promote_parent_dependency(board):
    conn = board[0]
    tid = target(board, 'todo')
    parent = kb.create_task(conn, title='unfinished', assignee='peer')
    with kb.write_txn(conn):
        conn.execute('INSERT INTO task_links VALUES (?,?)', (parent, tid))
    before = snapshot(conn, tid)
    assert 'error' in call('promote', tid)
    assert snapshot(conn, tid) == before


def test_audit_failure_rolls_back_move(board, monkeypatch):
    conn = board[0]
    tid = target(board)
    before = snapshot(conn, tid)
    def fail(*a, **kw):
        raise OSError('audit write failed')
    monkeypatch.setattr(kb, '_append_event', fail)
    assert 'error' in call('unblock', tid, **args(conn, tid, 'unblock'))
    assert snapshot(conn, tid) == before


def test_simultaneous_duplicate_unblock_is_serialized(board):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hermes_cli.kanban_db_moves import worker_board_move
    conn, _, _, path, _ = board
    tid = target(board)
    kw = args(conn, tid, 'unblock')
    start = Barrier(2)
    def attempt():
        local = connect(path)
        try:
            start.wait(timeout=10)
            try:
                return worker_board_move(local, tid, move='unblock', **kw)
            except ValueError:
                return 'refused'
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ['ready', 'refused']
    assert len([e for e in kb.list_events(conn, tid) if e.kind == 'unblocked']) == 1


def test_live_running_target_is_unchanged(board):
    conn = board[0]
    tid = kb.create_task(conn, title='live peer', assignee='peer')
    assert kb.claim_task(conn, tid)
    run = kb.get_task(conn, tid).current_run_id
    assert adopt_worker_pid(conn, tid, run, os.getpid())
    before = snapshot(conn, tid)
    for move in ('promote', 'unblock'):
        assert 'error' in call(move, tid, **args(conn, tid, move))
    assert snapshot(conn, tid) == before


def test_worker_scheduled_unblock_is_refused(board):
    conn = board[0]
    tid = target(board, 'scheduled')
    before = snapshot(conn, tid)
    assert 'error' in call('unblock', tid, expected_blocked_event=1, evidence='fixed')
    assert snapshot(conn, tid) == before


def test_orchestrator_scheduled_unblock_is_unchanged(board, monkeypatch):
    conn = board[0]
    tid = target(board, 'scheduled')
    for name in ('TASK', 'RUN_ID', 'CLAIM_LOCK'):
        monkeypatch.delenv('HERMES_KANBAN_' + name)
    assert call('unblock', tid)['status'] == 'ready'
    assert kb.list_events(conn, tid)[-1].payload is None


def test_connection_pin_mismatch_is_refused(board, monkeypatch):
    from hermes_cli.kanban_db_moves import worker_board_move
    conn, source, _, path, home = board
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    unrelated = connect(home / 'other.db')
    monkeypatch.setenv('HERMES_KANBAN_TASK', source)
    tid = target(board)
    before = snapshot(conn, tid)
    try:
        with pytest.raises(ValueError, match='connection is not dispatcher database'):
            worker_board_move(unrelated, tid, move='unblock', **args(conn, tid, 'unblock'))
    finally:
        unrelated.close()
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('table', ['tasks', 'task_runs'])
def test_missing_source_record(board, table):
    conn, source, run, *_ = board
    tid = target(board)
    with kb.write_txn(conn):
        conn.execute(f'DELETE FROM {table} WHERE id = ?', (source if table == 'tasks' else run,))
    before = snapshot(conn, tid)
    assert not kt._check_kanban_board_moves()
    assert 'error' in call('unblock', tid, **args(conn, tid, 'unblock'))
    assert snapshot(conn, tid) == before


@pytest.mark.parametrize('raw', ['not-a-run', '0', '999999'])
def test_invalid_run_pin(board, monkeypatch, raw):
    conn = board[0]
    tid = target(board)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', raw)
    before = snapshot(conn, tid)
    assert not kt._check_kanban_board_moves()
    assert 'error' in call('unblock', tid, **args(conn, tid, 'unblock'))
    assert snapshot(conn, tid) == before


def test_omitted_flag_defaults_off(board):
    conn, _, _, _, home = board
    (home / 'config.yaml').write_text('{}', encoding='utf-8')
    tid = target(board)
    before = snapshot(conn, tid)
    assert not kt._check_kanban_board_moves()
    assert 'error' in call('unblock', tid, **args(conn, tid, 'unblock'))
    assert snapshot(conn, tid) == before


def test_source_current_run_cas(board):
    conn, source, run, *_ = board
    tid = target(board)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET current_run_id = ? WHERE id = ?', (run + 1, source))
    before = snapshot(conn, tid)
    assert not kt._check_kanban_board_moves()
    assert 'error' in call('unblock', tid, **args(conn, tid, 'unblock'))
    assert snapshot(conn, tid) == before
