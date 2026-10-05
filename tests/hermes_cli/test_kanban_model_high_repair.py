"""H-S1/H-S2/H-S3 regressions. All identities, proofs, and DBs are FIXTURES.

No provider calls: the existing owner-admission store is seeded as a fixture,
and dispatch exercises its real claim/catch/accounting path with an argv-only spawn.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.delegation_context import delegated_child_context, DELEGATED_CHILD_ENV_MARKER
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_board_ops_store as owner_store
from hermes_cli.kanban_model_selection import set_policy, selection_command, bind_worker_selection


@pytest.fixture
def lane(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(root))
    monkeypatch.setenv('HERMES_HOME', str(root / 'profiles' / 'owner'))
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', DELEGATED_CHILD_ENV_MARKER,
                'HERMES_KANBAN_DB', 'HERMES_PROFILE_NAME', 'HERMES_PROFILE', 'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key, raising=False)
    for name in ('owner', 'worker', 'other'):
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('{}\n')
    kb.init_db()
    with kbc.connect_closing() as conn:
        owner_store.initialize(conn)
        control = {'node_id': 'fixture-node', 'board': 'default', 'owner_profile': 'owner',
                   'owner_home': str(root / 'profiles' / 'owner'), 'executor_profile': 'other',
                   'executor_home': str(root / 'profiles' / 'other'),
                   'authority_provenance': 'FIXTURE node-root admission'}
        conn.execute('INSERT INTO kanban_board_ops_control(singleton,contract,enabled,revision) VALUES(1,?,1,1)',
                     (json.dumps(control),))
        yield root, conn


def policy():
    stamp = datetime.now(timezone.utc).isoformat()
    route = {'provider': 'fixture-provider', 'model': 'fixture-model'}
    return {'node': 'fixture-node', 'profile': 'worker', 'task_class': 'review',
            'required_capabilities': ['tools'], 'minimum_quality': 0.8,
            'snapshot': {'fleet_profile_config_v1': {'node': 'fixture-node', 'profile': 'worker',
                'model_chain': [route], 'credential_provider_names': ['fixture-provider'], 'observed_at': stamp},
                'model_capabilities': [{**route, 'node': 'fixture-node', 'profile': 'worker',
                    'capabilities': ['tools'], 'quality': {'review': 0.9}, 'uncertain': False,
                    'proof': {'authenticated': True, 'usable': True, 'observed_at': stamp}}]}}


def task(conn, *, exact=False):
    return kb.create_task(conn, title='FIXTURE model high repair', assignee='worker',
                          model_override='fixture-model', provider_override='fixture-provider',
                          exact_model_reason='FIXTURE essential evaluation' if exact else None)


def state(conn, tid):
    return (dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()),
            [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id', (tid,))])


@pytest.mark.parametrize('change', ['remove_reason', 'change_reason', 'swap_route', 'same_lock'])
def test_h1_existing_exact_lock_is_immutable(lane, change):
    _, conn = lane
    tid = task(conn, exact=True)
    before = state(conn, tid)
    p = {'exact_reason': 'FIXTURE essential evaluation', 'checkpoint_ref': 'FIXTURE refreshed checkpoint'}
    if change in {'remove_reason', 'swap_route'}:
        p = policy()
    if change == 'change_reason':
        p['exact_reason'] = 'different evaluation'
    if change == 'swap_route':
        p['snapshot']['model_capabilities'][0]['model'] = 'other-model'
        p['snapshot']['fleet_profile_config_v1']['model_chain'][0]['model'] = 'other-model'
    if change == 'same_lock':
        result = set_policy(conn, tid, p)
        assert result['selected'] == {'provider': 'fixture-provider', 'model': 'fixture-model'}
        assert kb.get_task(conn, tid).model_policy == p
        with pytest.raises(ValueError, match='exact-model policy'):
            kb.set_model_override(conn, tid, 'other-model', provider='fixture-provider')
    else:
        with pytest.raises(ValueError, match='exact-model lock'):
            set_policy(conn, tid, p)
        assert state(conn, tid) == before


@pytest.mark.parametrize('context', ['worker', 'worker_run', 'delegate', 'child_process', 'forged_actor',
                                     'forged_proof', 'remove_snapshot', 'no_admission', 'owner'])
def test_h2_apply_requires_owner_and_never_authenticates_caller_proof(lane, monkeypatch, context):
    root, conn = lane
    tid = task(conn, exact=True)
    p = {'exact_reason': 'FIXTURE essential evaluation'}
    if context == 'worker':
        monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    elif context == 'worker_run':
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '1')
    elif context == 'child_process':
        monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(root))
    elif context == 'forged_actor':
        monkeypatch.setenv('HERMES_HOME', str(root / 'profiles' / 'other'))
        monkeypatch.setenv('HERMES_PROFILE_NAME', 'owner')  # A display alias cannot authenticate.
    elif context == 'forged_proof':
        p = policy()
        p['exact_reason'] = 'FIXTURE essential evaluation'
    elif context == 'remove_snapshot':
        stored = policy()
        stored['exact_reason'] = 'FIXTURE essential evaluation'
        conn.execute('UPDATE tasks SET model_policy=? WHERE id=?', (json.dumps(stored), tid))
    elif context == 'no_admission':
        conn.execute('DELETE FROM kanban_board_ops_control')
    before = state(conn, tid)
    scope = delegated_child_context() if context == 'delegate' else nullcontext()
    with scope:
        if context == 'owner':
            result = set_policy(conn, tid, p)
            assert result['actor'] == 'owner'
            assert result['actor_home'] == str(root / 'profiles' / 'owner')
            assert result['route_evidence']['source'] == 'task_exact_route'
            assert result['route_evidence']['authenticated'] == 'unobserved'
            event = json.loads(state(conn, tid)[1][-1]['payload'])
            assert event['actor'] == 'owner' and event['route_evidence'] == result['route_evidence']
        else:
            expected = {
                'worker': 'worker_or_delegated_selection_mutation_forbidden',
                'worker_run': 'worker_or_delegated_selection_mutation_forbidden',
                'delegate': 'worker_or_delegated_selection_mutation_forbidden',
                'child_process': 'worker_or_delegated_selection_mutation_forbidden',
                'forged_actor': 'selection_requires_admitted_owner_profile',
                'forged_proof': 'trusted_route_evidence_unavailable',
                'remove_snapshot': 'trusted_route_evidence_unavailable',
                'no_admission': 'owner_selection_authority_unavailable',
            }[context]
            with pytest.raises(ValueError, match=expected):
                set_policy(conn, tid, p)
            assert state(conn, tid) == before


def test_h2_public_apply_rejects_worker_but_preview_is_read_only(lane, tmp_path, monkeypatch, capsys):
    _, conn = lane
    tid = task(conn, exact=True)
    file = tmp_path / 'fixture-policy.json'
    file.write_text(json.dumps({'exact_reason': 'FIXTURE essential evaluation'}))
    args = SimpleNamespace(policy_file=str(file), task_id=tid, apply=False)
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    before = state(conn, tid)
    assert selection_command(args) == 0
    capsys.readouterr()
    args.apply = True
    assert selection_command(args) == 2
    assert 'worker' in json.loads(capsys.readouterr().out)['reason']
    assert state(conn, tid) == before


@pytest.mark.parametrize('fault', ['stale', 'unavailable', 'missing_route', 'exact_stale', 'quality', 'capability', 'policy_hold', 'unavailable_policy_hold', 'advisory', 'bug'])
def test_h3_dispatch_accounts_for_evidence_outage_not_product_failure(lane, monkeypatch, all_assignees_spawnable, fault):
    _, conn = lane
    tid = task(conn)
    p = policy()
    if fault in {'stale', 'exact_stale'}:
        p['snapshot']['fleet_profile_config_v1']['observed_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    if fault == 'unavailable':
        p['snapshot']['model_capabilities'] = []
    if fault == 'missing_route':
        p['snapshot']['model_capabilities'][0]['model'] = 'FIXTURE unrelated route'
    if fault == 'exact_stale':
        p['exact_reason'] = 'FIXTURE essential evaluation'
    if fault == 'quality':
        p['snapshot']['model_capabilities'][0]['quality']['review'] = 0.1
    if fault == 'capability':
        p['snapshot']['model_capabilities'][0]['capabilities'] = []
    if fault in {'policy_hold', 'unavailable_policy_hold'}:
        p['failed_routes'] = [{'provider': 'fixture-provider', 'model': 'fixture-model',
                              'observed_at': '2026-01-01T00:00:00Z', 'kind': 'policy'}]
    if fault == 'unavailable_policy_hold':
        p['snapshot']['model_capabilities'] = []
    if fault == 'advisory':
        p['snapshot']['model_capabilities'][0]['uncertain'] = True
        p['advisory'] = {'ok': False, 'error': 'not_configured'}
    # Fixture injection represents a previously stored policy; do not pretend
    # the current owner API supplied protected authentication evidence.
    conn.execute('UPDATE tasks SET model_policy=? WHERE id=?', (json.dumps(p), tid))
    def argv_only_spawn(claimed, workspace, board=None):
        if fault == 'bug':
            raise RuntimeError('FIXTURE launcher product bug')
        dispatch._worker_argv(claimed, 'worker', None)
        raise AssertionError('No fixture worker should be spawned')
    result = dispatch.dispatch_once(conn, spawn_fn=argv_only_spawn, failure_limit=1)
    row = kb.get_task(conn, tid)
    infrastructure = fault in {'stale', 'unavailable', 'missing_route', 'exact_stale', 'advisory'}
    assert row.consecutive_failures == (0 if infrastructure else 1)
    assert row.status == ('ready' if infrastructure else 'blocked')
    assert result.auto_blocked == ([] if infrastructure else [tid])
    run = conn.execute('SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
    assert bool(json.loads(run['metadata']).get('infrastructure')) is infrastructure
    if infrastructure:
        assert dispatch.check_respawn_guard(conn, tid) == 'infrastructure_cooldown'
        runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        assert dispatch.dispatch_once(conn, spawn_fn=argv_only_spawn).spawned == []
        assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == runs


@pytest.mark.parametrize('fault', ['stale', 'quality', 'mismatch'])
def test_h3_worker_init_binding_reaches_real_exit_contract(lane, monkeypatch, fault):
    import cli
    import run_agent
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_cli import mcp_startup

    _, conn = lane
    tid = task(conn)
    p = policy()
    if fault == 'stale':
        p['snapshot']['fleet_profile_config_v1']['observed_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    elif fault == 'quality':
        p['snapshot']['model_capabilities'][0]['quality']['review'] = 0.1
    conn.execute('UPDATE tasks SET model_policy=? WHERE id=?', (json.dumps(p), tid))
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    # Replace only external setup/constructor. The constructor below reaches
    # real task ownership, real stored-policy selection and binding; no provider.
    monkeypatch.setattr(cli, '_prepare_deferred_agent_startup', lambda: None)
    monkeypatch.setattr(mcp_startup, 'ensure_mcp_discovery_before_agent_build', lambda **kw: None)
    def constructor(**kw):
        agent = SimpleNamespace(provider=kw['provider'], model=kw['model'])
        bind_worker_selection(agent)
        raise AssertionError('Fixture refusal must occur before any agent runs')
    monkeypatch.setattr(run_agent, 'AIAgent', constructor)
    fields = ('enabled_toolsets disabled_toolsets system_prompt prefill_messages reasoning_config service_tier '
              '_providers_only _providers_ignore _providers_order _provider_sort _provider_require_params '
              '_provider_data_collection _openrouter_min_coding_score _fallback_model checkpoints_enabled '
              'checkpoint_max_snapshots checkpoint_max_total_size_mb checkpoint_max_file_size_mb pass_session_id '
              '_on_thinking _on_tool_progress _on_notice _on_notice_clear _on_reaction').split()
    stub = SimpleNamespace(**{key: None for key in fields},
        agent=None, _session_db=object(), _resumed=False, _single_query_mode=True,
        model='other-model' if fault == 'mismatch' else 'fixture-model', max_turns=1,
        verbose=False, session_id='FIXTURE-session', ignore_rules=True,
        _inline_diffs_enabled=False, streaming_enabled=False, _last_turn_result=None,
        finalize_preloaded_skills=lambda: None, _install_tool_callbacks=lambda: None,
        _ensure_tirith_security=lambda: None, _ensure_runtime_credentials=lambda: True,
        _current_reasoning_callback=lambda: None)
    assert CLIAgentSetupMixin._init_agent(stub, runtime_override={'provider': 'fixture-provider'}) is False
    assert cli._single_query_exit_code(stub._last_turn_result) == (75 if fault == 'stale' else 1)
    if fault == 'stale':
        assert stub._last_turn_result['failure_reason'] == 'model_selection_evidence_unavailable'
