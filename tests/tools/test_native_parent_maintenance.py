"""Actual constructor and frozen-policy parent authority; no profile-name grants."""
import json
import os
from dataclasses import replace
from unittest.mock import patch

import pytest
from tests._fixtures.native_agent_isolation import native_agent_isolation


@pytest.fixture
def parent_assignment(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_policy import build_policy, policy_scope
    from hermes_constants import get_hermes_home
    from gateway.session_context import scoped_current_session_id
    from hermes_cli.config import save_config
    for key in ('HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_CRON_SESSION', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB'):
        monkeypatch.delenv(key, raising=False)
    home = get_hermes_home()
    save_config({'approvals': {'mode': 'manual'}, 'agent': {'max_turns': 40}})
    db = tmp_path / 'authority.db'
    conn = connect(db)
    own = kb.create_task(conn, title='ordinary native parent', assignee='unfamiliar-owner')
    target = kb.create_task(conn, title='routing target')
    kb.claim_task(conn, own)
    kb.claim_task(conn, target)
    kb.block_task(conn, target, reason='fixture')
    task = kb.get_task(conn, own)
    context = {'task_id': own, 'run_id': task.current_run_id, 'claim_lock': task.claim_lock,
               'db': str(db), 'board': 'default', 'profile': 'unfamiliar-owner',
               'tenant': None, 'selected_toolsets': ['kanban', 'file']}
    kb._append_event(conn, own, 'worker_bound', {'pid': os.getpid(), 'claim_lock': task.claim_lock}, run_id=task.current_run_id)
    for key, value in {'HERMES_KANBAN_TASK': own, 'HERMES_KANBAN_RUN_ID': str(task.current_run_id),
                       'HERMES_KANBAN_CLAIM_LOCK': task.claim_lock, 'HERMES_KANBAN_DB': str(db)}.items():
        monkeypatch.setenv(key, value)
    policy = replace(build_policy({'source': 'cli', 'cwd': str(tmp_path), 'toolsets': ['kanban', 'file']}, {}, profile_terminal=False),
                     source='kanban', platform='kanban', kanban_json=json.dumps(context))
    with policy_scope(policy), scoped_current_session_id('parent-session'):
        yield conn, own, target, policy, home
    conn.close()


def construct_parent(toolsets=None):
    from run_agent import AIAgent
    with patch('agent.process_bootstrap.OpenAI'):
        return AIAgent(provider='openai', model='gpt-6.1-sol', api_key='fixture-only',
                       base_url='http://127.0.0.1:1/v1',
                       session_id='parent-session', enabled_toolsets=toolsets or ['kanban', 'file'],
                       quiet_mode=True, skip_context_files=True, skip_memory=True,
                       skip_background_review=True)


def test_parent_context_and_board_guards(parent_assignment, tmp_path):
    from hermes_cli.kanban_db_connect import connect
    from tools import kanban_tools as kt
    conn, _, _, policy, _ = parent_assignment
    assert kt._native_parent_context() == json.loads(policy.kanban_json)
    kt._require_parent_board_arg({})
    kt._require_parent_board_arg({'board': 'default'})
    kt._require_assigned_board(conn)
    with pytest.raises(kt._Reject):
        kt._require_parent_board_arg({'board': 'foreign'})
    foreign = connect(tmp_path / 'foreign.db')
    try:
        with pytest.raises(kt._Reject):
            kt._require_assigned_board(foreign)
    finally:
        foreign.close()


@pytest.mark.parametrize('kind', ['child', 'terminal', 'cron', 'pin', 'missing-pin', 'stale',
                                  'pid', 'unselected', 'injected', 'interactive'])
def test_parent_admission_fails_closed(parent_assignment, monkeypatch, kind):
    from contextlib import nullcontext
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
    from gateway.session_policy import policy_scope
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    conn, own, _, policy, _ = parent_assignment
    context = json.loads(policy.kanban_json)
    contexts = {
        'child': delegated_child_context,
        'cron': non_dispatcher_owned_context,
        'unselected': lambda: policy_scope(replace(policy, toolsets=('file',))),
        'injected': lambda: policy_scope(replace(
            policy, kanban_json=json.dumps(dict(context, selected_toolsets=['file'])))),
        'interactive': lambda: policy_scope(replace(policy, source='cli')),
    }
    actions = {
        'terminal': lambda: monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1'),
        'pin': lambda: monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', 'forged'),
        'missing-pin': lambda: monkeypatch.delenv('HERMES_KANBAN_TASK'),
        'stale': lambda: conn.execute('UPDATE tasks SET claim_expires=0 WHERE id=?', (own,)),
        'pid': lambda: kb._append_event(conn, own, 'worker_bound', {'pid': 1}, run_id=context['run_id']),
    }
    with contexts.get(kind, nullcontext)():
        actions.get(kind, lambda: None)()
        conn.commit()
        assert kt._native_parent_context() is None
        with pytest.raises(kt._Reject):
            kt._require_parent_board_arg({})


@pytest.mark.parametrize('mutation', [None, 'task-expiry', 'run-expiry', 'run-lock', 'run-status',
                                    'pid', 'tenant', 'profile', 'board'])
def test_db_parent_assignment_checked_before_unblock(parent_assignment, mutation):
    from hermes_cli import kanban_db as kb
    from dataclasses import asdict
    conn, own, target, policy, _ = parent_assignment
    caller = json.loads(policy.kanban_json)
    run = caller['run_id']
    statements = {
        'task-expiry': ('UPDATE tasks SET claim_expires=0 WHERE id=?', own),
        'run-expiry': ('UPDATE task_runs SET claim_expires=0 WHERE id=?', run),
        'run-lock': ("UPDATE task_runs SET claim_lock='revoked' WHERE id=?", run),
        'run-status': ("UPDATE task_runs SET status='reclaimed' WHERE id=?", run),
        'tenant': ("UPDATE tasks SET tenant='foreign' WHERE id=?", target),
        'profile': ("UPDATE tasks SET assignee='foreign' WHERE id=?", own),
    }
    if mutation in statements:
        sql, value = statements[mutation]
        conn.execute(sql, (value,))
        conn.commit()
    elif mutation == 'pid':
        kb._append_event(conn, own, 'worker_bound', {'pid': 1}, run_id=run)
    elif mutation == 'board':
        caller['db'] += '.foreign'
    before = asdict(kb.get_task(conn, target)), kb.list_events(conn, target)
    assert kb.unblock_task(conn, target, caller_assignment=caller) is (mutation is None)
    if mutation is not None:
        assert (asdict(kb.get_task(conn, target)), kb.list_events(conn, target)) == before
    else:
        assert kb.get_task(conn, target).status == 'ready'
        assert kb.list_events(conn, target)[-1].kind == 'unblocked'


def test_real_constructor_keeps_worker_tools_and_parent_context(parent_assignment):
    from tools import kanban_tools as kt
    agent = construct_parent()
    try:
        assert {'kanban_show', 'kanban_comment'} <= agent.valid_tool_names
        assert kt._native_parent_context() == json.loads(parent_assignment[3].kanban_json)
    finally:
        agent.close()
