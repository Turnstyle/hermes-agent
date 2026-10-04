"""Registry integration contracts; saved Luna metadata is a fixture, not live proof."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_model_selection import select_model


@pytest.fixture
def registry_policy(tmp_path, monkeypatch):
    record = json.loads((Path(__file__).parent / 'fixtures/model_registry_luna.json').read_text())
    script = tmp_path / 'fleet_ops.py'
    script.write_text('import json, sys\nassert sys.argv[1:4] == ["model-routes", "get", "--model"]\n'
                      'assert sys.argv[4:] == ["openai_gpt-6-luna", "--json"]\n'
                      f'print({json.dumps(record)!r})\n')
    monkeypatch.setenv('HERMES_FLEET_OPS', str(script))
    stamp = datetime.now(timezone.utc).isoformat()
    route = {'provider': 'openai', 'model': 'gpt-6-luna'}
    policy = {'node': 'fixture-node', 'profile': 'worker', 'task_class': 'review',
              'required_capabilities': [], 'minimum_quality': 0.8,
              'registry': {'model_ids': ['openai_gpt-6-luna']},
              'advisory': {'ok': True, 'observed_at': stamp, 'eligible_routes': [route]},
              'snapshot': {'model_capabilities': [], 'fleet_profile_config_v1': {
                  'node': 'fixture-node', 'profile': 'worker', 'model_chain': [route],
                  'credential_provider_names': ['openai'], 'observed_at': stamp}}}
    task = SimpleNamespace(id='fixture-task', assignee='worker', status='ready',
                           model_override='gpt-6-luna', provider_override='openai',
                           model_policy=policy, completion_contract='fixture-contract')
    return task, policy, script, record


@pytest.mark.parametrize('case', ['real_fixture', 'numeric', 'no_advice', 'stale', 'wrong_node',
                                  'unauthorized', 'quota', 'exact', 'capability', 'low_quality'])
def test_registry_selection_preserves_evidence_boundaries(registry_policy, case):
    from hermes_cli.kanban_model_registry import read_model_record
    task, policy, script, record = registry_policy
    before = deepcopy(policy)
    meta, reason = read_model_record('openai_gpt-6-luna', policy, now=datetime.now(timezone.utc).timestamp())
    assert reason == 'registry_lookup_ok'
    assert meta['uncertain'] is True
    assert 'quality' not in meta  # Unscaled benchmark and untested verdicts prove no task score.
    assert meta['provider'] == 'openai' and meta['model'] == 'gpt-6-luna'
    assert meta['capabilities'] == record['good_at'] + record['best_fleet_uses']
    assert meta['proof']['authenticated'] is True
    assert policy == before
    if case in {'numeric', 'low_quality'}:
        record['job_type_verdicts']['review'] = 0.9 if case == 'numeric' else 0.1
        script.write_text(f'print({json.dumps(record)!r})\n')
    if case == 'no_advice':
        policy['advisory']['ok'] = False
    if case == 'stale':
        policy['snapshot']['fleet_profile_config_v1']['observed_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    if case == 'wrong_node':
        policy['snapshot']['fleet_profile_config_v1']['node'] = 'other'
    if case == 'unauthorized':
        policy['snapshot']['fleet_profile_config_v1']['credential_provider_names'] = []
    if case == 'quota':
        policy['failed_routes'] = [{'provider': 'openai', 'model': 'gpt-6-luna',
                                  'observed_at': datetime.now(timezone.utc).isoformat(), 'kind': 'quota'}]
    if case == 'exact':
        policy['exact_reason'] = 'Fixture evaluation identity'
    if case == 'capability':
        policy['required_capabilities'] = ['tools']
    result = select_model(task)
    assert result['selected'] == (policy['advisory']['eligible_routes'][0]
                                  if case in {'real_fixture', 'numeric'} else None)
    assert result['registry_reason'] == 'registry_lookup_ok'


@pytest.mark.parametrize('fault', ['missing', '404', pytest.param('timeout', marks=pytest.mark.live_system_guard_bypass), 'bad_json'])
def test_registry_outage_uses_advised_chain_once_without_leaking_errors(registry_policy, fault):
    task, policy, script, record = registry_policy
    if fault == 'missing':
        script.unlink()
    elif fault == '404':
        script.write_text('import sys\nprint("SECRET 404", file=sys.stderr)\nsys.exit(1)\n')
    elif fault == 'timeout':
        # Real child plus inherited pipes exercises the fleet reader -> gcloud
        # topology. The marker allows only this test's disposable process group.
        script.write_text('import subprocess, sys, time\n'
                          'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])\n'
                          'time.sleep(10)\n')
    else:
        script.write_text('print("SECRET invalid JSON")\n')
    import time
    started = time.monotonic()
    if fault != 'missing':
        count = script.with_suffix('.attempts')
        script.write_text('from pathlib import Path\n'
                          f'with Path({str(count)!r}).open("a") as attempts: attempts.write("attempt\\n")\n'
                          + script.read_text())
    result = select_model(task)
    if fault != 'missing':
        assert count.read_text().splitlines() == ['attempt']
    assert time.monotonic() - started < 5  # Loose bound, still below the child sleep.
    assert result['selected'] == {'provider': 'openai', 'model': 'gpt-6-luna'}
    assert result['registry_reason'] == {
        'missing': 'registry_reader_missing', '404': 'registry_lookup_failed',
        'timeout': 'registry_lookup_timeout', 'bad_json': 'registry_invalid_record'}[fault]
    assert 'SECRET' not in json.dumps(result)
    policy['advisory']['ok'] = False
    assert select_model(task)['selected'] is None


def test_registry_public_command_and_worker_scope_a_b_a(registry_policy, tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_model_selection import selection_command, bind_worker_selection, fallback_allowed

    _, template, script, _ = registry_policy
    for profile in ('worker-a', 'worker-b', 'worker-a'):
        home = tmp_path / profile
        home.mkdir(exist_ok=True)
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        kb.init_db()
        policy = deepcopy(template)
        policy['profile'] = profile
        policy['snapshot']['fleet_profile_config_v1']['profile'] = profile
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title='Fixture registry routing', assignee=profile,
                                 model_override='gpt-6-luna', provider_override='openai')
        file = home / 'policy.json'
        file.write_text(json.dumps(policy))
        args = SimpleNamespace(policy_file=str(file), task_id=tid, apply=True)
        assert selection_command(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['registry_reason'] == 'registry_lookup_ok'
        with kbc.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            assert task.model_policy == policy
            assert task.status == 'ready'
        monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
        agent = SimpleNamespace(provider='openai', model='gpt-6-luna')
        bind_worker_selection(agent)
        assert agent._model_selection['selected'] == result['selected']
        assert agent._fallback_chain == []
        assert fallback_allowed(agent, 'openai', 'gpt-6-luna')
        assert not fallback_allowed(agent, 'unapproved', 'other')


@pytest.mark.parametrize('fault', ['wrong_id', 'bad_shape', 'stale_advice', 'policy_hold', 'exact_outage'])
def test_registry_failures_cannot_lift_owner_holds(registry_policy, fault):
    task, policy, script, record = registry_policy
    if fault == 'wrong_id':
        record['model_id'] = 'openai/other-model'
        script.write_text(f'print({json.dumps(record)!r})\n')
        policy['advisory']['eligible_routes'] = []
    elif fault == 'bad_shape':
        script.write_text('print("[]")\n')
        policy['advisory']['eligible_routes'] = []
    elif fault == 'stale_advice':
        policy['advisory']['observed_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    elif fault == 'policy_hold':
        policy['failed_routes'] = [{'provider': 'openai', 'model': 'gpt-6-luna',
                                  'observed_at': '2026-01-01T00:00:00Z', 'kind': 'policy'}]
    else:
        script.unlink()
        policy['exact_reason'] = 'Exact fixture identity'
    result = select_model(task)
    assert result['selected'] is None
    if fault == 'exact_outage':
        assert result['reason'] == 'exact_model_unavailable'
