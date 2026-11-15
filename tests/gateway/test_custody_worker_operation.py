"""Offline receipts for the fixed custody operation. No private custody IO or children."""
import asyncio
from contextlib import closing
import hashlib
import json
import logging
import sqlite3
import threading
import traceback
from types import SimpleNamespace as NS

import pytest

pytest_plugins = ['tests.gateway.test_custody_observation']


@pytest.fixture(autouse=True)
def fresh_owner_database(inert_imports, monkeypatch):
    import hermes_state
    # A newly created fixture DB has no previous WAL holder. Keep this suite off host process inventory.
    monkeypatch.setattr(hermes_state, 'refuse_deleted_wal_generation', lambda path: None)


@pytest.fixture
def dispatch(native, monkeypatch):
    import psutil
    from hermes_cli import process_identity
    from gateway.session_admission import admission_fingerprint
    from gateway.session_worker import _SCOPE, worker_request
    from hermes_state_runtime import _secret_digest

    process = NS(is_running=lambda: True, status=lambda: 'running', username=lambda: 'fixture')
    monkeypatch.setattr(psutil, 'Process', lambda *a: process)
    monkeypatch.setattr(process_identity, 'worker_birth', lambda p: 456)
    native.auth._require_admission_open = lambda: None
    params = dict(profile_id=native.obs.ACTOR, session_id=native.ref.session_id,
                  execution_id='admission-worker:synthetic-admission', generation=2,
                  pid=123, birth=456, secret='fixture-worker-secret', epoch=1, sequence=1,
                  operation='custody.maker_observation')
    actor = NS(profile_id=native.obs.ACTOR, subject='synthetic-principal', capabilities={'worker:adopt'})
    connection = NS(authority=native.auth, actor=actor)
    claim = admission_fingerprint(canonical_target=native.ref.session_id,
        payload={k: params[k] for k in _SCOPE} | {'principal': actor.subject})
    native.owner.execute('UPDATE worker_executions SET adoption_digest=?', (_secret_digest(claim),))
    native.owner.execute('UPDATE session_admissions SET request_id=?', ('kanban:["fleet","t_2b29710a",7]',))
    native.owner.commit()

    async def send(payload, **changes):
        return await worker_request(connection, native.ref, params | {'payload': payload} | changes,
                                    operation='persist')
    return NS(send=send, params=params, actor=actor, connection=connection, native=native)


def test_observe_real_receipt_without_capture(dispatch):
    result = asyncio.run(dispatch.send({'phase': 'observe'}))
    assert result['status'] == 'native_observer_unavailable'
    stored = dispatch.native.owner.execute('SELECT result_json FROM worker_receipts').fetchone()
    assert json.loads(stored[0]) == result


NAMES = {'fleet-relay-claude': 'FIXTURE_CLAUDE_NAME', 'fleet-relay-openai': 'FIXTURE_OPENAI_NAME'}
ATTACH = {'phase': 'attach', 'names': NAMES}


@pytest.fixture
def custody_source(dispatch, tmp_path, monkeypatch):
    from gateway import session_custody_operation as operation
    source = tmp_path / 'maker_custody.py'
    source.write_text('def association(provider, name, config_path, field_path):\n'
                      '    return True\n'
                      'def compare(provider, name, final_env):\n'
                      '    return final_env.get(name) == "fixture-private-value"\n')
    # Test-only loader seam: never supplied by a receipt, environment or runtime setting.
    monkeypatch.setattr(operation, '_SOURCE', source)
    return source


def assert_private(*values):
    rendered = '\n'.join(str(v) for v in values)
    assert all(name not in rendered for name in NAMES.values())
    assert 'fixture-private-value' not in rendered


def test_attach_observe_detach_real_receipts(dispatch, custody_source, caplog):
    from tests.gateway.test_custody_observation import maker_attempt
    caplog.set_level(logging.INFO)
    async def scenario():
        assert await dispatch.send(ATTACH) == {'attached': True}
        capture = dispatch.native.auth.runner._maker_custody_capture
        assert await dispatch.send(ATTACH) == {'attached': True}  # exact receipt replay
        assert dispatch.native.auth.runner._maker_custody_capture is capture
        assert await dispatch.send(ATTACH, sequence=2) == {'refused': 'already_attached'}
        observed = await dispatch.send({'phase': 'observe'}, sequence=3)
        assert observed['status'] == 'projected_witnesses'
        assert [r['maker_launch_available'] for r in observed['rows']] == [None, None]
        assert await dispatch.send({'phase': 'detach'}, sequence=4) == {'detached': True}
        assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')
        maker, attempt, _scope = maker_attempt(dispatch.native, capture)
        maker.runner = dispatch.native.auth.runner
        _process, launch = dispatch.native.obs.capture_spawn(
            maker, lambda *a, **kw: object(), [], env=dict.fromkeys(NAMES.values(), 'fixture-private-value'),
            attempt=attempt)
        assert launch is None
        assert all(capture.witnesses(p, n) == [] for p, n in NAMES.items())
        assert_private(observed, caplog.text)
        assert await dispatch.send({'phase': 'observe'}, sequence=5) == dispatch.native.obs.observe_admitted(
            dispatch.native.auth, dispatch.native.ref, 'synthetic-admission')
    asyncio.run(scenario())
    assert hashlib.sha256(custody_source.read_bytes()).hexdigest() in caplog.text
    assert_private(list(dispatch.native.owner.execute('SELECT result_json FROM worker_receipts')), caplog.text)


@pytest.mark.parametrize('actor', ['other', 'authority'])
def test_wrong_actor_denied_before_loading(dispatch, monkeypatch, actor):
    from gateway import session_custody_operation as operation
    monkeypatch.setattr(operation, '_load_custody_source', lambda: pytest.fail('unauthorized source read'))
    if actor == 'other':
        dispatch.actor.profile_id = '/fixture/other'
    else:
        dispatch.native.auth.profile_id = '/fixture/other'
    with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='permission_denied'):
        asyncio.run(dispatch.send(ATTACH))
    assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')


@pytest.mark.parametrize('field,value', [
    ('task_id', 'other'), ('tenant', 'other'), ('board', 'other'), ('db', '/fixture/other.db'),
])
def test_correct_actor_wrong_admission_denied(dispatch, monkeypatch, field, value):
    from gateway import session_custody_operation as operation
    monkeypatch.setattr(operation, '_load_custody_source', lambda: pytest.fail('unauthorized source read'))
    dispatch.native.context[field] = value
    dispatch.native.policy.kanban_json = json.dumps(dispatch.native.context)
    with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='permission_denied'):
        asyncio.run(dispatch.send(ATTACH))


@pytest.mark.parametrize('table,column,value', [
    ('tasks', 'current_run_id', 8), ('tasks', 'claim_expires', 0), ('task_runs', 'claim_expires', 0),
    ('task_runs', 'ended_at', 1), ('tasks', 'claim_lock', 'other'),
])
def test_stale_claim_denied_before_loading(dispatch, monkeypatch, table, column, value):
    from gateway import session_custody_operation as operation
    monkeypatch.setattr(operation, '_load_custody_source', lambda: pytest.fail('stale source read'))
    dispatch.native.board.execute(f'UPDATE {table} SET {column}=?', (value,))
    dispatch.native.board.commit()
    with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='stale_kanban_claim'):
        asyncio.run(dispatch.send(ATTACH))


def test_wrong_native_request_denied(dispatch, custody_source):
    dispatch.native.owner.execute('UPDATE session_admissions SET request_id="other"')
    dispatch.native.owner.commit()
    with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='permission_denied'):
        asyncio.run(dispatch.send(ATTACH))


@pytest.mark.parametrize('payload', [
    {'phase': 'observe', 'extra': True}, {'phase': 'detach', 'names': NAMES},
    ATTACH | {'extra': True}, {'phase': 'attach', 'names': NAMES | {'extra': 'BAD'}},
    {'phase': 'attach', 'names': {'fleet-relay-claude': 'VALID_NAME'}},
    *({'phase': 'attach', 'names': NAMES | {'fleet-relay-claude': name}}
      for name in ('A', '_BAD', 'lowercase', 'A-B', 'A' * 129, 'A\n', 1)),
    {'phase': 'launch'}, [],
])
def test_closed_payloads(dispatch, payload, monkeypatch):
    from gateway import session_custody_operation as operation
    monkeypatch.setattr(operation, '_load_custody_source', lambda: pytest.fail('invalid source read'))
    with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='invalid_params'):
        asyncio.run(dispatch.send(payload))


@pytest.mark.parametrize('source,reason', [
    (None, 'custody_source_unavailable'), ('this is invalid python !', 'custody_source_unavailable'),
    ('raise ValueError("FIXTURE_CLAUDE_NAME fixture-private-value")', 'custody_source_unavailable'),
    ('association = None\ncompare = None', 'custody_source_unavailable'),
    ('def association(*a): return False\ndef compare(*a): return True', 'association_refused'),
    ('def association(*a): return 1\ndef compare(*a): return True', 'association_refused'),
    ('def association(provider, *a): return provider == "fleet-relay-claude"\n'
     'def compare(*a): return True', 'association_refused'),
    ('def association(*a): raise ValueError("FIXTURE_CLAUDE_NAME fixture-private-value")\n'
     'def compare(*a): return True', 'association_refused'),
])
def test_unavailable_or_refused_source_is_private(dispatch, custody_source, source, reason, caplog):
    if source is None:
        custody_source.unlink()
    else:
        custody_source.write_text(source)
    result = asyncio.run(dispatch.send(ATTACH))
    assert result == {'refused': reason}
    assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')
    assert_private(result, caplog.text)


def test_module_callbacks_hold_owner_and_board_fences(dispatch, custody_source, monkeypatch):
    from gateway import session_custody_operation as operation
    loader = operation._load_custody_source
    calls = []
    def load_fixture():
        module, digest = loader()
        def association(provider, name, config_path, field_path):
            assert dispatch.native.owner.in_transaction
            assert config_path == dispatch.native.obs.CONFIG
            assert field_path == ('providers', provider, 'api_key')
            with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='permission_denied'):
                dispatch.native.auth.db._execute_write(lambda c: None)
            with closing(sqlite3.connect(dispatch.native.context['db'], timeout=0)) as board:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    board.execute('UPDATE tasks SET status="done"')
            calls.append(provider)
            return True
        module.association = association
        return module, digest
    monkeypatch.setattr(operation, '_load_custody_source', load_fixture)
    assert asyncio.run(dispatch.send(ATTACH)) == {'attached': True}
    assert calls == ['fleet-relay-claude', 'fleet-relay-openai']


def test_malicious_comparison_exception_is_redacted(dispatch, custody_source, caplog):
    from tests.gateway.test_custody_observation import maker_attempt
    custody_source.write_text('def association(*a): return True\n'
        'def compare(*a): raise ValueError("FIXTURE_CLAUDE_NAME fixture-private-value")\n')
    async def scenario():
        assert await dispatch.send(ATTACH) == {'attached': True}
        capture = dispatch.native.auth.runner._maker_custody_capture
        maker, attempt, _scope = maker_attempt(dispatch.native, capture)
        with pytest.raises(dispatch.native.obs.RuntimeStoreError, match='invalid_custody_comparison') as caught:
            dispatch.native.obs.capture_spawn(maker, lambda *a, **k: pytest.fail('spawn after comparison failure'),
                                            [], env={}, attempt=attempt)
        assert_private(''.join(traceback.format_exception(caught.value)), caplog.text)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['epoch', 'generation', 'claim', 'run_end'])
def test_stale_observe_forgets_capture_and_replay_denied(dispatch, custody_source, change):
    async def scenario():
        assert await dispatch.send(ATTACH) == {'attached': True}
        if change == 'epoch':
            dispatch.native.auth.epoch = 2
        elif change == 'generation':
            dispatch.native.auth.executing.clear()
        else:
            column, value = ('claim_expires', 0) if change == 'claim' else ('status', 'done')
            dispatch.native.board.execute(f'UPDATE tasks SET {column}=?', (value,))
            dispatch.native.board.commit()
        with pytest.raises(dispatch.native.obs.RuntimeStoreError):
            await dispatch.send({'phase': 'observe'}, sequence=2)
        assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')
        with pytest.raises(dispatch.native.obs.RuntimeStoreError):
            await dispatch.send(ATTACH)  # replay cannot revive a dead admission
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value,reason', [
    ('secret', 'other-worker-secret', 'permission_denied'), ('generation', 3, 'stale_generation'),
    ('epoch', 2, 'stale_epoch'), ('sequence', 2, 'invalid_params'),
])
def test_worker_receipt_claim_and_sequence_fences(dispatch, custody_source, field, value, reason):
    with pytest.raises(dispatch.native.obs.RuntimeStoreError, match=reason):
        asyncio.run(dispatch.send(ATTACH, **{field: value}))
    assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')


@pytest.mark.parametrize('change', ['epoch', 'claim', 'run_end'])
def test_idle_guard_clears_without_next_operation(dispatch, custody_source, monkeypatch, change):
    from gateway import session_custody_operation as operation
    original = operation.forget_custody
    async def scenario():
        forgotten = asyncio.Event()
        loop = asyncio.get_running_loop()
        def forget(*args, **kwargs):
            original(*args, **kwargs)
            loop.call_soon_threadsafe(forgotten.set)
        monkeypatch.setattr(operation, 'forget_custody', forget)
        assert await dispatch.send(ATTACH) == {'attached': True}
        if change == 'epoch':
            dispatch.native.auth.epoch = 2
        else:
            column, value = ('claim_expires', 0) if change == 'claim' else ('status', 'done')
            dispatch.native.board.execute(f'UPDATE tasks SET {column}=?', (value,))
            dispatch.native.board.commit()
        await asyncio.wait_for(forgotten.wait(), 5)
        assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')
    asyncio.run(scenario())


def test_admission_exit_clears_capture_without_guard(dispatch, custody_source, monkeypatch):
    from gateway import session_custody_operation as operation
    from gateway import session_finite
    from gateway.session_authority import SessionAuthority
    monkeypatch.setattr(operation, 'arm_custody_guard', lambda a: None)
    async def execute(*args):
        return 'fixture-completed'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    async def scenario():
        assert await dispatch.send(ATTACH) == {'attached': True}
        result = await SessionAuthority._execute_claimed(dispatch.native.auth, dispatch.native.ref,
            NS(review=NS(), event_stream=NS(lock=threading.Lock())),
            {'admission_id': 'synthetic-admission'})
        assert result == ('fixture-completed', 'completed')
        assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')
    asyncio.run(scenario())


def test_owner_stop_clears_capture_without_guard(dispatch, custody_source, monkeypatch):
    from gateway import run_runtime, session_custody_operation as operation
    from gateway.session_runtime_workers import stop_authority_work
    monkeypatch.setattr(operation, 'arm_custody_guard', lambda a: None)
    monkeypatch.setattr(run_runtime, 'stop_authority_turns', lambda *a, **kw: None)
    async def scenario():
        assert await dispatch.send(ATTACH) == {'attached': True}
        stop_authority_work(dispatch.native.auth)
        assert not hasattr(dispatch.native.auth.runner, '_maker_custody_capture')
    asyncio.run(scenario())


@pytest.mark.parametrize('comparison', [True, False, None])
def test_dispatch_projects_native_maker_comparison(dispatch, custody_source, comparison, monkeypatch):
    from gateway import session_custody_operation as operation
    from tests.gateway.test_custody_observation import maker_attempt
    # This synthetic maker shares a ledger row with custody; idle expiry has its own tests.
    monkeypatch.setattr(operation, 'arm_custody_guard', lambda a: None)
    custody_source.write_text('def association(*a): return True\n'
                             f'def compare(provider, name, final_env): return {comparison!r}\n')
    native = dispatch.native
    async def scenario():
        assert await dispatch.send(ATTACH) == {'attached': True}
        capture = native.auth.runner._maker_custody_capture
        maker, attempt, scope = maker_attempt(native, capture)
        maker.runner = native.auth.runner
        process, launch = native.obs.capture_spawn(maker, lambda *a, **kw: object(), [],
            env=dict.fromkeys(NAMES.values(), 'fixture-private-value'), attempt=attempt)
        capture.reserved(maker, launch, scope, process)
        capture.ready(maker, launch, scope)
        maker._managed_workers = {scope['session_id']: NS(process=process, _custody_launch=launch,
                                                        _reservation_identity=launch.reservation)}
        native.owner.execute('UPDATE worker_executions SET status="terminal"')
        native.owner.commit()
        capture.succeeded(maker, launch, scope)
        native.owner.execute('UPDATE worker_executions SET status="running"')
        native.owner.commit()
        result = await dispatch.send({'phase': 'observe'}, sequence=2)
        assert [r['maker_launch_available'] for r in result['rows']] == [comparison, comparison]
        assert [r['prospective_inherited_clean_child_available'] for r in result['rows']] == [None, None]
        assert_private(result)
    asyncio.run(scenario())
