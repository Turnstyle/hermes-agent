"""Private, fixed custody observation. No wire method, environment authority or IO readers.

The custody owner supplies association and comparison callbacks in memory. Only
nullable comparisons survive the final spawn environment's lifetime.
"""
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from types import MappingProxyType

from agent.delegation_context import is_dispatcher_owned_worker_context
from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, _session

ROOT = '/Users/turner/.hermes'
ACTOR = ROOT + '/profiles/tb-keys-router'
MAKER = ROOT + '/profiles/tb-bot-maker'
CONFIG = MAKER + '/config.yaml'
BOARD_DB = ROOT + '/kanban/boards/fleet/kanban.db'
PROVIDERS = ('fleet-relay-claude', 'fleet-relay-openai')


class MakerCapture:
    """Host-owned producer; names and callbacks never enter worker bootstrap frames."""

    def __init__(self, names, association, compare):
        from gateway.session_custody_projection import _valid_names
        if not _valid_names(names):
            raise RuntimeStoreError('permission_denied')
        for provider in PROVIDERS:
            if association(provider, names[provider], CONFIG, ('providers', provider, 'api_key')) is not True:
                raise RuntimeStoreError('permission_denied')
        self._names = dict(names)
        self._compare = compare
        self._lock = threading.Lock()
        self._launches = {}
        self._attempts = {}
        self._pending = {}

    def forget_maker(self, authority):
        if str(authority.profile_id) == MAKER:
            with self._lock:
                self._launches.pop(MAKER, None)

    def begin(self, authority, ref, row):
        if str(authority.profile_id) != MAKER:
            return None
        self.cancel(self._attempts.get(MAKER))
        attempt = _Attempt(authority, ref.session_id, row['admission_id'],
                           row['generation'], authority.epoch)
        with self._lock:
            self._launches.pop(MAKER, None)
            self._attempts[MAKER] = attempt
        return attempt

    def cancel(self, attempt):
        with self._lock:
            pending = self._pending.pop(attempt, None)
            if self._attempts.get(MAKER) is attempt:
                self._attempts.pop(MAKER, None)
                self._launches.pop(MAKER, None)
        if pending is not None and pending.reference is not None:
            pending.reference.close()

    def capture(self, authority, env, attempt=None):
        self.forget_maker(authority)
        if str(authority.profile_id) != MAKER or env is None or attempt is None:
            return None
        # Never retain the environment, even in a closure. Exceptions propagate untouched.
        available = {}
        for provider in PROVIDERS:
            value = self._compare(provider, self._names[provider], MappingProxyType(env))
            if value is not None and type(value) is not bool:
                raise RuntimeStoreError('invalid_custody_comparison')
            available[provider] = value
        with self._lock:
            if self._attempts.get(MAKER) is not attempt or attempt.authority is not authority:
                raise RuntimeStoreError('stale_generation')
            launch = _Launch(available, attempt)
            self._pending[attempt] = launch
        return launch

    def reserved(self, authority, launch, scope, process):
        if launch is None:
            return
        attempt = launch.attempt
        if (attempt.authority is not authority or launch.process is not process
                or scope.get('profile_id') != MAKER
                or scope.get('session_id') != attempt.session_id
                or scope.get('execution_id') != 'admission-worker:' + attempt.admission_id
                or scope.get('generation') != attempt.generation or scope.get('epoch') != attempt.epoch):
            raise RuntimeStoreError('stale_generation')
        with self._lock:
            if self._attempts.get(MAKER) is not attempt or launch.reservation is not None:
                raise RuntimeStoreError('stale_generation')
            launch.reservation = tuple(scope[k] for k in _RESERVATION_KEYS)

    def ready(self, authority, launch, scope):
        if launch is None:
            return
        attempt = launch.attempt
        if (launch.reservation is None
                or launch.reservation != tuple(scope.get(k) for k in _RESERVATION_KEYS)
                or authority.executing.get(attempt.session_id) != attempt.generation
                or attempt.authority is not authority or str(authority.profile_id) != MAKER
                or scope.get('profile_id') != MAKER
                or scope.get('session_id') != attempt.session_id
                or scope.get('execution_id') != 'admission-worker:' + attempt.admission_id
                or scope.get('generation') != attempt.generation
                or scope.get('epoch') != attempt.epoch or authority.epoch != attempt.epoch):
            raise RuntimeStoreError('stale_generation')
        def check(conn):
            _epoch(conn, scope['epoch'])
            row = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                               (scope['execution_id'],)).fetchone()
            if not row or any(row[k] != scope[v] for k, v in (
                    ('session_id', 'session_id'), ('generation', 'generation'), ('owner_epoch', 'epoch'))):
                raise RuntimeStoreError('stale_generation')
            admission = _admission(conn, attempt.admission_id)
            if (admission['status'] != 'started' or admission['generation'] != attempt.generation
                    or admission['owner_epoch'] != attempt.epoch
                    or admission['target_session_id'] != attempt.session_id
                    or _session(conn, attempt.session_id)['runtime_generation'] != attempt.generation):
                raise RuntimeStoreError('stale_generation')
            if row['status'] != 'running':
                raise RuntimeStoreError('producer_not_started')
        authority.db._execute_write(check, patience_s=0)
        with self._lock:
            if self._attempts.get(MAKER) is not attempt or launch.scope is not None:
                raise RuntimeStoreError('stale_generation')
            launch.scope = (scope['execution_id'], scope['generation'], scope['epoch'])
            self._launches[str(authority.profile_id)] = launch

    def succeeded(self, authority, launch, scope):
        if launch is None:
            return
        from gateway.session_custody_child import _validate_parent
        _validate_parent(authority, launch, scope, terminal=True)
        with self._lock:
            if self._launches.get(MAKER) is not launch or launch.succeeded:
                raise RuntimeStoreError('stale_generation')
            launch.succeeded = True
            self._pending.pop(launch.attempt, None)

    def discard(self, launch):
        if launch is None:
            return
        self.cancel(launch.attempt)
        with self._lock:
            for home, current in list(self._launches.items()):
                if current is launch:
                    del self._launches[home]

    def witnesses(self, provider, name):
        if provider not in PROVIDERS or name != self._names[provider]:
            raise RuntimeStoreError('permission_denied')
        with self._lock:
            launch = self._launches.get(MAKER)
            if launch is None or not launch.succeeded:
                return []
            attempt = launch.attempt
            if (attempt.authority.epoch != attempt.epoch
                    or attempt.authority.executing.get(attempt.session_id, attempt.generation) != attempt.generation
                    or self._attempts.get(MAKER) is not attempt):
                return []
            result = [dict(kind='actual_maker_launch', available=launch.available[provider],
                         resource_root=ROOT, maker_home=MAKER, maker_config=CONFIG,
                         launch_capture_id=launch.capture_id, executed=True, reference_only=False)]
            from gateway.session_custody_child import ReferenceChild
            child = launch.reference.result_for(launch) if type(launch.reference) is ReferenceChild else None
            if child is not None:
                result.append(dict(kind='prospective_reference_child',
                    available=child[provider], resource_root=ROOT,
                    maker_home=MAKER, maker_config=CONFIG, launch_capture_id=launch.capture_id,
                    executed=True, reference_only=True))
            return result


_RESERVATION_KEYS = ('profile_id', 'session_id', 'execution_id', 'generation', 'epoch', 'pid', 'birth')


@dataclass(frozen=True, eq=False)
class _Attempt:
    authority: object
    session_id: str
    admission_id: str
    generation: int
    epoch: int


class _Launch:
    __slots__ = ('available', 'capture_id', 'scope', 'attempt', 'reservation', 'process', 'reference', 'succeeded')

    def __init__(self, available, attempt):
        self.attempt = attempt
        self.reservation = None
        self.process = None
        self.reference = None
        self.succeeded = False
        self.available = available
        self.capture_id = uuid.uuid4().hex
        self.scope = None


def producer(authority):
    return getattr(getattr(authority, 'runner', None), '_maker_custody_capture', None)


def capture_spawn(authority, popen, args, *, attempt=None, reference_cwd=None, **kwargs):
    """Runs in acquire_process's actual Popen thread, after _worker_env's overlays."""
    capture = producer(authority)
    launch = capture.capture(authority, kwargs.get('env'), attempt) if capture is not None else None
    try:
        process = popen(args, **kwargs)
    except BaseException:
        if capture is not None:
            capture.cancel(attempt)
        raise
    if launch is not None:
        launch.process = process
        if reference_cwd is not None:
            from gateway.session_custody_child import ReferenceChild
            try:
                reference = ReferenceChild(popen, env=kwargs['env'], cwd=reference_cwd)
                with capture._lock:
                    current = (capture._attempts.get(MAKER) is attempt
                               and capture._pending.get(attempt) is launch)
                    if current:
                        launch.reference = reference
                if not current:
                    reference.close()
                    raise RuntimeStoreError('stale_generation')
            except BaseException:
                capture.cancel(attempt)
                from gateway.session_managed_worker import ManagedWorker
                ManagedWorker(process).close()
                raise
    return process, launch


def _read_only(action, *args):
    return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                                         sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY


class AdmittedObservation:
    """Construct only in the native owner's private handler, never from RPC params.

    Session and admission are resolved against the owner's live policy and ledger.
    Both databases hold IMMEDIATE transactions throughout the projection callbacks.
    """
    authorized_tenant = 'turnerbook'

    def __init__(self, authority, ref, admission_id, *, owner_conn=None):
        self._authority, self._ref, self._admission_id = authority, ref, admission_id
        self._owner_conn = owner_conn
        self._active = False

    @contextmanager
    def _owner_transaction(self):
        db = self._authority.db
        if self._owner_conn is not None:
            if self._owner_conn is not db._conn or not self._owner_conn.in_transaction:
                raise RuntimeStoreError('permission_denied')
            yield
        else:
            if db._conn is None or db._conn.in_transaction:
                raise RuntimeStoreError('permission_denied')
            db._conn.execute('BEGIN IMMEDIATE')
            try:
                yield
            finally:
                db._conn.rollback()

    @contextmanager
    def lease(self):
        if self._active or not is_dispatcher_owned_worker_context():
            raise RuntimeStoreError('permission_denied')
        authority = self._authority
        if str(authority.profile_id) != ACTOR:
            raise RuntimeStoreError('permission_denied')
        from gateway.session_managed_worker import managed_policy
        policy = managed_policy(authority, self._ref)
        if policy is None or policy.source != 'kanban':
            raise RuntimeStoreError('permission_denied')
        context = json.loads(policy.kanban_json)
        if any(context.get(k) != v for k, v in (
                ('task_id', 't_2b29710a'), ('board', 'fleet'),
                ('tenant', 'turnerbook'), ('profile', 'tb-keys-router'))):
            raise RuntimeStoreError('permission_denied')
        if context.get('db') != BOARD_DB:
            raise RuntimeStoreError('permission_denied')
        from hermes_cli.kanban_db_connect import write_txn
        db = authority.db
        self._active = True
        try:
            # The native SessionDB writer lock also fences same-process owner writers.
            from hermes_state_observation import observation_scope
            with observation_scope(db), (db._lock if self._owner_conn is None else nullcontext()):
                db._raise_if_db_replaced()
                with self._owner_transaction(), closing(sqlite3.connect(context['db'], timeout=0)) as board:
                    board.row_factory = sqlite3.Row
                    with write_txn(board):
                        self._owner, self._board, self._context = db._conn, board, context
                        self._epoch = authority.epoch
                        self._generation = authority.executing.get(self._ref.session_id)
                        self.revalidate()
                        self.admitted = self.current_context()
                        # Prevent reentrant callbacks from mutating through native connections.
                        db._conn.set_authorizer(_read_only)
                        board.set_authorizer(_read_only)
                        try:
                            from hermes_state_observation import board_observation_scope
                            with board_observation_scope(context['db']):
                                yield self
                                self.revalidate()
                        finally:
                            db._conn.set_authorizer(None)
                            board.set_authorizer(None)
        finally:
            self._active = False
            self._owner = self._board = None

    def current_context(self):
        return dict(actor_home=ACTOR, task_id='t_2b29710a', board='fleet',
                    tenant='turnerbook', resource_root=ROOT, source='kanban',
                    dispatcher_owned=True, session_id=self._ref.session_id,
                    execution_id='admission-worker:' + self._admission_id, epoch=self._epoch)

    def revalidate(self):
        if not self._active or not is_dispatcher_owned_worker_context():
            raise RuntimeStoreError('permission_denied')
        authority = self._authority
        from gateway.session_managed_worker import managed_policy
        current = managed_policy(authority, self._ref)
        if (str(authority.profile_id) != ACTOR or current is None or current.source != 'kanban'
                or json.loads(current.kanban_json) != self._context):
            raise RuntimeStoreError('permission_denied')
        _epoch(self._owner, self._epoch)
        if authority.epoch != self._epoch:
            raise RuntimeStoreError('stale_epoch')
        admission = _admission(self._owner, self._admission_id)
        sid = self._ref.session_id
        generation = self._generation
        if (generation is None or authority.executing.get(sid) != generation
                or admission['target_session_id'] != sid or admission['status'] != 'started'
                or admission['owner_epoch'] != self._epoch or admission['generation'] != generation
                or _session(self._owner, sid)['runtime_generation'] != generation):
            raise RuntimeStoreError('stale_generation')
        worker = self._owner.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                                    ('admission-worker:' + self._admission_id,)).fetchone()
        if (worker is None or worker['status'] != 'running' or worker['session_id'] != sid
                or worker['owner_epoch'] != self._epoch or worker['generation'] != generation):
            raise RuntimeStoreError('stale_generation')
        c = self._context
        from agent.kanban_turn_recovery import worker_claim_is_live
        if not worker_claim_is_live(task_id=c['task_id'], db_path=c['db'],
                                    run_id=str(c['run_id']), claim_lock=c['claim_lock'], verify_pid=False):
            raise RuntimeStoreError('stale_kanban_claim')
        task = self._board.execute('SELECT * FROM tasks WHERE id=?', (c['task_id'],)).fetchone()
        run = self._board.execute('SELECT * FROM task_runs WHERE id=?', (c['run_id'],)).fetchone()
        if (not task or not run or task['status'] != 'running' or run['status'] != 'running'
                or task['current_run_id'] != c['run_id'] or run['task_id'] != c['task_id']
                or task['claim_lock'] != c['claim_lock'] or run['claim_lock'] != c['claim_lock']
                or run['ended_at'] is not None or task['tenant'] != 'turnerbook'
                or task['assignee'] != 'tb-keys-router'
):
            raise RuntimeStoreError('stale_kanban_claim')
        for kind in ('owner_admitted', 'worker_bound'):
            row = self._board.execute('SELECT payload FROM task_events WHERE task_id=? AND run_id=? '
                                      'AND kind=? ORDER BY id DESC LIMIT 1',
                                      (c['task_id'], c['run_id'], kind)).fetchone()
            event = json.loads(row[0]) if row else {}
            if kind == 'owner_admitted':
                valid = (event.get('session_id') == sid and event.get('db') == str(authority.db.db_path)
                         and event.get('request_id') == 'kanban:' + json.dumps(
                             ['fleet', 't_2b29710a', c['run_id']], separators=(',', ':')))
            else:
                # Worker identity lives in the private reservation, not in worker_executions schema.
                bound = getattr(authority, '_managed_workers', {}).get(sid)
                valid = (bound is not None and bound.worker is not None
                         and event.get('pid') == bound.worker[0]
                         and event.get('claim_lock') == c['claim_lock']
                         and getattr(bound, '_reservation_identity', None) == (
                             ACTOR, sid, 'admission-worker:' + self._admission_id, generation,
                             self._epoch, *bound.worker))
            if not valid:
                raise RuntimeStoreError('stale_kanban_claim')

    def observe_reference(self, provider, approved_name):
        self.revalidate()
        capture = producer(self._authority)
        result = [] if capture is None else capture.witnesses(provider, approved_name)
        self.revalidate()
        return result


def observe_admitted(authority, ref, admission_id, *, owner_conn=None):
    """Custody's in-process consumer. Intentionally absent from the RPC/tool table."""
    from gateway.session_custody_projection import observe
    capture = producer(authority)
    if capture is None:
        from gateway.session_custody_projection import _unknown
        return _unknown('native_observer_unavailable')
    return observe(dict(capture._names), native=AdmittedObservation(
        authority, ref, admission_id, owner_conn=owner_conn))
