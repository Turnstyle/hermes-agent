"""Exact owner recovery preserves history and credential guards, including an already-correct outer owner."""

import itertools
import json
from types import SimpleNamespace

import pytest

from gateway.session_policy import policy_for_source, restore_policy
from gateway.session_policy_credentials import PREFIX, recover_config_secrets
from persistence.conversation.lineage import CompressionChild
from persistence.conversation.messages import NewMessage
from persistence.core.errors import AuthorityRefusal
from tests.fixtures.storage_holder import policy_key, raw_holder
from tests.fixtures.storage_read import session_row
from tests.fixtures.storage_seed import seed_session

METHOD = 'session.recover_config_secret_provenance'
SID = 'retained-local-history'
LEGACY_KEY = 'gateway.local_legacy.v1:' + SID
PREIMAGE_KEY = 'gateway.config_secret_provenance.v1:' + SID


def _state(store):
    with raw_holder(store.persistence)._read_ctx() as conn:
        return dict(conn.execute('SELECT key,value FROM state_meta WHERE key IN (?,?,?)',
                                 (policy_key(SID), LEGACY_KEY, PREIMAGE_KEY)))


def _history(store, session_id=SID):
    with raw_holder(store.persistence)._read_ctx() as conn:
        return [tuple(row) for row in conn.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id', (session_id,))]


def _compress_lineage(db):
    lineage = [SID, 'compressed-middle', 'compressed-tip']
    for parent, child in itertools.pairwise(lineage):
        assert db.coordination.try_acquire_compression_lock(session_id=parent, holder='fixture')
        try:
            db.lineage.publish_compression_child(parent,
                CompressionChild(session_id=child, source=db.sessions.get(parent).source),
                [NewMessage(role='user', content='retained user'),
                 NewMessage(role='assistant', content='retained assistant')],
                compression_lock_holder='fixture')
        finally:
            db.coordination.release_compression_lock(session_id=parent, holder='fixture')
    assert list(db.bindings.receipt(SID).lineage) == lineage
    return lineage


async def _fixture(tmp_path, monkeypatch, outer='current', count=2):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.owner_identity import host_owner_subject
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    home = tmp_path / 'profiles' / 'current'
    home.mkdir(parents=True)
    old_home = str(home.with_name('previous'))
    monkeypatch.setenv('HERMES_HOME', str(home))
    config = {'model': {'default': 'fixture-model', 'provider': 'custom',
                        'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'synthetic-route-value'},
              'platform_toolsets': {'cli': []}, 'checkpoints': {'enabled': False}}
    (home / 'config.yaml').write_text(json.dumps(config))
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a, **k: config)
    store = SessionStore(home / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store.persistence, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None, _evict_cached_agent=lambda route: None,
                             _resolve_session_agent_runtime=lambda **k: ('fixture-model', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    owner = await initialize_session_authority(runner, profile_id=str(home), instance_id='fixture')
    peer = AuthorityConnection(owner, object(), {'provider': 'local', 'user_id': host_owner_subject(),
        'profile_id': str(home), 'instance_id': 'fixture', 'native_bootstrap': True}, operator=True)
    try:
        seed_session(store.persistence, SID, 'cli', model='fixture-model', cwd=str(tmp_path))
        store.persistence.messages.append_batch(SID, [NewMessage(
            role='user' if i % 2 == 0 else 'assistant', content=f'baseline-{i}') for i in range(count)])
        resumed = await peer.dispatch({'id': 1, 'method': 'session.resume',
                                      'params': {'session_id': SID, 'omit_messages': True}})
        assert 'result' in resumed, resumed

        def stale(conn):
            receipt = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (policy_key(SID),)).fetchone()[0])
            source = json.loads(receipt['policy']['config_secret_ref'][len(PREFIX):])
            source.update(home=old_home, profile=old_home)
            receipt['policy']['config_secret_ref'] = PREFIX + json.dumps(source)
            receipt['extra_receipt_field'] = {'retained': [1, 2, 3]}
            if outer == 'old':
                receipt['profile_id'] = old_home
                legacy = {k: receipt[k] for k in ('session_id', 'profile_id', 'principal_id', 'legacy_session_id')}
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(legacy), LEGACY_KEY))
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), policy_key(SID)))
        raw_holder(store.persistence)._execute_write(stale)
        # Simulate the stale policy that the running owner has already restored.
        from gateway.session_local import publish_local_policy
        publish_local_policy(owner, SID, evict=False)
        getattr(owner, '_local_config_secrets', {}).clear()
        return store, peer, old_home, config
    except BaseException:
        await peer.close()
        store.close_all_db_handles()
        raise


async def _rpc(peer, old_home, action='apply', **extra):
    return await peer.dispatch({'id': 2, 'method': METHOD,
                               'params': {'session_id': SID, 'old_home': old_home, 'action': action, **extra}})


@pytest.mark.asyncio
@pytest.mark.parametrize('outer', ['current', 'old'])
async def test_exact_owner_recovers_and_rolls_back_without_changing_history(tmp_path, monkeypatch, outer):
    store, peer, old_home, _config = await _fixture(tmp_path, monkeypatch, outer, count=3444)
    owner = peer.authority
    try:
        before, history = _state(store), _history(store)
        assert len(history) == 3444
        stale = restore_policy(store.persistence.bindings.receipt(SID).policy)
        with pytest.raises(AuthorityRefusal, match='launch_credentials_unavailable'):
            recover_config_secrets(owner, stale, skip=())
        preview = await _rpc(peer, old_home, 'preview')
        assert preview.get('result', {}).get('status') == 'preview', preview
        assert _state(store) == before
        events = []
        owner.sessions[SID].event_stream.observers.add(events.append)
        applied = await _rpc(peer, old_home)
        assert applied.get('result', {}).get('status') == 'applied', applied
        after = _state(store)
        assert PREIMAGE_KEY in after
        preimage = json.loads(after[PREIMAGE_KEY])
        assert preimage['before'] == [before[policy_key(SID)], before[LEGACY_KEY]]
        previous, current = json.loads(before[policy_key(SID)]), json.loads(after[policy_key(SID)])
        old_ref = json.loads(previous['policy']['config_secret_ref'][len(PREFIX):])
        new_ref = json.loads(current['policy']['config_secret_ref'][len(PREFIX):])
        assert new_ref == {**old_ref, 'home': owner.profile_id, 'profile': owner.profile_id}
        expected = {**previous, 'profile_id': owner.profile_id,
                    'policy': {**previous['policy'], 'config_secret_ref': current['policy']['config_secret_ref']}}
        assert current == expected
        published = policy_for_source(owner.runner, owner.sessions[SID].source)
        assert published.config_secret_ref == current['policy']['config_secret_ref']
        recovered = recover_config_secrets(owner, published, skip=())
        assert set(recovered) == {('model', 'api_key')}
        from agent.credential_persistence import fingerprint_secret_value
        # Compare fingerprints so even a failed assertion never prints a synthetic secret.
        assert fingerprint_secret_value(recovered[('model', 'api_key')]) == new_ref['entries'][0][1]
        assert any(frame['params']['type'] == 'session.updated' for frame in events)
        assert _history(store) == history
        repeated = await _rpc(peer, old_home)
        assert repeated.get('result', {}).get('status') == 'unchanged', repeated
        assert _state(store) == after
        rolled = await _rpc(peer, old_home, 'rollback')
        assert rolled.get('result', {}).get('status') == 'rolled_back', rolled
        restored = _state(store)
        assert {k: restored[k] for k in before} == before
        assert json.loads(restored[PREIMAGE_KEY])['rolled_back'] is True
        assert policy_for_source(owner.runner, owner.sessions[SID].source).config_secret_ref == stale.config_secret_ref
        assert _history(store) == history
        again = await _rpc(peer, old_home, 'rollback')
        assert again.get('result', {}).get('status') == 'unchanged', again
        assert _state(store) == restored
    finally:
        await peer.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('segment,mutation', [
    ('single', 'append'), ('single', 'rewrite'), ('single', 'deactivate'), ('single', 'delete'),
    (SID, 'rewrite'), (SID, 'deactivate'), (SID, 'delete'),
    ('compressed-middle', 'rewrite'), ('compressed-middle', 'deactivate'), ('compressed-middle', 'delete'),
    ('compressed-tip', 'append'), ('compressed-tip', 'rewrite'),
    ('compressed-tip', 'deactivate'), ('compressed-tip', 'delete'),
])
async def test_rollback_refuses_native_transcript_mutations(tmp_path, monkeypatch, segment, mutation):
    store, peer, old_home, _config = await _fixture(tmp_path, monkeypatch)
    db = store.persistence
    try:
        lineage = [SID] if segment == 'single' else _compress_lineage(db)
        target = SID if segment == 'single' else segment
        applied = await _rpc(peer, old_home)
        assert applied.get('result', {}).get('status') == 'applied', applied
        revisions = {sid: session_row(db, sid)['runtime_revision'] for sid in lineage}
        original = db.messages.conversation_revision(lineage[-1], include_lineage=True)
        row_id = db.messages.active_message_ids(target)[0]
        mutations = {
            'append': lambda: db.messages.append_batch(target, [NewMessage(role='user', content='new input')]),
            'rewrite': lambda: db.messages.set_user_message_content(target, row_id, 'rewritten user input'),
            'deactivate': lambda: db.messages.deactivate_message(target, row_id),
            'delete': lambda: db.messages.clear_transcript(target),
        }
        mutations[mutation]()
        assert {sid: session_row(db, sid)['runtime_revision'] for sid in lineage} == revisions
        current = db.messages.conversation_revision(lineage[-1], include_lineage=True)
        assert current != original
        if mutation == 'rewrite':
            assert (current.active_count, current.max_active_id) == (original.active_count, original.max_active_id)
            assert current.transcript_revision > original.transcript_revision
        before = _state(store)
        history = {sid: _history(store, sid) for sid in lineage}
        generation = session_row(db, SID)['runtime_generation']
        reply = await _rpc(peer, old_home, 'rollback')
        assert reply.get('error', {}).get('data', {}).get('reason') == 'revision_conflict', reply
        assert _state(store) == before
        assert {sid: _history(store, sid) for sid in lineage} == history
        assert session_row(db, SID)['runtime_generation'] == generation
    finally:
        await peer.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unchanged_compressed_lineage_can_roll_back(tmp_path, monkeypatch):
    store, peer, old_home, _config = await _fixture(tmp_path, monkeypatch)
    try:
        lineage = _compress_lineage(store.persistence)
        before = _state(store)
        history = {sid: _history(store, sid) for sid in lineage}
        applied = await _rpc(peer, old_home)
        assert applied.get('result', {}).get('status') == 'applied', applied
        rolled = await _rpc(peer, old_home, 'rollback')
        assert rolled.get('result', {}).get('status') == 'rolled_back', rolled
        restored = _state(store)
        assert {key: restored[key] for key in before} == before
        assert {sid: _history(store, sid) for sid in lineage} == history
        again = await _rpc(peer, old_home, 'rollback')
        assert again.get('result', {}).get('status') == 'unchanged', again
        assert _state(store) == restored
    finally:
        await peer.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('rolled_back', [False, True])
async def test_rollback_refuses_preimage_without_transcript_witness(tmp_path, monkeypatch, rolled_back):
    store, peer, old_home, _config = await _fixture(tmp_path, monkeypatch)
    try:
        applied = await _rpc(peer, old_home)
        assert applied.get('result', {}).get('status') == 'applied', applied
        if rolled_back:
            rolled = await _rpc(peer, old_home, 'rollback')
            assert rolled.get('result', {}).get('status') == 'rolled_back', rolled

        def remove_witness(conn):
            record = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (PREIMAGE_KEY,)).fetchone()[0])
            record.pop('transcript_witness', None)
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(record), PREIMAGE_KEY))
        raw_holder(store.persistence)._execute_write(remove_witness)
        before, history = _state(store), _history(store)
        generation = session_row(store.persistence, SID)['runtime_generation']
        reply = await _rpc(peer, old_home, 'rollback')
        assert reply.get('error', {}).get('data', {}).get('reason') == 'revision_conflict', reply
        assert _state(store) == before
        assert _history(store) == history
        assert session_row(store.persistence, SID)['runtime_generation'] == generation
    finally:
        await peer.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason', [
    ('unrelated_home', 'invalid_params'), ('mixed_reference', 'launch_credentials_unavailable'),
    ('reference_session', 'launch_credentials_unavailable'), ('mixed_pair', 'storage_unavailable'),
    ('unrelated_reference', 'launch_credentials_unavailable'),
    ('pair_session', 'storage_unavailable'), ('endpoint', 'launch_credentials_unavailable'),
    ('fingerprint', 'launch_credentials_unavailable'), ('not_operator', 'permission_denied'),
    ('write_failure', 'storage_unavailable'), ('already_good', None),
    ('rollback_conflict', 'revision_conflict'),
    ('queued', 'session_busy'), ('stale_epoch', 'stale_epoch'),
])
async def test_refusals_and_noops_leave_the_paired_state_unchanged(tmp_path, monkeypatch, case, reason):
    store, peer, old_home, config = await _fixture(tmp_path, monkeypatch)
    owner = peer.authority
    try:
        if case == 'rollback_conflict':
            applied = await _rpc(peer, old_home)
            assert applied.get('result', {}).get('status') == 'applied', applied

        def corrupt(conn):
            receipt = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (policy_key(SID),)).fetchone()[0])
            source = json.loads(receipt['policy']['config_secret_ref'][len(PREFIX):])
            if case == 'mixed_reference':
                source['profile'] = owner.profile_id
            elif case == 'unrelated_reference':
                source.update(home=str(tmp_path / 'unrelated'), profile=str(tmp_path / 'unrelated'))
            elif case == 'reference_session':
                source['session'] = 'another-session'
            elif case == 'already_good':
                source.update(home=owner.profile_id, profile=owner.profile_id)
            elif case == 'endpoint':
                frozen = json.loads(receipt['policy']['config_json'])
                frozen['model']['base_url'] = 'http://127.0.0.1:10/v1'
                receipt['policy']['config_json'] = json.dumps(frozen)
            elif case == 'rollback_conflict':
                receipt['extra_receipt_field'] = {'intervening': True}
            receipt['policy']['config_secret_ref'] = PREFIX + json.dumps(source)
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), policy_key(SID)))
            if case in ('mixed_pair', 'pair_session'):
                legacy = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (LEGACY_KEY,)).fetchone()[0])
                legacy['profile_id' if case == 'mixed_pair' else 'session_id'] = 'different'
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(legacy), LEGACY_KEY))
            if case == 'write_failure':
                conn.execute("CREATE TRIGGER refuse_pair_update BEFORE UPDATE ON state_meta "
                             "WHEN OLD.key='gateway.local_legacy.v1:retained-local-history' "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        raw_holder(store.persistence)._execute_write(corrupt)
        if case == 'fingerprint':
            config['model']['api_key'] = 'synthetic-rotated-value'
        if case == 'not_operator':
            peer.native_owner = False
        if case == 'queued':
            store.persistence.admissions.admit(epoch=owner.epoch, principal_id=peer.actor.subject,
                session_id=SID, request_id='queued-fixture', payload={'text': 'retained queued input'})
        if case == 'stale_epoch':
            owner.epoch += 1
        if case == 'unrelated_home':
            old_home = str(tmp_path / 'unrelated' / 'previous')
        before, history = _state(store), _history(store)
        reply = await _rpc(peer, old_home, 'rollback' if case == 'rollback_conflict' else 'apply')
        if reason is None:
            assert reply.get('result', {}).get('status') == 'unchanged', reply
        else:
            assert reply.get('error', {}).get('data', {}).get('reason') == reason, reply
        assert _state(store) == before
        assert (PREIMAGE_KEY in before) == (case == 'rollback_conflict')
        assert _history(store) == history
    finally:
        await peer.close()
        store.close_all_db_handles()
