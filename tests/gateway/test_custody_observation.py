"""Offline fixed custody bindings. All inputs and transport identities are synthetic."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def inert_imports(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    # Only bootstrap's import-time IO is inert; candidate modules import normally.
    monkeypatch.setitem(sys.modules, 'hermes_bootstrap', NS())
    import subprocess
    def no_process(*a,**kw):
        raise AssertionError('real child forbidden in offline tests')
    monkeypatch.setattr(subprocess,'Popen',no_process)
    monkeypatch.setitem(sys.modules, 'hermes_yaml', NS(safe_load=lambda *a,**k: {}))
    from gateway.session_managed_worker import ManagedWorker
    # OS process identity and signals are transport-only in this offline suite.
    monkeypatch.setattr(ManagedWorker, '_signal_worker', lambda *a,**k: None)
    monkeypatch.setattr(ManagedWorker, '_signal_group',
                        lambda self,kill: (self.process.kill if kill else self.process.terminate)())
    monkeypatch.setitem(sys.modules, 'agent.context_compressor', NS(
        LEGACY_SUMMARY_PREFIX='synthetic-legacy', SUMMARY_PREFIX='synthetic-summary',
        _MERGED_PRIOR_CONTEXT_HEADER='synthetic-merge', _MERGED_SUMMARY_DELIMITER='synthetic-delimiter',
        _SUMMARY_END_MARKER='synthetic-end', MODEL_ONLY_DISPLAY_METADATA_KEY='synthetic-display',
        _DB_PERSISTED_MARKER='synthetic-persisted', _is_checkpoint_item=lambda x: False,
        _newest_checkpoint_carrier=lambda x: None, split_user_originated_turn=lambda x: (x,[]),
        user_originated_turn_view=lambda x: x))




@pytest.fixture
def native(tmp_path, monkeypatch):
    from gateway import session_custody_observation as obs
    from gateway import session_managed_worker as managed
    import hermes_state
    from hermes_state import SessionDB
    monkeypatch.setattr(hermes_state, '_in_test_context', lambda: True)
    monkeypatch.setattr(hermes_state, '_real_platform_state_root', lambda: tmp_path / 'synthetic-production')
    db = SessionDB(tmp_path / 'owner.db')
    owner = db._conn
    db.create_session('synthetic-session','kanban')
    owner.execute("UPDATE sessions SET runtime_generation=2 WHERE id='synthetic-session'")
    owner.execute("INSERT INTO runtime_epoch VALUES(1,1,'synthetic-instance')")
    owner.execute("INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
                  "lineage_json,payload_json,payload_digest,intent,status,owner_epoch,generation) "
                  "VALUES('synthetic-admission','synthetic-request','synthetic-principal','synthetic-session',"
                  "'[]','{}','synthetic-digest','queue','started',1,2)")
    owner.execute("INSERT INTO worker_executions(execution_id,session_id,kind,status,owner_epoch,generation,adoption_digest) "
                  "VALUES('admission-worker:synthetic-admission','synthetic-session','compute','running',1,2,'synthetic-digest')")
    owner.commit()
    from hermes_cli import kanban_db as kb
    board_path = tmp_path / 'board.db'
    board = sqlite3.connect(board_path)
    board.row_factory = sqlite3.Row
    board.executescript(kb.SCHEMA_SQL)
    board.execute("INSERT INTO tasks(id,title,status,created_at,assignee,tenant,current_run_id,claim_lock,claim_expires,worker_pid) "
                  "VALUES('t_2b29710a','synthetic task','running',1,'tb-keys-router','turnerbook',7,'synthetic-lock',9999999999,123)")
    board.execute("INSERT INTO task_runs(id,task_id,status,started_at,claim_lock,claim_expires,worker_pid) "
                  "VALUES(7,'t_2b29710a','running',1,'synthetic-lock',9999999999,123)")
    auth = NS(profile_id=obs.ACTOR, db=db, runner=NS(), epoch=1,
              executing={'synthetic-session': 2}, _managed_workers={'synthetic-session': NS(worker=(123, 456),_reservation_identity=(obs.ACTOR,'synthetic-session',
                  'admission-worker:synthetic-admission',2,1,123,456))})
    monkeypatch.setattr(obs, 'BOARD_DB', str(board_path))
    context = dict(task_id='t_2b29710a', board='fleet',tenant='turnerbook',profile='tb-keys-router',
                   db=str(board_path),run_id=7,claim_lock='synthetic-lock')
    policy = NS(source='kanban',kanban_json=json.dumps(context))
    monkeypatch.setattr(managed, 'managed_policy', lambda a, r: policy)
    from hermes_cli import kanban_db_connect as kb_connect
    monkeypatch.setattr(kb_connect, '_check_file_length_invariant', lambda conn: None)
    for kind, event in [('owner_admitted', dict(session_id='synthetic-session',db=str(db.db_path),
                          request_id='kanban:["fleet","t_2b29710a",7]')),
                        ('worker_bound',dict(pid=123,claim_lock='synthetic-lock'))]:
        board.execute('INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,1)',
                      ('t_2b29710a',7,kind,json.dumps(event)))
    board.commit()
    yield NS(obs=obs,managed=managed,owner=owner,board=board,auth=auth,context=context,policy=policy,
             ref=NS(session_id='synthetic-session'),lease=lambda: obs.AdmittedObservation(auth,NS(session_id='synthetic-session'),'synthetic-admission'))
    board.close()
    db.close()


def capture(obs, compare=None):
    names = dict(zip(obs.PROVIDERS, ('SYNTHETIC_CLAUDE_NAME','SYNTHETIC_OPENAI_NAME')))
    return obs.MakerCapture(names, lambda p,n,c,f: c == obs.CONFIG and f == ('providers',p,'api_key'),
                            compare or (lambda p,n,e: e.get(n) == 'synthetic-value'))


def test_projection_missing_capture_is_unknown(native):
    from gateway.session_custody_projection import observe
    result = observe(capture(native.obs)._names, native=native.lease())
    assert result['readiness'] == 'UNPROVEN'
    assert [r['maker_launch_available'] for r in result['rows']] == [None,None]


@pytest.mark.parametrize('table,column,value', [
    ('tasks','claim_lock','wrong'),('tasks','claim_expires',0),('task_runs','claim_expires',0),
    ('task_runs','status','terminal'),('tasks','tenant','wrong'),('tasks','assignee','wrong'),
    ('tasks','current_run_id',8)])
def test_exact_claim_denials(native,table,column,value):
    native.board.execute(f'UPDATE {table} SET {column}=?',(value,)); native.board.commit()
    with pytest.raises(native.obs.RuntimeStoreError,match='stale_kanban_claim'):
        with native.lease().lease():
            pytest.fail('denied lease entered')


@pytest.mark.parametrize('field,value', [('board','wrong'),('tenant','wrong'),('task_id','wrong'),('profile','wrong')])
def test_policy_denials(native,field,value):
    native.context[field] = value
    native.policy.kanban_json = json.dumps(native.context)
    with pytest.raises(native.obs.RuntimeStoreError,match='permission_denied'):
        with native.lease().lease():
            pytest.fail('denied lease entered')


def test_delegation_and_nondispatcher_denied(native):
    from agent.delegation_context import non_dispatcher_owned_context, delegated_child_context
    for scope in (non_dispatcher_owned_context,delegated_child_context):
        with scope():
            with pytest.raises(native.obs.RuntimeStoreError,match='permission_denied'):
                with native.lease().lease():
                    pytest.fail('child admitted')


def test_epoch_and_reentrant_denial(native):
    with native.lease().lease() as lease:
        with pytest.raises(native.obs.RuntimeStoreError,match='permission_denied'):
            native.auth.db._execute_write(lambda conn: conn.execute('UPDATE runtime_epoch SET epoch=2'))
        with pytest.raises(sqlite3.DatabaseError):
            native.owner.execute('UPDATE runtime_epoch SET epoch=2')
        with pytest.raises(sqlite3.OperationalError):
            native.board.execute('UPDATE tasks SET status="terminal"')
        native.auth.epoch = 2
        with pytest.raises(native.obs.RuntimeStoreError,match='stale_epoch'):
            lease.revalidate()
        native.auth.epoch = 1
    lease = native.lease()
    with lease.lease():
        with pytest.raises(native.obs.RuntimeStoreError,match='permission_denied'):
            with lease.lease():
                pytest.fail('reentrant lease entered')


def test_original_denial_object_propagates(native):
    denial = native.obs.RuntimeStoreError('synthetic_original_denial')
    native.auth.runner._maker_custody_capture = capture(native.obs)
    def refuse(p,n):
        raise denial
    native.auth.runner._maker_custody_capture.witnesses = refuse
    with pytest.raises(native.obs.RuntimeStoreError) as error:
        native.obs.observe_admitted(native.auth,native.ref,'synthetic-admission')
    assert error.value is denial


def test_association_before_any_comparison(native):
    calls=[]
    with pytest.raises(native.obs.RuntimeStoreError,match='permission_denied'):
        native.obs.MakerCapture(capture(native.obs)._names,lambda *args: False, lambda *args: calls.append(args))
    assert calls == []


def maker_attempt(native, c):
    maker = NS(profile_id=native.obs.MAKER,runner=NS(_maker_custody_capture=c),db=native.auth.db,
               epoch=1,executing={'synthetic-session':2})
    attempt=c.begin(maker,native.ref,dict(admission_id='synthetic-admission',generation=2))
    scope=dict(profile_id=native.obs.MAKER,execution_id='admission-worker:synthetic-admission',
               session_id='synthetic-session',generation=2,epoch=1,pid=123,birth=456)
    return maker,attempt,scope


def test_final_capture_private_and_adoption(native):
    c = capture(native.obs)
    maker,attempt,scope = maker_attempt(native,c)
    env = dict.fromkeys(c._names.values(),'synthetic-value')
    process=object()
    got, launch = native.obs.capture_spawn(maker,lambda args,**kw: process,[],env=env,attempt=attempt)
    assert got is process
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []
    c.reserved(maker,launch,scope,process)
    c.ready(maker,launch,scope)
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []
    maker._managed_workers = {scope['session_id']: NS(process=process, _custody_launch=launch,
                                                    _reservation_identity=launch.reservation)}
    native.owner.execute("UPDATE worker_executions SET status='terminal'"); native.owner.commit()
    c.succeeded(maker,launch,scope)
    native.owner.execute("UPDATE worker_executions SET status='running'"); native.owner.commit()
    native.auth.runner._maker_custody_capture = c
    receipt = native.obs.observe_admitted(native.auth,native.ref,'synthetic-admission')
    assert [r['maker_launch_available'] for r in receipt['rows']] == [True,True]
    encoded=json.dumps(receipt)
    assert 'SYNTHETIC_' not in encoded and 'synthetic-value' not in encoded
    assert launch.capture_id not in encoded
    assert all(r['prospective_inherited_clean_child_available'] is None for r in receipt['rows'])
    c.discard(launch)
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []


def test_inherited_env_and_failed_spawn_unknown(native):
    c=capture(native.obs)
    maker=NS(profile_id=native.obs.MAKER,runner=NS(_maker_custody_capture=c))
    assert native.obs.capture_spawn(maker,lambda *a,**kw: object(),[],env=None)[1] is None
    def fail(*a,**kw):
        raise OSError('synthetic spawn failure')
    with pytest.raises(OSError):
        native.obs.capture_spawn(maker,fail,[],env={})
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []


def test_ready_requires_native_running_execution(native):
    c=capture(native.obs)
    maker,attempt,scope=maker_attempt(native,c)
    process,launch=native.obs.capture_spawn(maker,lambda *a,**kw: object(),[],env={},attempt=attempt)
    c.reserved(maker,launch,scope,process)
    native.owner.execute('UPDATE worker_executions SET status="registered"');native.owner.commit()
    with pytest.raises(native.obs.RuntimeStoreError,match='producer_not_started'):
        c.ready(maker,launch,scope)
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []


def test_native_worker_constructor_and_capture_order(native,monkeypatch):
    from agent import secret_scope
    monkeypatch.setitem(sys.modules,'hermes_yaml',NS())
    monkeypatch.setitem(sys.modules,'hermes_cli.auth',NS(PROVIDER_REGISTRY={}))
    monkeypatch.setitem(sys.modules,'hermes_cli.config',NS(OPTIONAL_ENV_VARS={},CORE_DECLARED_ENV_NAMES=set(),
        PLATFORM_SECRET_ENV_SUFFIXES=('_TOKEN',),platform_manifest_secret_envs=lambda **kw: set(),
        TERMINAL_CONFIG_ENV_MAP={}))
    monkeypatch.setitem(sys.modules,'hermes_cli.profile_channels',NS(config_env_table_keys=lambda: ()))
    from tools.environments import local
    calls=[]
    monkeypatch.setattr(native.managed,'os',NS(environ={'OPENAI_API_KEY':'synthetic-residue','AUXILIARY_TEST_API_KEY':'synthetic-internal','SYNTHETIC_LAUNCH_KEY':'synthetic-residue','PATH':'/synthetic/bin','_HERMES_SERVE_PID':'synthetic-owner'}))
    monkeypatch.setattr(local,'_is_routed_home',lambda home: True)
    monkeypatch.setattr(secret_scope,'is_multiplex_active',lambda: True)
    monkeypatch.setattr(secret_scope,'build_profile_secret_scope',lambda home: {'SYNTHETIC_CLAUDE_NAME':'synthetic-value'})
    monkeypatch.setattr(secret_scope,'load_env_file',lambda path:{})
    monkeypatch.setitem(sys.modules,'hermes_cli.env_loader',NS(launch_dotenv_keys=lambda:{'SYNTHETIC_LAUNCH_KEY'},
        managed_dotenv_keys=lambda:set(),source_supplied_names=lambda:set()))
    from hermes_cli import config_backend
    monkeypatch.setattr(config_backend,'deployment_env_names',lambda:set())
    # Only private config/registry/home inputs are inert. All filtering and finalization execute.
    monkeypatch.setitem(sys.modules,'tools.env_passthrough',NS(is_env_passthrough=lambda n:False,
        resolve_passthrough_value=lambda n,v:v,scoped_passthrough_additions=lambda e:{}))
    for name in ('_home_adapter_secret_env','_registry_adapter_secret_env','_registered_adapter_secret_env',
                 '_plugin_terminal_env_strip_keys'):
        monkeypatch.setattr(local,name,lambda:frozenset())
    monkeypatch.setattr(local,'_prepend_hermes_bin_dir',lambda path:path)
    monkeypatch.setattr(local,'_strip_hermes_owned_pythonpath_and_runtime_markers',lambda e:None)
    monkeypatch.setattr(local,'_apply_windows_msys_bash_env_defaults',lambda e:None)
    import hermes_constants
    monkeypatch.setattr(hermes_constants,'get_hermes_home_override',lambda:None)
    monkeypatch.setattr(hermes_constants,'get_routing_process_hermes_home',lambda:Path('/synthetic/launch'))
    monkeypatch.setattr(hermes_constants,'apply_subprocess_home_env',lambda env: calls.append('home'))
    c=capture(native.obs,lambda p,n,e: calls.append(('compare', n in e, e.get('HERMES_HOME') == native.obs.MAKER)) or (n in e))
    maker,attempt,scope=maker_attempt(native,c)
    env=native.managed._worker_env(maker)
    native.obs.capture_spawn(maker,lambda *a,**kw: calls.append('popen') or object(),[],env=env,attempt=attempt)
    assert calls == ['home','home',('compare',True,True),('compare',False,True),'popen']
    assert 'OPENAI_API_KEY' not in env and 'AUXILIARY_TEST_API_KEY' not in env
    assert 'SYNTHETIC_LAUNCH_KEY' not in env


def test_actual_native_spawn_path_and_cancel(native,monkeypatch):
    from gateway import session_worker_spawn as spawn
    from gateway import session_managed_worker_log as log
    c=capture(native.obs)
    auth=native.auth
    auth.profile_id=native.obs.MAKER
    auth.runner._maker_custody_capture=c
    monkeypatch.setattr(native.managed,'_worker_env',lambda a: dict.fromkeys(c._names.values(),'synthetic-value'))
    monkeypatch.setattr(log,'worker_log_path',lambda home: None)
    workers=[]
    class Worker:
        def __init__(self,process):
            self.process=process
            workers.append(self)
    real_worker=native.managed.ManagedWorker
    monkeypatch.setattr(native.managed,'ManagedWorker',
        lambda p: real_worker(p) if isinstance(p,InertReferenceProcess) else Worker(p))
    monkeypatch.setattr(native.managed.subprocess,'Popen',
        lambda args,**kw: InertReferenceProcess(kw['env']) if args[-1]=='agent.managed_reference_worker' else object())
    row={'admission_id':'synthetic-admission','generation':2}
    worker, process=asyncio.run(native.managed._spawn_registered_worker(auth,native.ref,row))
    assert worker.process is process and worker._custody_launch is not None
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []
    async def cancel(*a,**kw):
        raise asyncio.CancelledError()
    monkeypatch.setattr(spawn,'acquire_process',cancel)
    auth.pending_results={}
    assert asyncio.run(native.managed._spawn_registered_worker(auth,native.ref,row)) is None
    assert auth.pending_results['synthetic-admission']['result']['interrupted'] is True


@pytest.mark.parametrize('field,value', [('epoch',2),('generation',3),('session_id','synthetic-wrong'),('execution_id','synthetic-wrong')])
def test_ready_wrong_identity(native,field,value):
    c=capture(native.obs)
    maker,attempt,scope=maker_attempt(native,c)
    process,launch=native.obs.capture_spawn(maker,lambda *a,**kw: object(),[],env={},attempt=attempt)
    c.reserved(maker,launch,scope,process)
    scope[field]=value
    with pytest.raises(native.obs.RuntimeStoreError):
        c.ready(maker,launch,scope)
    assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]]) == []


def test_custody_cannot_mutate_final_env(native):
    def compare(p,n,env):
        env[n]='synthetic-replacement'
    c=capture(native.obs,compare)
    with pytest.raises(TypeError):
        maker,attempt,scope=maker_attempt(native,c)
        c.capture(maker,{},attempt)


def test_missing_observer_without_names():
    from gateway.session_custody_projection import observe
    result=observe(None)
    assert result['status']=='native_observer_unavailable'
    assert all(r['maker_launch_available'] is None for r in result['rows'])


@pytest.mark.parametrize('drift', ['authority','profile','cancel','replay','new_attempt','execution_generation'])
def test_capture_attempt_cannot_be_promoted(native,drift):
    c=capture(native.obs)
    maker,attempt,scope=maker_attempt(native,c)
    process,launch=native.obs.capture_spawn(maker,lambda *a,**kw: object(),[],env={},attempt=attempt)
    c.reserved(maker,launch,scope,process)
    if drift=='authority':
        maker=NS(**vars(maker))
    elif drift=='profile':
        maker.profile_id=native.obs.ACTOR
    elif drift=='cancel':
        c.cancel(attempt)
    elif drift=='replay':
        c.ready(maker,launch,scope)
    elif drift=='new_attempt':
        c.begin(maker,native.ref,dict(admission_id='synthetic-next',generation=2))
    else:
        maker.executing['synthetic-session']=3
    with pytest.raises(native.obs.RuntimeStoreError):
        c.ready(maker,launch,scope)


def test_claim_clock_expiry_and_cross_connection_lock(native,monkeypatch):
    from agent import kanban_turn_recovery
    seen=[]
    real=kanban_turn_recovery.worker_claim_is_live
    def checked(**kw):
        seen.append(kw)
        return real(**kw)
    monkeypatch.setattr(kanban_turn_recovery,'worker_claim_is_live',checked)
    with native.lease().lease() as lease:
        from hermes_cli.kanban_db_connect import write_txn
        with pytest.raises(native.obs.RuntimeStoreError,match='permission_denied'):
            with write_txn(native.board):
                pytest.fail('reentrant board writer admitted')
        for path in (native.context['db'],str(native.auth.db.db_path)):
            with sqlite3.connect(path,timeout=0) as other:
                with pytest.raises(sqlite3.OperationalError,match='locked'):
                    other.execute('BEGIN IMMEDIATE')
        monkeypatch.setattr(kanban_turn_recovery,'_now',lambda: 10000000000)
        with pytest.raises(native.obs.RuntimeStoreError,match='stale_kanban_claim'):
            lease.revalidate()
        monkeypatch.setattr(kanban_turn_recovery,'_now',lambda: 1)
    assert seen and all(x['verify_pid'] is False and x['db_path']==native.context['db'] for x in seen)


@pytest.mark.parametrize('drift', ['db','actor','admission','session','worker','memory_generation'])
def test_admitted_identity_drift(native,drift):
    if drift=='db':
        native.context['db']+='-other'
        native.policy.kanban_json=json.dumps(native.context)
    elif drift=='actor':
        native.auth.profile_id=native.obs.MAKER
    elif drift=='admission':
        native.owner.execute("UPDATE session_admissions SET status='unknown'");native.owner.commit()
    elif drift=='session':
        native.owner.execute('UPDATE sessions SET runtime_generation=3');native.owner.commit()
    elif drift=='worker':
        native.auth._managed_workers['synthetic-session'].worker=(999,456)
    else:
        native.auth.executing['synthetic-session']=3
    with pytest.raises(native.obs.RuntimeStoreError):
        with native.lease().lease():
            pytest.fail('drift admitted')


@pytest.mark.parametrize('outcome',['finished','failure','cancel','adoption_failure','old_worker','result_failed','result_interrupted','nonzero_exit'])
def test_native_reserve_adopt_ready_finish(native,monkeypatch,outcome):
    from gateway import session_worker_reservation as reservation
    from gateway import session_managed_worker_log as logs
    from gateway import session_managed_worker as managed
    from hermes_state_runtime import adopt_worker_execution, mutate_worker_execution
    from hermes_state_keys import admission_fingerprint
    from io import BytesIO
    # Optional UI and attachment services are inert. Ledger/lifecycle code remains native.
    monkeypatch.setitem(sys.modules,'gateway.run_turn_progress',NS(publish_worker_tool_event=lambda *a: False))
    async def turn_text(*args):
        return 'synthetic text',None
    monkeypatch.setitem(sys.modules,'gateway.session_context_refs',NS(managed_turn_text=turn_text,refused_turn_result=lambda *a: {}))
    monkeypatch.setattr(reservation,'verify_worker_identity',lambda p,h: (321,654) if isinstance(p,InertReferenceProcess) else (123,456))
    monkeypatch.setattr(logs,'worker_log_path',lambda home: None)
    c=capture(native.obs)
    auth=native.auth
    auth.profile_id=native.obs.MAKER
    auth.runner._maker_custody_capture=c
    auth.pending_results={};auth.waiters={}
    auth._require_admission_open=lambda: None
    auth.adopt_agent=lambda *a: None
    auth.publish_execution=lambda *a: None
    auth._publish_pending=lambda *a: None
    auth.logical_owner=lambda sid:sid
    auth.sessions={'synthetic-session':NS(source=None,event_stream=NS(lock=threading.RLock(),publish=lambda *a,**k:None),
                                        controls=NS(snapshot=lambda *a: None))}
    native.owner.execute('DELETE FROM worker_executions');native.owner.commit()
    process=NS(pid=123,stdin=BytesIO(),stdout=BytesIO(),stderr=None,wait=lambda *a,**k:1 if outcome=='nonzero_exit' else 0)
    def popen(args, **kwargs):
        if args[-1] == 'agent.managed_reference_worker':
            return InertReferenceProcess(kwargs['env'])
        return process
    monkeypatch.setattr(managed.subprocess,'Popen',popen)
    monkeypatch.setattr(managed,'_worker_env',lambda a:dict.fromkeys(c._names.values(),'synthetic-value'))
    monkeypatch.setattr(managed,'_settled_session_usage',lambda *a: {})
    monkeypatch.setattr(managed,'_prompt_frame',lambda *a:False)
    states=[];workers=[]
    def bootstrap(a,r,row,policy,scope,text):
        states.append('reserved')
        assert native.owner.execute('SELECT status FROM worker_executions').fetchone()[0]=='registered'
        return dict(api_key=None,scope=scope)
    monkeypatch.setattr(managed,'_bootstrap',bootstrap)
    class InertWorker(managed.ManagedWorker):
        def __init__(self,p):
            super().__init__(p)
            self.writer=NS(start=lambda:None)
            self.frames=0;self.scope=None;workers.append(self)
        def send(self,frame):
            if 'scope' in frame:
                self.scope=frame['scope']
        async def next_frame(self,*args,**kw):
            self.frames+=1
            if self.frames==1:
                return dict(type='hello',pid=123,birth=456,ancestors=[])
            scope=self.scope
            if self.frames==2:
                if outcome=='adoption_failure':
                    raise native.obs.RuntimeStoreError('synthetic_adoption_denial')
                claim=admission_fingerprint(canonical_target=scope['session_id'],
                    payload={k:v for k,v in scope.items() if k!='epoch'}|{'principal':reservation.owner_worker_subject()})
                adopt_worker_execution(auth.db,epoch=scope['epoch'],execution_id=scope['execution_id'],
                    session_id=scope['session_id'],generation=scope['generation'],adoption_secret=claim)
                states.append('adopted')
                if outcome=='old_worker':
                    del self._custody_launch
                return dict(type='ready',pid=123)
            if self.frames==3:
                states.append('ready')
                if outcome=='failure':
                    return dict(type='error',reason='managed_worker_failed')
                if outcome=='cancel':
                    raise asyncio.CancelledError()
                return dict(type='result',result=dict(final_response='synthetic complete',
                    failed=outcome=='result_failed',interrupted=outcome=='result_interrupted'))
            mutate_worker_execution(auth.db,epoch=scope['epoch'],execution_id=scope['execution_id'],
                session_id=scope['session_id'],generation=scope['generation'],sequence=1,
                operation='execution.finish',payload={})
            states.append('finished')
            return dict(type='finished')
        def close(self):
            self.closed.set();states.append('closed')
    real_worker = managed.ManagedWorker
    monkeypatch.setattr(managed,'ManagedWorker',lambda p: real_worker(p) if isinstance(p,InertReferenceProcess) else InertWorker(p))
    from gateway import session_worker_child
    monkeypatch.setattr(session_worker_child,'delegation_limits',lambda *a:(10,1))
    row=dict(admission_id='synthetic-admission',principal_id='synthetic-principal',generation=2)
    policy=NS(kanban_json=None)
    if outcome in ('failure','cancel','adoption_failure'):
        with pytest.raises(managed.ManagedExecutionUnknown):
            asyncio.run(managed.execute_managed(auth,native.ref,row,policy))
        assert c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]])==[]
        assert all(r[0]=='terminal' for r in native.owner.execute('SELECT status FROM worker_executions'))
    else:
        assert asyncio.run(managed.execute_managed(auth,native.ref,row,policy))=='synthetic complete'
        assert states==['reserved','adopted','ready','finished','closed']
        assert all(r[0]=='terminal' for r in native.owner.execute('SELECT status FROM worker_executions'))
        witnesses=c.witnesses(native.obs.PROVIDERS[0],c._names[native.obs.PROVIDERS[0]])
        assert bool(witnesses) is (outcome=='finished')
        if outcome=='finished':
            assert [w['kind'] for w in witnesses] == ['actual_maker_launch','prospective_reference_child']
            assert witnesses[1]['available'] is True
            assert witnesses[0]['launch_capture_id']==witnesses[1]['launch_capture_id']
    assert not auth._managed_workers and workers[0].closed.is_set()


def test_public_bootstrap_has_no_reference_only_mode():
    from agent.managed_worker import validate_bootstrap
    with pytest.raises(ValueError,match='invalid_managed_worker_bootstrap'):
        validate_bootstrap({'reference_only_v1': True})


class InertReferenceProcess:
    """Only process IO is inert; the candidate's fixed probe evaluates synthetic env."""
    def __init__(self, environment, *, transform=None, returncode=0):
        import queue
        from agent.managed_worker import encode_frame
        self.environment = dict(environment)
        self.transform = transform
        self.exit_code = returncode
        self.returncode = None
        self.pid = 321
        self.stderr = None
        self.lines = queue.Queue()
        self.lines.put(encode_frame(dict(type='hello',pid=321,birth=654,ancestors=[])))
        self.stdout = NS(readline=lambda *a:self.lines.get(timeout=5), close=lambda:None)
        self.stdin = NS(write=self.write,flush=lambda:None,close=lambda:None)

    def write(self, data):
        from agent.managed_reference_worker import execute
        from agent.managed_worker import encode_frame
        result = execute(json.loads(data), self.environment)
        if self.transform:
            result = self.transform(result)
        self.lines.put(encode_frame(result))
        self.returncode = self.exit_code

    def wait(self, *a, **kw):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.lines.put(b'')

    kill = terminate


@pytest.fixture
def reference_launch(native, monkeypatch):
    from gateway import session_worker_reservation, session_worker_child
    c = capture(native.obs)
    maker, attempt, scope = maker_attempt(native,c)
    maker._require_admission_open=lambda:None
    monkeypatch.setattr(session_worker_child,'delegation_limits',lambda *a:(10,1))
    seen=[]
    def verify(process, hello):
        assert process is seen[0] and hello == dict(type='hello',pid=321,birth=654,ancestors=[])
        return 321,654
    monkeypatch.setattr(session_worker_reservation,'verify_worker_identity',verify)
    env = dict.fromkeys(c._names.values(),'synthetic-value')
    maker_process=object()
    def popen(args, **kw):
        if args[-1]=='agent.managed_reference_worker':
            assert kw['env'] is env
            assert set(kw)=={'env','cwd','stdin','stdout','stderr','close_fds'}
            process=InertReferenceProcess(kw['env']); seen.append(process)
            return process
        assert kw['env'] is env
        return maker_process
    process,launch=native.obs.capture_spawn(maker,popen,['synthetic-maker'],env=env,
                                           attempt=attempt,reference_cwd=Path('.'))
    c.reserved(maker,launch,scope,process)
    maker._managed_workers={scope['session_id']:NS(process=process,_custody_launch=launch,
                                                  _reservation_identity=launch.reservation)}
    c.ready(maker,launch,scope)
    yield NS(c=c,maker=maker,scope=scope,launch=launch,process=seen[0])
    launch.reference.close()


def test_reference_native_completion_is_not_launch_comparison(native,reference_launch):
    r=reference_launch
    # Launch comparisons were True. The actual probe sees one absent synthetic NAME.
    r.process.environment.pop(r.c._names[native.obs.PROVIDERS[1]])
    available=asyncio.run(r.launch.reference.complete(r.maker,r.launch,r.scope,r.c._names))
    assert available == {'fleet-relay-claude':True,'fleet-relay-openai':False}
    child=r.launch.reference.child
    row=native.owner.execute('SELECT * FROM worker_executions WHERE execution_id=?',(child['execution_id'],)).fetchone()
    assert row['kind']=='child' and row['status']=='terminal' and row['owner_epoch']==1
    assert r.c.witnesses(native.obs.PROVIDERS[0],r.c._names[native.obs.PROVIDERS[0]]) == []
    from hermes_state_runtime import mutate_worker_execution
    mutate_worker_execution(native.auth.db,epoch=1,execution_id=r.scope['execution_id'],
        session_id=r.scope['session_id'],generation=2,sequence=1,operation='execution.finish',payload={})
    r.c.succeeded(r.maker,r.launch,r.scope)
    for p,expected in zip(native.obs.PROVIDERS,(True,False)):
        witnesses=r.c.witnesses(p,r.c._names[p])
        assert witnesses[0]['available'] is True
        assert witnesses[1]['available'] is expected
        assert witnesses[0]['launch_capture_id']==witnesses[1]['launch_capture_id']
    with pytest.raises(native.obs.RuntimeStoreError,match='stale_generation'):
        asyncio.run(r.launch.reference.complete(r.maker,r.launch,r.scope,r.c._names))


@pytest.mark.parametrize('drift',['challenge','status','epoch','generation','reservation','process','cancel','new_capture','failure','child_generation'])
def test_reference_completion_denies_forgery_and_drift(native,reference_launch,drift):
    r=reference_launch
    def transform(frame):
        if drift=='challenge':
            frame['challenge']='0'*64
        elif drift=='status':
            native.auth.db._execute_write(lambda conn:conn.execute("UPDATE session_admissions SET status='unknown'"))
        elif drift=='epoch':
            r.maker.epoch=2
        elif drift=='generation':
            r.maker.executing['synthetic-session']=3
        elif drift=='reservation':
            r.maker._managed_workers['synthetic-session']._reservation_identity=()
        elif drift=='process':
            r.maker._managed_workers['synthetic-session'].process=object()
        elif drift=='cancel':
            r.c.cancel(r.launch.attempt)
        elif drift=='child_generation':
            native.auth.db._execute_write(lambda conn:conn.execute(
                'UPDATE worker_executions SET generation=1 WHERE execution_id=?',
                (r.launch.reference.child['execution_id'],)))
        elif drift=='new_capture':
            r.c.begin(r.maker,native.ref,dict(admission_id='synthetic-next',generation=2))
        return frame
    r.process.transform=transform
    if drift=='failure':
        r.process.exit_code=1
    with pytest.raises(native.obs.RuntimeStoreError):
        asyncio.run(r.launch.reference.complete(r.maker,r.launch,r.scope,r.c._names))
    assert r.launch.reference.available is None
    assert r.c.witnesses(native.obs.PROVIDERS[0],r.c._names[native.obs.PROVIDERS[0]])==[]


def test_reference_ledger_only_does_not_publish(native,reference_launch):
    r=reference_launch
    from gateway.session_worker_child import reserve_reference_child
    child=reserve_reference_child(r.maker,r.launch,r.scope,(321,654),'synthetic-principal')
    from hermes_state_runtime import mutate_worker_execution
    mutate_worker_execution(native.auth.db,epoch=1,**child,sequence=1,operation='execution.finish',payload={})
    assert r.launch.reference.available is None
    assert r.c.witnesses(native.obs.PROVIDERS[0],r.c._names[native.obs.PROVIDERS[0]])==[]


def test_stop_during_reference_hello_preserves_maker_deadline(native,monkeypatch):
    """Stop after ready must supervise the maker while reference hello is stalled."""
    import time
    from io import BytesIO
    from gateway import session_worker_reservation as reservation
    from gateway import session_worker_child, session_managed_worker_log as logs
    from hermes_state_runtime import adopt_worker_execution
    from hermes_state_keys import admission_fingerprint
    managed=native.managed
    real_worker=managed.ManagedWorker
    c=capture(native.obs)
    auth=native.auth
    auth.profile_id=native.obs.MAKER
    auth.runner._maker_custody_capture=c
    auth.pending_results={};auth.waiters={}
    auth._require_admission_open=lambda:None
    auth.adopt_agent=lambda *a:None
    auth.publish_execution=lambda *a:None
    auth._publish_pending=lambda *a:None
    auth.sessions={'synthetic-session':NS(event_stream=NS(lock=threading.RLock(),publish=lambda *a,**k:None),
                                        controls=NS(snapshot=lambda *a:None))}
    native.owner.execute('DELETE FROM worker_executions');native.owner.commit()
    monkeypatch.setitem(sys.modules,'gateway.run_turn_progress',NS(publish_worker_tool_event=lambda *a:False))
    async def turn_text(*args):
        return 'synthetic text',None
    monkeypatch.setitem(sys.modules,'gateway.session_context_refs',NS(managed_turn_text=turn_text,refused_turn_result=lambda *a:{}))
    monkeypatch.setattr(logs,'worker_log_path',lambda *a:None)
    monkeypatch.setattr(managed,'_worker_env',lambda *a:dict.fromkeys(c._names.values(),'synthetic-value'))
    monkeypatch.setattr(reservation,'verify_worker_identity',lambda *a:(123,456))
    monkeypatch.setattr(session_worker_child,'delegation_limits',lambda *a:(10,1))
    monkeypatch.setattr(managed,'_bootstrap',lambda a,r,row,p,scope,text:dict(api_key=None,scope=scope))
    monkeypatch.setattr(managed,'_prompt_frame',lambda *a:False)
    # A generous test clock budget, independent of the production 30-second constant.
    monkeypatch.setattr(managed,'STOP_ACK_SECONDS',2)
    probe=InertReferenceProcess(dict.fromkeys(c._names.values(),'synthetic-value'))
    probe.lines.get_nowait()  # no hello; pipe waits for termination, not a fabricated completion
    probe_reading=threading.Event()
    def stalled_hello(*args):
        probe_reading.set()
        return probe.lines.get(timeout=10)
    probe.stdout.readline=stalled_hello
    process=NS(pid=123,stdin=BytesIO(),stdout=BytesIO(),stderr=None,wait=lambda *a,**k:0)
    monkeypatch.setattr(managed.subprocess,'Popen',lambda args,**kw:
                        probe if args[-1]=='agent.managed_reference_worker' else process)
    import queue
    maker_lines=queue.Queue()
    process.stdout=NS(readline=lambda *a:maker_lines.get(timeout=10),close=lambda:None)
    workers=[]
    async def scenario():
        ready=asyncio.Event()
        class MakerWorker(real_worker):
            def __init__(self,p):
                super().__init__(p)
                self.writer=NS(start=lambda:None)
                self.frames=0
                workers.append(self)
            def send(self,frame):
                self.scope=frame['scope']
            async def next_frame(self,timeout,ack):
                self.frames+=1
                if self.frames==1:
                    return dict(type='hello',pid=123,birth=456,ancestors=[])
                if self.frames==2:
                    scope=self.scope
                    claim=admission_fingerprint(canonical_target=scope['session_id'],payload={
                        k:v for k,v in scope.items() if k!='epoch'}|{'principal':reservation.owner_worker_subject()})
                    adopt_worker_execution(auth.db,epoch=1,execution_id=scope['execution_id'],
                        session_id=scope['session_id'],generation=2,adoption_secret=claim)
                    # A native delegated child must retire with this stalled root too.
                    auth.db._execute_write(lambda conn:session_worker_child.reserve_child_execution(
                        auth.db,conn,scope['session_id'],{'secret':'synthetic-child-assignment'},epoch=1,
                        execution_id=scope['execution_id'],identity={k:scope[k] for k in ('profile_id','pid','birth')},
                        principal='synthetic-principal',limits=(10,1),refused=None))
                    ready.set()
                    return dict(type='ready',pid=123)
                # Exercise the actual maker Stop supervision and its first-stop clock.
                return await super().next_frame(timeout,ack)
            def close(self):
                self.closed.set()
                self._demand.release()
                maker_lines.put(b'')
                if self.reader is not None:
                    self.reader.join(timeout=1)
        monkeypatch.setattr(managed,'ManagedWorker',lambda p:real_worker(p) if p is probe else MakerWorker(p))
        task=asyncio.create_task(managed.execute_managed(auth,native.ref,
            dict(admission_id='synthetic-admission',principal_id='synthetic-principal',generation=2),NS(kanban_json=None)))
        await asyncio.wait_for(ready.wait(),5)
        assert await asyncio.to_thread(probe_reading.wait,5), 'probe never waited for hello'
        # Stop admission uses the actual native control latch after the probe pipe stalls.
        worker=workers[0]
        worker.interrupt()
        first=worker.stopped_at
        worker.interrupt()
        assert worker.stopped_at==first
        began=time.monotonic()
        try:
            with pytest.raises(managed.ManagedExecutionUnknown):
                await asyncio.wait_for(asyncio.shield(task),4)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        assert 1.5 <= time.monotonic()-began < 4
        assert worker.closed.is_set() and probe.poll() is not None
        assert not auth._managed_workers
    asyncio.run(scenario())
    assert all(r[0]=='terminal' for r in native.owner.execute('SELECT status FROM worker_executions'))
    assert native.owner.execute("SELECT COUNT(*) FROM worker_executions WHERE kind='child'").fetchone()[0]==1
    assert all(c.witnesses(p,c._names[p])==[] for p in native.obs.PROVIDERS)
