import json
import os

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


def test_outbox_freezes_failed_payload_and_reopens_exclusively(tmp_path):
    scope = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}
    rows = [{'role': 'user', 'content': 'frozen'}]
    def unavailable(method, **params):
        rows[0]['content'] = 'caller changed'
        raise TimeoutError('offline')
    store = RuntimeSessionStore(unavailable, scope, tmp_path / 'private')
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch('s', rows)
        disk = json.loads(store.path.read_text())
        assert disk['pending'][0]['payload']['messages'][0]['content'] == 'frozen'
        assert store.journal['pending'] == disk['pending']
        with pytest.raises(WorkerPersistenceError, match='outbox_in_use'):
            RuntimeSessionStore(unavailable, scope, tmp_path / 'private')
        with pytest.raises(WorkerPersistenceError, match='offline'):
            store.queue_token_counts('s', input_tokens=1)
    finally:
        with pytest.raises(WorkerPersistenceError, match='offline'):
            store.close()
    seen = []
    def commit(method, **params):
        seen.append(params)
        return {'count': 1, 'annotations': [{'_row_id': 42}]}
    restored = RuntimeSessionStore(commit, scope, tmp_path / 'private')
    try:
        assert restored.retry_pending() == []  # reopening already reconciled the acknowledged journal
        assert seen[0]['payload']['messages'][0]['content'] == 'frozen'
        assert json.loads(restored.path.read_text())['pending'] == []
    finally:
        restored.close()


@pytest.mark.platforms('posix')
def test_outbox_has_private_posix_mode(tmp_path):
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    try:
        assert store.path.stat().st_mode & 0o077 == 0
    finally:
        store.close()


def test_outbox_capacity_failure_does_not_advance_or_discard(tmp_path):
    def unavailable(*args, **kwargs):
        raise TimeoutError('offline')
    store = RuntimeSessionStore(unavailable, {'session_id': 's'}, tmp_path / 'private', max_bytes=500)
    try:
        before = store.path.read_bytes()
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'x' * 1000}])
        assert store.path.read_bytes() == before
        assert store.failure == 'outbox_full'
    finally:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.close()


def test_worker_turn_wait_callbacks_remain_local(tmp_path):
    attempts, notices = [], []
    def rpc(method, **params):
        attempts.append(params)
        return {'value': len(attempts) > 1}
    store = RuntimeSessionStore(rpc, {'session_id': 's'}, tmp_path / 'private')
    try:
        assert store.acquire_session_turn_lease('s', 'holder', wait_seconds=3,
            poll_interval_seconds=.01, on_wait=notices.append)
        assert notices and len(attempts) == 2
        assert all(set(row['payload']) == {'holder', 'ttl_seconds'} for row in attempts)
        assert not store.acquire_session_turn_lease('s', 'holder', should_abort=lambda: True)
    finally:
        store.close()


def test_outbox_filesystem_failure_is_sticky(tmp_path, monkeypatch):
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    def fail_replace(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('agent.runtime_session_store.os.replace', fail_replace)
    try:
        with pytest.raises(OSError, match='disk unavailable'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.failure == 'disk unavailable'
    finally:
        store._outbox_owner.close()


def test_outbox_journal_survives_a_cross_device_rename(tmp_path, monkeypatch):
    """The journal publishes through utils.atomic_replace: a bind-mounted outbox whose rename
    fails EXDEV falls back to copy+fsync instead of failing every worker write, and the
    journal stays owner-only (POSIX mode bits; Windows has none to check)."""
    import errno
    import stat
    real_replace = os.replace
    def exdev_replace(src, dst):
        if str(dst).endswith('pending.json'):
            raise OSError(errno.EXDEV, 'cross-device link')
        return real_replace(src, dst)
    monkeypatch.setattr(os, 'replace', exdev_replace)
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    try:
        journal = store.path
        assert json.loads(journal.read_text())['scope'] == {'session_id': 's'}
        if os.name == 'posix':
            assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert [p.name for p in journal.parent.iterdir() if p.name.startswith('.pending-')] == []
    finally:
        store._outbox_owner.close()


def test_worker_store_persists_and_restores_the_tools_prefix_pin(tmp_path):
    """persist_agent_tool_names writes the pin through the worker facade and a rebuilt worker
    agent reads it back from the same row (F26). Without the facade method the pin stays NULL
    and a config flip between turns silently forks the cached tools[] prefix."""
    from types import SimpleNamespace
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
    from tools.mcp_tool_agent import persist_agent_tool_names
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('owned', 'cli', system_prompt='prefix')
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='test-secret')
        store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
        try:
            tools = [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
                     for n in ('terminal', 'tool_search', 'tool_describe', 'tool_call')]
            persist_agent_tool_names(SimpleNamespace(_session_db=store, session_id='owned', tools=tools))
            db.create_session('direct', 'cli', system_prompt='prefix')
            persist_agent_tool_names(SimpleNamespace(_session_db=db, session_id='direct', tools=tools))
            # The same pin the direct store writes (the RPC canonicalizes key order for its receipt
            # digest): a worker-pinned session rebuilt in-process reads back version + tools intact.
            assert json.loads(db.get_session('owned')['tool_names']) == json.loads(db.get_session('direct')['tool_names'])
            assert json.loads(store.get_session('owned')['tool_names'])['tools'] == tools
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.update_session_tool_names('foreign', ['terminal'])
            with pytest.raises(Exception, match='invalid_params'):
                store.update_session_tool_names('owned', [{'not': 'a name'}])
        finally:
            store._outbox_owner.close()
    finally:
        db.close()


@pytest.fixture
def receipt_link(tmp_path, monkeypatch):
    """Real owner authentication and receipt transaction; only the ACK can be lost."""
    import asyncio
    import subprocess
    import sys
    import psutil
    from types import SimpleNamespace
    from agent.runtime_session_store import WorkerRPC
    from gateway.session_worker import worker_request
    from hermes_cli.process_identity import worker_birth
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli')
    process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    authority = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='owner'), profile_id='fixture',
        _require_admission_open=lambda: None, authorize=lambda *a: None, wake_after_worker=lambda sid: None,
        sessions={}, logical_owner=lambda sid: sid)
    actor = SimpleNamespace(subject='fixture', profile_id='fixture', capabilities={'worker:adopt'})
    connection = SimpleNamespace(authority=authority, actor=actor)
    scope = dict(profile_id='fixture', execution_id='worker', session_id='owned', generation=0,
                 pid=process.pid, birth=worker_birth(psutil.Process(process.pid)), secret='fixture-only-adoption-secret')
    scope['epoch'] = asyncio.run(worker_request(connection, None, scope | {'kind': 'compute'}, operation='register'))['owner_epoch']
    state = SimpleNamespace(db=db, requests=[], losses=1, override={}, connections=0, after_loss=lambda: None)

    class Link:
        def send(self, request_id, message):
            request = json.loads(message)
            state.requests.append(request)
            params = request['params'] | state.override
            try:
                self.reply = {'result': asyncio.run(worker_request(connection, None, params, operation='persist'))}
            except RuntimeStoreError as exc:
                self.reply = {'error': {'message': exc.reason}}

        def receive(self, timeout):
            if state.losses:
                state.losses -= 1
                state.after_loss()
                raise TimeoutError('worker_receipt_timeout')
            return self.reply

    def connect(self):
        state.connections += 1
        return Link()
    monkeypatch.setattr(WorkerRPC, '_connection', connect)
    state.store = RuntimeSessionStore(WorkerRPC(tmp_path), scope, tmp_path / 'outbox')
    try:
        yield state
    finally:
        state.store._outbox_owner.close()
        process.stdin.close()
        process.wait(timeout=5)
        db.close()


@pytest.mark.parametrize('operation', ['transcript.append', 'usage.main', 'session.activity'])
def test_lost_ack_reconciles_only_the_exact_journaled_receipt(receipt_link, operation):
    state, payload = receipt_link, {
        'transcript.append': {'messages': [{'role': 'assistant', 'content': 'one mutation'}]},
        'usage.main': {'input_tokens': 7},
        'session.activity': {'ts': 123.0, 'description': 'in flight', 'provenance': None},
    }[operation]
    result = state.store._apply(operation, payload)
    assert result is not None
    first, retry = state.requests
    assert first['params'] == retry['params']
    assert first['id'] != retry['id']
    assert state.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
    assert state.db._read_one('SELECT last_sequence FROM worker_executions')[0] == 1
    if operation == 'transcript.append':
        assert [m['content'] for m in state.db.get_messages_as_conversation('owned')] == ['one mutation']
    if operation == 'usage.main':
        assert state.db.get_session('owned')['input_tokens'] == 7
    assert state.store.failure is None and state.store.journal['pending'] == []


def test_unresolved_transport_is_bounded_and_sticky(receipt_link):
    state = receipt_link
    state.losses = 10
    with pytest.raises(WorkerPersistenceError, match='persistence'):
        state.store.queue_token_counts('owned', input_tokens=7)
    assert len(state.requests) == 2
    assert state.store.failure and state.store.journal['pending'][0]['sequence'] == 1
    with pytest.raises(WorkerPersistenceError):
        state.store.touch_session_activity('owned', ts=123)
    assert len(state.requests) == 2
    assert state.db.get_session('owned')['input_tokens'] == 7
    assert state.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1


@pytest.mark.parametrize(('override', 'reason'), [
    ({'generation': 99}, 'stale_generation'), ({'epoch': 99}, 'stale_epoch'),
    ({'session_id': 'foreign'}, 'permission_denied'),
    ({'secret': 'forged-fixture-secret'}, 'permission_denied'),
    ({'profile_id': 'foreign'}, 'profile_mismatch'),
])
def test_owner_denial_never_reconciles(receipt_link, override, reason):
    state = receipt_link
    state.losses, state.override = 0, override
    with pytest.raises(WorkerPersistenceError, match=reason):
        state.store.queue_token_counts('owned', input_tokens=7)
    assert len(state.requests) == 1 and state.store.failure == reason
    assert state.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 0


def test_swallowed_persistence_failure_prevents_the_next_tool(receipt_link):
    from types import SimpleNamespace
    from agent.tool_executor import _run_with_activity_heartbeat
    state = receipt_link
    state.store.failure = 'worker_persistence_transport_unresolved'
    actions = []
    agent = SimpleNamespace(_session_db=state.store)
    with pytest.raises(WorkerPersistenceError):
        _run_with_activity_heartbeat(agent, 'terminal', lambda: actions.append('ran'))
    assert actions == []


@pytest.mark.parametrize(('override', 'reason'), [
    ({'payload': {'input_tokens': 8}}, 'admission_conflict'),
    ({'operation': 'session.activity_clear', 'payload': {}}, 'admission_conflict'),
    ({'generation': 99}, 'stale_generation'),
    ({'epoch': 99}, 'stale_epoch'),
    ({'secret': 'forged-fixture-secret'}, 'permission_denied'),
])
def test_lost_ack_reconnect_denial_remains_sticky(receipt_link, override, reason):
    state = receipt_link
    state.after_loss = lambda: setattr(state, 'override', override)
    with pytest.raises(WorkerPersistenceError, match=reason):
        state.store.queue_token_counts('owned', input_tokens=7)
    assert len(state.requests) == 2 and state.store.failure == reason
    assert state.db.get_session('owned')['input_tokens'] == 7
    assert state.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
    with pytest.raises(WorkerPersistenceError, match=reason):
        state.store.touch_session_activity('owned', ts=123)
    assert len(state.requests) == 2


def test_exhausted_reconciliation_cannot_replenish_its_budget_on_reopen(receipt_link):
    state = receipt_link
    state.losses = 10
    with pytest.raises(WorkerPersistenceError, match='persistence_transport_unresolved'):
        state.store.queue_token_counts('owned', input_tokens=7)
    state.store._outbox_owner.close()
    with pytest.raises(WorkerPersistenceError, match='reconciliation_exhausted'):
        RuntimeSessionStore(state.store.rpc, state.store.scope, state.store.path.parent)
    assert len(state.requests) == 2


def test_persist_timings_contain_only_safe_coordinates(receipt_link, caplog):
    import logging
    caplog.set_level(logging.INFO)
    state = receipt_link
    state.store.touch_session_activity('owned', ts=123, description='never-log-payload')
    messages = [r.message for r in caplog.records if r.message.startswith('worker.persist')]
    assert any('verified execution=worker' in m and 'verify_ms=' in m for m in messages)
    assert any('transaction_dispatch execution=worker' in m and 'executor_queue_ms=' in m for m in messages)
    assert any('transaction execution=worker' in m and 'queue_ms=' in m and 'commit_return_ms=' in m for m in messages)
    assert 'never-log-payload' not in caplog.text and state.store.scope['secret'] not in caplog.text


@pytest.mark.parametrize('operation', ['child.reserve', 'delegation.dispatch', 'compression.publish'])
def test_sibling_receipts_reconcile_without_duplicate_child_or_ledger(receipt_link, operation):
    state, store = receipt_link, receipt_link.store
    if operation == 'delegation.dispatch':
        import sqlite3
        from contextlib import closing
        from tools.async_delegation import _initialize_schema
        with closing(sqlite3.connect(state.db.db_path)) as conn:
            _initialize_schema(conn)
        payload = dict(delegation_id='deleg_fixture', task={'goal': 'inert'}, dispatched_at=123.0,
                       parent_session_id='owned', session_key='', origin_ui_session_id='', origin_session_id='')
    elif operation == 'child.reserve':
        payload = {'secret': 'fixture-child-reservation-secret'}
    else:
        state.losses = 0
        store.append_messages_batch('owned', [{'role': 'user', 'content': 'before'}])
        assert store.try_acquire_compression_lock('owned', 'holder')
        watermark = store.get_active_message_watermark('owned')
        payload = dict(parent_session_id='owned', child_session_id='child', source='cli',
            messages=[{'role': 'assistant', 'content': 'summary'}], system_prompt='prefix',
            compression_lock_holder='holder', watermark=watermark)
    before = state.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0]
    start = len(state.requests)
    state.losses = 1
    if operation == 'compression.publish':
        store.publish_compression_child(**payload)
        assert store.scope['session_id'] == store.journal['scope']['session_id'] == 'child'
        assert [m['content'] for m in state.db.get_messages_as_conversation('child')] == ['summary']
        assert state.db.get_session('owned')['end_reason'] == 'compression'
    else:
        result = store._apply(operation, payload)
        if operation == 'child.reserve':
            assert state.db._read_one("SELECT COUNT(*) FROM worker_executions WHERE kind='child'")[0] == 1
            assert state.db.get_session(result['child']['session_id'])['parent_session_id'] == 'owned'
        else:
            assert result == {'value': None}
            assert state.db._read_one('SELECT COUNT(*) FROM async_delegations')[0] == 1
    first, retry = state.requests[start:]
    assert first['params'] == retry['params']
    assert state.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == before + 1
    assert store.failure is None and store.journal['pending'] == []


def test_successful_receipt_uses_one_request(receipt_link):
    state = receipt_link
    state.losses = 0
    state.store.append_messages_batch('owned', [{'role': 'assistant', 'content': 'success'}])
    assert len(state.requests) == 1
    assert state.store.failure is None and state.store.journal['pending'] == []
    assert [m['content'] for m in state.db.get_messages_as_conversation('owned')] == ['success']



def test_persistence_failure_never_enters_provider_recovery(receipt_link, monkeypatch):
    from types import SimpleNamespace
    from agent.turn_api_error import handle_api_error
    state = receipt_link
    state.store.failure = 'worker_persistence_transport_unresolved'
    def provider_recovery(*args, **kwargs):
        pytest.fail('provider recovery must not run after persistence failure')
    monkeypatch.setattr('agent.turn_api_error.recover_before_classification', provider_recovery)
    verdict = handle_api_error(SimpleNamespace(_session_db=state.store, thinking_callback=None),
        api_error=WorkerPersistenceError(state.store.failure), _retry=None, thinking_spinner=None,
        messages=[], api_messages=[], api_kwargs={}, system_message='', active_system_prompt='',
        conversation_history=[], approx_tokens=0, retry_count=0, max_retries=3, compression_attempts=0,
        max_compression_attempts=1, api_call_count=1, api_request_id='fixture', api_start_time=0,
        effective_task_id='fixture', turn_id='fixture')
    assert verdict.action == 'return'
    assert verdict.result['failure_reason'] == 'persistence' and verdict.result['failure_retryable'] is False


def test_activity_failure_prevents_checkpoint_commands_before_tool_dispatch(receipt_link, monkeypatch):
    from types import SimpleNamespace
    from agent.tool_executor import _begin_tool_execution
    state, checkpoints = receipt_link, []
    agent = SimpleNamespace(_session_db=state.store, quiet_mode=True, tool_progress_mode='off', tool_progress_callback=None,
        tool_start_callback=None, _checkpoint_mgr=SimpleNamespace(enabled=True),
        _touch_activity=lambda *a: setattr(state.store, 'failure', 'worker_persistence_transport_unresolved'))
    monkeypatch.setattr('agent.tool_executor._ensure_file_checkpoint', lambda *a: checkpoints.append('checkpoint'))
    ref = SimpleNamespace(name='write_file', args={'path': 'fixture'}, task_id='fixture', call_id='fixture')
    with pytest.raises(WorkerPersistenceError):
        _begin_tool_execution(agent, ref, None)
    assert checkpoints == []
