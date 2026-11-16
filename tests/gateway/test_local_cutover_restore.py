"""Canonical LOCAL receipts retain their route when a named bot leaves multiplexing."""
from pathlib import Path

import pytest

from gateway.run import GatewayRunner


async def _owner(home, root, monkeypatch, *, served, instance):
    from gateway.config import GatewayConfig
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import initialize_session_authority
    from hermes_state import SessionDB

    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    config = GatewayConfig()
    config.multiplex_profiles = served
    from gateway.session import SessionStore
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config, runner.adapters, runner._profile_adapters = config, {}, {}
    runner._primary_profile_name = 'default' if served or home == root else home.name
    runner.session_store = SessionStore(home / 'sessions', config)
    runner._session_db, runner._draining = runner.session_store._db, False
    registry = runner.session_authorities = SessionAuthorities(root if served else home, multiplexed=served)
    registry.add(home, None, name=home.name if served else None)
    db = SessionDB(db_path=home / 'state.db')
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id=instance, db=db)
    registry.replace(home, authority)
    authority._schedule = lambda ref: None
    return authority


def _snapshot(db):
    with db._read_ctx() as conn:
        return tuple(conn.iterdump())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cutover', 'secondary', 'named_legacy', 'default'])
async def test_restore_and_admission_keep_the_durable_route(tmp_path, monkeypatch, kind):
    from gateway import run
    from gateway.session_contract import SessionActor, Submission
    from gateway.session_local import create_local_session, authorize_local_source
    from gateway.session_local_recovery import restore_local_session
    from gateway.session_identity import replace_source
    from gateway.session import build_session_key
    from hermes_state_local import local_receipt
    from hermes_state_runtime import list_session_admissions

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.hermes'
    home = root if kind == 'default' else root / 'profiles' / 'tb-woody'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(run, '_load_profile_secret_scope', lambda home: {})
    first = await _owner(home, root, monkeypatch, served=kind in ('cutover', 'secondary'), instance='first')
    actor = SessionActor('owner', str(home), frozenset({'session:create', 'session:read', 'session:submit'}), 'socket')
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home, {}):
        ref = create_local_session(first, actor, {'request_id': 'chat', 'source': 'gui',
            'model': 'frozen', 'cwd': str(tmp_path), 'toolsets': []})
    receipt = local_receipt(first.db, ref.session_id)
    first.db.close()
    cold = await _owner(home, root, monkeypatch, served=kind == 'secondary', instance='cold')
    try:
        assert ref.session_id in cold.sessions, 'cold boot refused the owned receipt'
        cold.sessions.pop(ref.session_id)
        cold.runner.session_store._entries.pop(receipt['route'], None)
        before = _snapshot(cold.db)
        with monkeypatch.context() as reads_only:
            reads_only.setattr(cold.db, '_execute_write', lambda *a, **k: pytest.fail('restore wrote durable state'))
            assert restore_local_session(cold, ref.session_id) == ref
            assert restore_local_session(cold, ref.session_id) == ref
            from gateway.session_local_migration import resolve_local_target
            assert resolve_local_target(cold, actor, ref.session_id) == ref
            assert _snapshot(cold.db) == before
        live = cold.sessions[ref.session_id]
        route = receipt['route']
        expected_ns = 'tb-woody' if kind in ('cutover', 'secondary') else 'main'
        assert route == f'agent:{expected_ns}:local:dm:{ref.session_id}'
        cold.runner._canonicalize(live.source)
        assert cold.runner.session_store._generate_session_key(live.source) == route
        if kind == 'cutover':
            assert build_session_key(replace_source(live.source, message_id='next'), profile='wrong') == route
            from gateway.session import SessionSource
            wire = SessionSource.from_dict({**live.source.to_dict(), '_local_receipt_profile': 'tb-woody'})
            assert cold.runner.session_store._generate_session_key(wire) == f'agent:main:local:dm:{ref.session_id}'
            assert authorize_local_source(cold.runner, wire) is False
            from gateway.session_local_recovery import local_source
            fresh = local_source(cold, 'new-chat', actor.subject)
            assert cold.runner.session_store._generate_session_key(fresh) == 'agent:main:local:dm:new-chat'
        assert authorize_local_source(cold.runner, live.source) is True
        adapter = cold.runner._adapter_for_source(live.source)
        assert adapter._source_session_key(live.source) == route
        forged = replace_source(live.source, user_id='foreign')
        assert authorize_local_source(cold.runner, forged) is False
        admitted = await cold.submit(actor, Submission('input', ref, {'text': 'hello'}, 'queue'))
        assert admitted.status == 'queued'
        claimed, _ = await cold._claim_next(ref, live)
        assert claimed['admission_id'] == admitted.admission_id
        from gateway.session_ingress import execute_admission
        observed = []
        async def handle(event):
            observed.append((event.source, cold.runner.session_store._generate_session_key(event.source)))
            return ''
        monkeypatch.setattr(cold.runner, '_handle_message', handle)
        await execute_admission(cold, ref, claimed)
        assert observed == [(live.source, route)]
        assert list_session_admissions(cold.db, session_id=ref.session_id)[0]['status'] == 'started'
        assert local_receipt(cold.db, ref.session_id) == receipt
        assert cold.db.get_session(ref.session_id)['session_key'] == route
    finally:
        cold.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['foreign', 'alias', 'entry_route', 'origin', 'row_route',
                                    'row_user', 'receipt_home', 'receipt_id', 'authority_home', 'reverse'])
async def test_cutover_refuses_inconsistent_or_foreign_evidence(tmp_path, monkeypatch, damage):
    import json
    from gateway import run
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import restore_local_session
    from hermes_state_local import POLICY_PREFIX, local_receipt
    from hermes_state_runtime import RuntimeStoreError

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.hermes'
    home = root / 'profiles' / 'tb-woody'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(run, '_load_profile_secret_scope', lambda home: {})
    first = await _owner(home, root, monkeypatch, served=damage != 'reverse', instance='first')
    actor = SessionActor('owner', str(home), frozenset({'session:create'}), 'socket')
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home, {}):
        ref = create_local_session(first, actor, {'request_id': 'chat', 'source': 'gui',
            'model': 'frozen', 'cwd': str(tmp_path), 'toolsets': []})
    receipt = local_receipt(first.db, ref.session_id)
    if damage in ('foreign', 'alias'):
        if damage == 'alias':
            (home.parent / 'foreign').symlink_to(home, target_is_directory=True)
        route = receipt['route'].replace('agent:tb-woody:', 'agent:foreign:')
        receipt['route'] = receipt['entry']['session_key'] = route
        receipt['entry']['origin']['profile'] = 'foreign'
        first.db._write_sql('UPDATE sessions SET session_key=? WHERE id=?', (route, ref.session_id))
    receipt_changes = {
        'entry_route': (receipt['entry'], 'session_key', 'agent:main:local:dm:foreign'),
        'origin': (receipt['entry']['origin'], 'profile', 'foreign'),
        'receipt_home': (receipt, 'profile_id', str(root)),
        'receipt_id': (receipt, 'session_id', 'foreign'),
    }
    if change := receipt_changes.get(damage):
        node, key, value = change
        node[key] = value
    row_changes = {'row_route': ('session_key', 'agent:main:local:dm:foreign'),
                   'row_user': ('user_id', 'foreign')}
    if change := row_changes.get(damage):
        column, value = change
        first.db._write_sql(f'UPDATE sessions SET {column}=? WHERE id=?', (value, ref.session_id))
    first.db._write_sql('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + ref.session_id))
    first.db.close()
    cold = await _owner(home, root, monkeypatch, served=damage == 'reverse', instance='cold')
    try:
        if damage == 'authority_home':
            cold.sessions.clear()
            cold.profile_id = str(root)
            receipt['profile_id'] = str(root)
            receipt['legacy_session_id'] = ref.session_id
            cold.db._write_sql('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + ref.session_id))
        before = _snapshot(cold.db)
        with monkeypatch.context() as reads_only:
            reads_only.setattr(cold.db, '_execute_write', lambda *a, **k: pytest.fail('refusal wrote durable state'))
            with pytest.raises(RuntimeStoreError):
                restore_local_session(cold, ref.session_id)
        assert ref.session_id not in cold.sessions
        assert _snapshot(cold.db) == before
    finally:
        cold.db.close()
