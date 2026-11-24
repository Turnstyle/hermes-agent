"""Native parent body CAS: exact content, atomic audit, and frozen authority."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from tests._fixtures.native_agent_isolation import native_agent_isolation
from tests.tools.test_native_parent_maintenance import construct_parent

pytest_plugins = ("tests.tools.test_native_parent_maintenance",)


@pytest.fixture(autouse=True)
def empty_target_body(parent_assignment):
    conn, _, target, _, _ = parent_assignment
    conn.execute("UPDATE tasks SET body='' WHERE id=?", (target,))
    conn.commit()


def dispatch(target, old_body='', new_body='new', **extra):
    from tools import kanban_body_tools  # Register the real native body handler.
    from tools.registry import registry
    return json.loads(registry.dispatch('kanban_edit_body', dict(
        task_id=target, old_body=old_body, new_body=new_body, **extra)))


def test_actual_parent_exact_readback_conflict_and_lifecycle(parent_assignment):
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    agent = construct_parent()
    try:
        assert 'kanban_edit_body' in agent.valid_tool_names
        before = asdict(kb.get_task(conn, target))
        runs = [dict(r) for r in conn.execute('SELECT * FROM task_runs')]
        links = list(conn.execute('SELECT * FROM task_links'))
        body = ' \n\tΩ e\u0301\r\nend  '
        result = dispatch(target, new_body=body)
        assert result == {'ok': True, 'task_id': target, 'body': body}
        after = asdict(kb.get_task(conn, target))
        assert after == dict(before, body=body)
        assert [dict(r) for r in conn.execute('SELECT * FROM task_runs')] == runs
        assert list(conn.execute('SELECT * FROM task_links')) == links
        events = kb.list_events(conn, target)
        assert events[-1].kind == 'edited'
        assert events[-1].payload['fields'] == ['body']
        stale = dispatch(target, old_body='', new_body='lost update')
        assert stale['code'] == 'body_conflict' and stale['ok'] is False
        assert asdict(kb.get_task(conn, target)) == after
        assert kb.list_events(conn, target) == events
        assert dispatch(target, old_body=body, new_body='')['body'] == ''
    finally:
        agent.close()


@pytest.mark.parametrize('kind', ['native-child', 'terminal', 'kernel', 'unselected', 'forged', 'stale', 'tenant', 'board'])
def test_denied_authority_leaves_body_and_audit(parent_assignment, monkeypatch, kind):
    from agent.delegation_context import delegated_child_context
    from gateway.session_policy import policy_scope
    from hermes_cli import kanban_db as kb
    conn, own, target, policy, home = parent_assignment
    before = kb.list_events(conn, target)
    with ExitStack() as stack:
        if kind == 'native-child':
            stack.enter_context(delegated_child_context())
        if kind in ('terminal', 'kernel'):
            import sys
            from tools.environments.local import build_subprocess_env
            from tools.code_execution_env import _build_child_env
            env = (build_subprocess_env() if kind == 'terminal' else _build_child_env(
                rpc_endpoint='fixture', rpc_token='fixture', tmpdir=str(home), child_python=sys.executable))
            assert env.get('HERMES_DELEGATED_CHILD_CONTEXT')
            monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', env['HERMES_DELEGATED_CHILD_CONTEXT'])
        if kind == 'unselected':
            stack.enter_context(policy_scope(replace(policy, toolsets=('file',))))
        if kind == 'forged':
            monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', 'forged')
        if kind == 'stale':
            conn.execute('UPDATE tasks SET claim_expires=0 WHERE id=?', (own,)); conn.commit()
        if kind == 'tenant':
            conn.execute("UPDATE tasks SET tenant='foreign' WHERE id=?", (target,)); conn.commit()
        if kind in ('native-child', 'terminal', 'kernel'):
            child = construct_parent()
            try:
                assert 'kanban_edit_body' not in child.valid_tool_names
            finally:
                child.close()
        result = dispatch(target, **({'board': 'foreign'} if kind == 'board' else {}))
        assert 'error' in result, result
    assert kb.get_task(conn, target).body == ''
    assert kb.list_events(conn, target) == before


@pytest.mark.parametrize('old,new', [(None, 'x'), (0, 'x'), ('', None), ('', []), ('', 'x' * 262145)])
def test_exact_string_and_size_boundary(parent_assignment, old, new):
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    assert 'error' in dispatch(target, old_body=old, new_body=new)
    assert kb.get_task(conn, target).body == ''


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_target_is_refused(parent_assignment, status):
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, target)); conn.commit()
    before = asdict(kb.get_task(conn, target)), kb.list_events(conn, target)
    assert 'error' in dispatch(target)
    assert (asdict(kb.get_task(conn, target)), kb.list_events(conn, target)) == before


@pytest.mark.parametrize('mutation', ['task-expiry', 'run-expiry', 'run-lock', 'run-status', 'pid', 'tenant'])
def test_revoked_after_preflight(parent_assignment, monkeypatch, mutation):
    from hermes_cli import kanban_db as kb
    conn, own, target, _, _ = parent_assignment
    original = kb.write_txn
    @contextmanager
    def revoke(opened, **kwargs):
        run = kb.get_task(conn, own).current_run_id
        statements = {
            'task-expiry': ('UPDATE tasks SET claim_expires=0 WHERE id=?', own),
            'run-expiry': ('UPDATE task_runs SET claim_expires=0 WHERE id=?', run),
            'run-lock': ("UPDATE task_runs SET claim_lock='revoked' WHERE id=?", run),
            'run-status': ("UPDATE task_runs SET status='reclaimed' WHERE id=?", run),
            'tenant': ("UPDATE tasks SET tenant='foreign' WHERE id=?", target),
        }
        if mutation == 'pid':
            kb._append_event(conn, own, 'worker_bound', {'pid': 1}, run_id=run)
        else:
            sql, value = statements[mutation]; conn.execute(sql, (value,))
        conn.commit()
        with original(opened, **kwargs):
            yield opened
    monkeypatch.setattr(kb, 'write_txn', revoke)
    before = kb.list_events(conn, target)
    assert 'error' in dispatch(target)
    assert kb.get_task(conn, target).body == ''
    assert kb.list_events(conn, target) == before


def test_audit_failure_rolls_back(parent_assignment, monkeypatch):
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    def fail(*args, **kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(kb, '_append_event', fail)
    before = kb.list_events(conn, target)
    assert 'error' in dispatch(target)
    assert kb.get_task(conn, target).body == ''
    assert kb.list_events(conn, target) == before


def test_two_writers_only_one_wins(parent_assignment):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_db_body import compare_and_set_body
    conn, _, target, policy, _ = parent_assignment
    caller = json.loads(policy.kanban_json)
    barrier = threading.Barrier(2)
    before = len(kb.list_events(conn, target))
    def write(value):
        opened = connect(Path(caller['db']))
        try:
            barrier.wait(timeout=10)
            return compare_and_set_body(opened, target, old_body='', new_body=value, caller_assignment=caller)
        finally:
            opened.close()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, ['first', 'second']))
    assert sorted(r['ok'] for r in results) == [False, True]
    winner = next(r for r in results if r['ok'])
    assert kb.get_task(conn, target).body == winner['body']
    assert len(kb.list_events(conn, target)) == before + 1


@pytest.mark.parametrize('stored,old_body', [
    ("CAST(X'80' AS TEXT)", '\ufffd'),
    ("X'616263'", 'abc'),
    ('NULL', ''),
])
def test_noncanonical_stored_body_conflicts(parent_assignment, stored, old_body):
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    conn.execute('UPDATE tasks SET body=' + stored + ' WHERE id=?', (target,))
    conn.commit()
    before = conn.execute('SELECT typeof(body), hex(body) FROM tasks WHERE id=?', (target,)).fetchone()
    events = kb.list_events(conn, target)
    result = dispatch(target, old_body=old_body, new_body='must not overwrite')
    assert result.get('code') == 'body_conflict', result
    after = conn.execute('SELECT typeof(body), hex(body) FROM tasks WHERE id=?', (target,)).fetchone()
    assert tuple(after) == tuple(before)
    assert kb.list_events(conn, target) == events


@pytest.mark.parametrize('stored,tenant', [
    ("CAST(X'80' AS TEXT)", '\ufffd'),
    ("X'74656e616e74'", 'tenant'),
])
def test_noncanonical_target_tenant_is_denied(parent_assignment, stored, tenant):
    from gateway.session_policy import policy_scope
    from hermes_cli import kanban_db as kb
    conn, own, target, policy, _ = parent_assignment
    conn.execute('UPDATE tasks SET tenant=? WHERE id=?', (tenant, own))
    conn.execute('UPDATE tasks SET tenant=' + stored + ' WHERE id=?', (target,))
    conn.commit()
    context = dict(json.loads(policy.kanban_json), tenant=tenant)
    events = kb.list_events(conn, target)
    with policy_scope(replace(policy, kanban_json=json.dumps(context))):
        result = dispatch(target)
    assert 'error' in result and not result.get('ok'), result
    assert kb.get_task(conn, target).body == ''
    assert kb.list_events(conn, target) == events


@pytest.mark.parametrize('body', ['a\x00b', 'e\u0301', '\u00e9'])
def test_canonical_text_remains_byte_exact(parent_assignment, body):
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    assert dispatch(target, new_body=body)['body'] == body
    if body == 'e\u0301':
        assert dispatch(target, old_body='\u00e9', new_body='no')['code'] == 'body_conflict'
        assert kb.get_task(conn, target).body == body
    assert dispatch(target, old_body=body, new_body='exact success')['body'] == 'exact success'


def test_frozen_selection_not_injected_toolset(parent_assignment):
    from gateway.session_policy import policy_scope
    conn, _, target, policy, _ = parent_assignment
    context = dict(json.loads(policy.kanban_json), selected_toolsets=['file'])
    with policy_scope(replace(policy, kanban_json=json.dumps(context))):
        result = dispatch(target)
    assert 'error' in result and not result.get('ok'), result
    assert conn.execute('SELECT body FROM tasks WHERE id=?', (target,)).fetchone()[0] == ''


@pytest.mark.parametrize('kind', ['terminal', 'kernel'])
def test_descendant_subprocess_dispatch(parent_assignment, tmp_path, kind):
    import subprocess
    import sys
    from hermes_cli import kanban_db as kb
    from tools.code_execution_env import _build_child_env
    from tools.environments.local import build_subprocess_env
    conn, _, target, _, _ = parent_assignment
    if kind == 'kernel':
        env = _build_child_env(rpc_endpoint='fixture', rpc_token='fixture',
                               tmpdir=str(tmp_path), child_python=sys.executable)
    else:
        env = build_subprocess_env()
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK'):
        assert key not in env
    script = (
        'import sys,json;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[2])) + ');'
        'from tools import kanban_body_tools;from tools.registry import registry;'
        'print(registry.dispatch("kanban_edit_body",' + repr(dict(task_id=target, old_body='', new_body='denied')) + '))'
    )
    before = kb.list_events(conn, target)
    completed = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path,
                               capture_output=True, text=True, timeout=45, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert 'error' in result and 'Unknown tool' not in result['error'], result
    assert not result.get('ok')
    assert kb.get_task(conn, target).body == ''
    assert kb.list_events(conn, target) == before


def test_maximum_body_and_missing_or_extra_arguments(parent_assignment):
    from tools.registry import registry
    from hermes_cli import kanban_db as kb
    conn, _, target, _, _ = parent_assignment
    # Unicode byte bound is distinct from a character count.
    maximum = 'Ω' * 131072
    assert dispatch(target, new_body=maximum)['body'] == maximum
    events = kb.list_events(conn, target)
    assert 'error' in dispatch(target, old_body=maximum, new_body=maximum + 'Ω')
    assert 'error' in dispatch(target, old_body=maximum, new_body=maximum)
    assert 'error' in dispatch(target, old_body=maximum, new_body='x', status='ready')
    assert 'error' in json.loads(registry.dispatch('kanban_edit_body', {'task_id': target, 'new_body': 'x'}))
    assert kb.get_task(conn, target).body == maximum
    assert kb.list_events(conn, target) == events
