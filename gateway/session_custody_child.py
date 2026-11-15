"""Private, single-use reference process bound to one finalized maker launch."""
import asyncio
import secrets
from dataclasses import dataclass
import subprocess
import sys

from agent.managed_reference_worker import PROVIDERS
from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, _session


class ReferenceChild:
    def __init__(self, popen, *, env, cwd):
        # Same finalized mapping as the maker Popen. Never retain it or place values on a pipe.
        self.process = popen([sys.executable, '-m', 'agent.managed_reference_worker'],
                             env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, close_fds=True)
        from gateway.session_managed_worker import ManagedWorker
        self.transport = ManagedWorker(self.process)
        self.challenge = secrets.token_hex(32)
        self.consumed = False
        self.child = None
        self._completion = None

    @property
    def available(self):
        return self._completion.available if self._completion is not None else None

    def result_for(self, launch):
        done = self._completion
        if (done is None or done.launch is not launch or launch.reference is not self
                or done.reservation != launch.reservation or done.process is not self.process
                or done.capture_id != launch.capture_id
                or done.child != tuple(self.child[k] for k in ('session_id','execution_id','generation'))):
            return None
        return done.available

    def close(self):
        self.transport.close()

    async def complete(self, authority, launch, scope, names):
        if self.consumed or launch.reference is not self:
            raise RuntimeStoreError('stale_generation')
        self.consumed = True
        from gateway.session_worker_reservation import verify_worker_identity, owner_worker_subject
        hello = await self.transport.next_frame(60, ack=0)
        identity = await asyncio.to_thread(verify_worker_identity, self.process, hello)
        from gateway.session_worker_child import reserve_reference_child
        self.child = await asyncio.to_thread(reserve_reference_child, authority, launch, scope, identity, owner_worker_subject())
        await asyncio.to_thread(self.transport.send, {'type': 'reference.run', 'challenge': self.challenge, 'names': names})
        frame = await self.transport.next_frame(10, ack=0)
        if (set(frame) != {'type', 'challenge', 'available'} or frame['type'] != 'reference.complete'
                or frame['challenge'] != self.challenge or type(frame['available']) is not dict
                or set(frame['available']) != set(PROVIDERS)
                or any(type(v) is not bool for v in frame['available'].values())):
            raise RuntimeStoreError('invalid_worker_frame')
        if await asyncio.to_thread(self.process.wait, timeout=10) != 0:
            raise RuntimeStoreError('managed_worker_failed')
        # Revalidate after the process result, then retire through the native child lifecycle.
        from hermes_state_runtime import mutate_worker_execution
        await asyncio.to_thread(_validate_parent, authority, launch, scope)
        await asyncio.to_thread(mutate_worker_execution, authority.db, epoch=scope['epoch'], **self.child,
                                sequence=1, operation='execution.finish', payload={})
        from types import MappingProxyType
        self._completion = _Completion(launch, launch.reservation, self.process, launch.capture_id,
                                       tuple(self.child[k] for k in ('session_id','execution_id','generation')),
                                       identity, MappingProxyType(dict(frame['available'])))
        return self.available


def _validate_parent(authority, launch, scope, *, terminal=False, conn=None):
    from gateway.session_custody_observation import MAKER, producer, _RESERVATION_KEYS
    capture = producer(authority)
    attempt = launch.attempt
    if (capture is None or capture._attempts.get(MAKER) is not attempt
            or attempt.authority is not authority or str(authority.profile_id) != MAKER
            or authority.epoch != attempt.epoch or scope['epoch'] != attempt.epoch
            or authority.executing.get(attempt.session_id) != attempt.generation
            or launch.reservation != tuple(scope.get(k) for k in _RESERVATION_KEYS)):
        raise RuntimeStoreError('stale_generation')

    bound = getattr(authority, '_managed_workers', {}).get(attempt.session_id)
    if (bound is None or bound.process is not launch.process
            or getattr(bound, '_custody_launch', None) is not launch
            or getattr(bound, '_reservation_identity', None) != launch.reservation):
        raise RuntimeStoreError('stale_generation')

    def check(conn):
        _epoch(conn, attempt.epoch)
        admission = _admission(conn, attempt.admission_id)
        row = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                           (scope['execution_id'],)).fetchone()
        if (admission['status'] != 'started' or admission['owner_epoch'] != attempt.epoch
                or admission['generation'] != attempt.generation
                or admission['target_session_id'] != attempt.session_id
                or _session(conn, attempt.session_id)['runtime_generation'] != attempt.generation
                or row is None or row['status'] != ('terminal' if terminal else 'running')
                or row['owner_epoch'] != attempt.epoch or row['generation'] != attempt.generation
                or row['session_id'] != attempt.session_id):
            raise RuntimeStoreError('stale_generation')
    if conn is not None:
        check(conn)
    else:
        authority.db._execute_write(check, patience_s=0)


@dataclass(frozen=True)
class _Completion:
    launch: object
    reservation: tuple
    process: object
    capture_id: str
    child: tuple
    identity: tuple
    available: object
