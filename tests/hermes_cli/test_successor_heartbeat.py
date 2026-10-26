import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt

@pytest.fixture
def card(monkeypatch,tmp_path):
    for key in ('HERMES_DELEGATED_CHILD_CONTEXT','HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_KANBAN_CLAIM_LOCK','HERMES_SESSION_ID'):
        monkeypatch.delenv(key,raising=False)
    for key,rel in [('HOME','host'),('HERMES_HOME','home'),('HERMES_KANBAN_HOME','board'),('HERMES_KANBAN_WORKSPACES_ROOT','workspaces')]:
        p=tmp_path/rel;p.mkdir();monkeypatch.setenv(key,str(p))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'board/kanban.db'))
    monkeypatch.setenv('HERMES_PROFILE','test-worker')
    kb._INITIALIZED_PATHS.clear();kb.init_db()
    with kbc.connect() as c:
        tid=kb.create_task(c,title='heartbeat-boundary',assignee='test-worker')
        kb.claim_task(c,tid,claimer='reused-lock')
        run=kb.get_task(c,tid).current_run_id
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(run))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK','reused-lock')
    return tid,run

def state(tid):
    with kbc.connect() as c:
        return {'task':dict(c.execute('SELECT claim_expires,last_heartbeat_at,current_run_id FROM tasks WHERE id=?',(tid,)).fetchone()),'runs':[dict(r) for r in c.execute('SELECT id,claim_expires,last_heartbeat_at FROM task_runs WHERE task_id=? ORDER BY id',(tid,))],'events':[dict(r) for r in c.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id',(tid,))]}

def test_stale_run_cannot_extend_successor(card):
    tid,old=card
    with kbc.connect() as c:
        kb.reclaim_task(c,tid)
        kb.claim_task(c,tid,claimer='reused-lock')
        c.execute('UPDATE tasks SET claim_expires=123 WHERE id=?',(tid,))
        c.execute('UPDATE task_runs SET claim_expires=123 WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)',(tid,));c.commit()
    before=state(tid)
    assert before['task']['current_run_id']!=old
    result=json.loads(kt._handle_heartbeat({'note':'stale A'}))
    after=state(tid)
    print(json.dumps({'result':result,'before':before,'after':after},default=str))
    assert after==before
    assert result.get('error') and not result.get('ok')

def test_valid_run_heartbeat(card):
    tid,run=card
    with kbc.connect() as c:
        c.execute('UPDATE tasks SET claim_expires=123 WHERE id=?',(tid,));c.commit()
    result=json.loads(kt._handle_heartbeat({'note':'valid'}))
    assert result.get('ok') is True
    s=state(tid)
    assert s['task']['claim_expires']>123
    assert s['task']['claim_expires']==s['runs'][-1]['claim_expires']
    assert s['events'][-1]['kind']=='heartbeat'

def test_event_failure_rolls_back_claim(card):
    tid,run=card
    with kbc.connect() as c:
        c.execute('UPDATE tasks SET claim_expires=123 WHERE id=?',(tid,));c.commit()
        c.execute("CREATE TRIGGER reject_heartbeat BEFORE INSERT ON task_events WHEN NEW.kind='heartbeat' BEGIN SELECT RAISE(ABORT,'heartbeat fault'); END");c.commit()
    before=state(tid)
    result=json.loads(kt._handle_heartbeat({}))
    assert result.get('error')
    assert state(tid)==before

@pytest.mark.parametrize('mode',['wrong-lock','lease-less','missing-run','missing-all','missing-current-run'])
def test_warning_and_conflict_policy(card,monkeypatch,mode):
    tid,run=card
    if mode=='wrong-lock':monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK','foreign-lock')
    elif mode=='missing-run':monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    elif mode=='missing-current-run':
        with kbc.connect() as c:
            c.execute('UPDATE tasks SET current_run_id=NULL WHERE id=?',(tid,));c.commit()
    elif mode=='missing-all':
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
        monkeypatch.delenv('HERMES_KANBAN_CLAIM_LOCK')
        with kbc.connect() as c:
            c.execute('UPDATE tasks SET current_run_id=NULL,claim_lock=NULL,claim_expires=NULL WHERE id=?',(tid,));c.commit()
    else:
        with kbc.connect() as c:
            c.execute('UPDATE tasks SET claim_lock=NULL,claim_expires=NULL WHERE id=?',(tid,));c.commit()
    before=state(tid)
    result=json.loads(kt._handle_heartbeat({}))
    if mode=='wrong-lock':
        assert state(tid)==before
        assert result.get('error') and not result.get('ok')
    else:
        assert result.get('ok') is True
        assert result.get('warning')


@pytest.fixture(params=['env', 'registry', 'cli'])
def production_entry(request, monkeypatch):
    from argparse import Namespace
    from hermes_cli import kanban
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0.0)
    def invoke(tid):
        if request.param == 'env':
            return kt.heartbeat_current_worker_from_env()
        if request.param == 'registry':
            return json.loads(kt.registry.dispatch('kanban_heartbeat', {'task_id': tid}))
        return kanban._cmd_heartbeat(Namespace(task_id=tid, note=None))
    return request.param, invoke


def test_production_entry_stale_same_lock_unchanged(card, production_entry):
    tid, old = card
    with kbc.connect() as c:
        kb.reclaim_task(c, tid)
        kb.claim_task(c, tid, claimer='reused-lock')
        c.execute('UPDATE tasks SET claim_expires=123 WHERE id=?', (tid,))
        c.execute('UPDATE task_runs SET claim_expires=123 WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)', (tid,))
        c.commit()
    before = state(tid)
    assert before['task']['current_run_id'] != old
    kind, invoke = production_entry
    result = invoke(tid)
    assert state(tid) == before
    if kind == 'env':
        assert result is False
    elif kind == 'registry':
        assert result.get('error') and not result.get('ok')
    else:
        assert result != 0


def test_production_entry_event_failure_is_atomic(card, production_entry):
    tid, _ = card
    with kbc.connect() as c:
        c.execute("CREATE TRIGGER reject_heartbeat BEFORE INSERT ON task_events WHEN NEW.kind='heartbeat' BEGIN SELECT RAISE(ABORT,'heartbeat fault'); END")
        c.commit()
    before = state(tid)
    kind, invoke = production_entry
    if kind == 'cli':
        with pytest.raises(Exception, match='heartbeat fault'):
            invoke(tid)
    else:
        result = invoke(tid)
        assert result is False if kind == 'env' else result.get('error')
    assert state(tid) == before


@pytest.fixture(params=['env', 'registry'])
def renewing_entry(request, monkeypatch):
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0.0)
    return request.param, lambda tid: (kt.heartbeat_current_worker_from_env() if request.param == 'env' else json.loads(kt.registry.dispatch('kanban_heartbeat', {'task_id': tid})))


@pytest.mark.parametrize('mode', ['valid', 'wrong-lock', 'missing-run', 'missing-current-run', 'lease-less', 'missing-all'])
def test_renewing_entry_policy(card, renewing_entry, monkeypatch, caplog, mode):
    kind, invoke = renewing_entry
    tid, _ = card
    if mode == 'wrong-lock':
        monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', 'foreign-lock')
    if mode in ('missing-run', 'missing-all'):
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    with kbc.connect() as c:
        if mode in ('missing-current-run', 'missing-all'):
            c.execute('UPDATE tasks SET current_run_id=NULL WHERE id=?', (tid,))
        if mode in ('lease-less', 'missing-all'):
            c.execute('UPDATE tasks SET claim_lock=NULL,claim_expires=NULL WHERE id=?', (tid,))
        else:
            c.execute('UPDATE tasks SET claim_expires=123 WHERE id=?', (tid,))
        c.commit()
    before = state(tid)
    result = invoke(tid)
    after = state(tid)
    if mode == 'wrong-lock':
        assert after == before
        assert result is False if kind == 'env' else result.get('error')
        return
    assert result is True if kind == 'env' else result.get('ok') is True
    assert after['events'][-1]['kind'] == 'heartbeat'
    assert after['task']['last_heartbeat_at'] is not None
    if mode in ('lease-less', 'missing-all'):
        assert after['task']['claim_expires'] is None
    else:
        assert after['task']['claim_expires'] > 123
    if mode != 'valid':
        assert 'heartbeat' in caplog.text if kind == 'env' else result.get('warning')



def test_env_throttle_leaves_state_unchanged(card, monkeypatch):
    tid, _ = card
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 100.0)
    monkeypatch.setattr(kt.time, 'monotonic', lambda: 120.0)
    before = state(tid)
    assert kt.heartbeat_current_worker_from_env() is False
    assert state(tid) == before


def test_cli_valid_heartbeat_does_not_renew_lease(card):
    from argparse import Namespace
    from hermes_cli import kanban
    tid, _ = card
    before = state(tid)
    assert kanban._cmd_heartbeat(Namespace(task_id=tid, note='cli')) == 0
    after = state(tid)
    assert after['task']['claim_expires'] == before['task']['claim_expires']
    assert after['runs'][-1]['claim_expires'] == before['runs'][-1]['claim_expires']
    assert after['events'][-1]['kind'] == 'heartbeat'
