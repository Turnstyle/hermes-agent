"""One fixed custody operation on the existing, fenced worker receipt stream."""
import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import re

from gateway.session_custody_observation import (
    ACTOR, PROVIDERS, AdmittedObservation, MakerCapture, observe_admitted,
)
from hermes_state_runtime import RuntimeStoreError, apply_worker_receipt

_SOURCE = Path(ACTOR) / 'plugins/keys-router-ops/maker_custody.py'
_OPERATION = 'custody.maker_observation'
_COMPARISON_FAILED = object()


def require_custody_actor(connection):
    if connection.actor.profile_id != ACTOR or str(connection.authority.profile_id) != ACTOR:
        raise RuntimeStoreError('permission_denied')


def _load_custody_source():
    """Fixed file only. Offline tests replace this loader with a temporary fixture loader."""
    try:
        source = _SOURCE.read_bytes()
        spec = importlib.util.spec_from_file_location('_hermes_maker_custody', _SOURCE)
        module = importlib.util.module_from_spec(spec)
        # Compile the bytes whose digest we report, without writing a bytecode cache.
        exec(compile(source, str(_SOURCE), 'exec'), module.__dict__)
        if not callable(module.association) or not callable(module.compare):
            return None
        return module, hashlib.sha256(source).hexdigest()
    except Exception:  # health: allow BLE001 -- custody module exceptions can contain private names or values
        return None


def _payload(payload):
    if type(payload) is not dict or type(payload.get('phase')) is not str:
        raise RuntimeStoreError('invalid_params')
    phase = payload['phase']
    if phase not in ('attach', 'observe', 'detach'):
        raise RuntimeStoreError('invalid_params')
    if set(payload) != ({'phase', 'names'} if phase == 'attach' else {'phase'}):
        raise RuntimeStoreError('invalid_params')
    if phase == 'attach':
        names = payload['names']
        if (type(names) is not dict or set(names) != set(PROVIDERS)
                or any(type(n) is not str or re.fullmatch(r'[A-Z][A-Z0-9_]{1,127}', n) is None
                       for n in names.values())):
            raise RuntimeStoreError('invalid_params')
    return phase


def _callbacks(module):
    def association(*args):
        try:
            return module.association(*args) is True
        except Exception:  # health: allow BLE001 -- association exceptions must not disclose custody material
            return False

    def compare(*args):
        try:
            value = module.compare(*args)
        except Exception:  # health: allow BLE001 -- comparison exceptions must not disclose custody material
            value = _COMPARISON_FAILED
        if value is None or type(value) is bool:
            return value
        raise RuntimeStoreError('invalid_custody_comparison') from None
    return association, compare


@dataclass(eq=False)
class _Binding:
    authority: object
    ref: object
    admission_id: str
    epoch: int
    capture: object
    guard: object = None


def forget_custody(authority, *, admission_id=None, expected=None, owner_conn=None):
    """End only this authority's custody admission, including idle owner teardown."""
    runner = authority.runner
    if owner_conn is not None and (owner_conn is not authority.db._conn or not owner_conn.in_transaction):
        raise RuntimeStoreError('permission_denied')
    with authority.db._lock if owner_conn is None else nullcontext():
        binding = getattr(runner, '_maker_custody_binding', None)
        if (binding is None or binding.authority is not authority
                or expected is not None and binding is not expected
                or admission_id is not None and binding.admission_id != admission_id):
            return
        del runner._maker_custody_binding
        if getattr(runner, '_maker_custody_capture', None) is binding.capture:
            del runner._maker_custody_capture
        capture = binding.capture
        # Cancel also closes pending reference transports. Forget successful witnesses too.
        with capture._lock:
            attempts = list(capture._attempts.values())
        for attempt in attempts:
            capture.forget_maker(attempt.authority)
            capture.cancel(attempt)
        if binding.guard is not None:
            binding.guard.get_loop().call_soon_threadsafe(binding.guard.cancel)


def _admission(authority, ref, params):
    prefix = 'admission-worker:'
    execution_id = params['execution_id']
    if (not isinstance(execution_id, str) or not execution_id.startswith(prefix)
            or '/' in execution_id or params['session_id'] != ref.session_id):
        raise RuntimeStoreError('permission_denied')
    return AdmittedObservation(authority, ref, execution_id[len(prefix):])


def _request(lease):
    admission = lease._owner.execute('SELECT request_id FROM session_admissions WHERE admission_id=?',
                                     (lease._admission_id,)).fetchone()
    if admission['request_id'] != 'kanban:' + json.dumps(
            ['fleet', 't_2b29710a', lease._context['run_id']], separators=(',', ':')):
        raise RuntimeStoreError('permission_denied')


def _attach(authority, ref, admission_id, payload, attached):
    if attached and getattr(authority.runner, '_maker_custody_binding', None) is attached[0]:
        return {'attached': True}  # the same receipt transaction retried after SQLite contention
    if getattr(authority.runner, '_maker_custody_capture', None) is not None:
        return {'refused': 'already_attached'}
    loaded = _load_custody_source()
    if loaded is None:
        return {'refused': 'custody_source_unavailable'}
    module, digest = loaded
    logging.getLogger(__name__).info('Maker custody source sha256=%s', digest)
    association, compare = _callbacks(module)
    try:
        capture = MakerCapture(payload['names'], association, compare)
    except RuntimeStoreError:
        return {'refused': 'association_refused'}
    authority.runner._maker_custody_capture = capture
    authority.runner._maker_custody_binding = _Binding(authority, ref, admission_id, authority.epoch, capture)
    attached.append(authority.runner._maker_custody_binding)
    return {'attached': True}


def custody_receipt(connection, ref, params):
    """The fixed handler runs only after receipt fencing, with both databases held."""
    authority = connection.authority
    require_custody_actor(connection)
    _payload(params['payload'])
    native = _admission(authority, ref, params)
    attached = []
    committed = False

    def handler(db, conn, session_id, payload):
        phase = _payload(payload)
        if phase == 'observe':
            # Even a missing capture must pass the exact admission checks first.
            with AdmittedObservation(authority, ref, native._admission_id, owner_conn=conn).lease() as lease:
                authority._require_admission_open()
                _request(lease)
            return observe_admitted(authority, ref, native._admission_id, owner_conn=conn)
        with AdmittedObservation(authority, ref, native._admission_id, owner_conn=conn).lease() as lease:
            authority._require_admission_open()
            _request(lease)
            if phase == 'attach':
                return _attach(authority, ref, native._admission_id, payload, attached)
            forget_custody(authority, owner_conn=conn)
            return {'detached': True}

    try:
        # Replays bypass handlers, so validate admission before replay readback as well.
        with native.lease():
            authority._require_admission_open()
            _request(native)
        binding = getattr(authority.runner, '_maker_custody_binding', None)
        if binding is not None and (binding.epoch != authority.epoch
                or binding.admission_id != native._admission_id):
            forget_custody(authority)
            raise RuntimeStoreError('stale_kanban_claim')
        result = apply_worker_receipt(authority.db, **{k: params[k] for k in (
            'epoch', 'execution_id', 'session_id', 'generation', 'sequence', 'operation', 'payload')},
            handlers={_OPERATION: handler})
        committed = True
        return result
    except RuntimeStoreError as exc:
        if exc.reason in ('permission_denied', 'stale_kanban_claim', 'stale_epoch', 'stale_generation', 'not_found'):
            forget_custody(authority)
        raise
    finally:
        # A failed receipt commit must not leave an unreceipted in-memory installation.
        if attached and not committed:
            forget_custody(authority, expected=attached[0])


def arm_custody_guard(authority):
    """Expire an idle capture when its native admission/claim or owner stops being live."""
    binding = getattr(authority.runner, '_maker_custody_binding', None)
    if binding is None or binding.authority is not authority or binding.guard is not None:
        return

    def check():
        if (authority.epoch != binding.epoch or getattr(authority, 'retiring', False)
                or getattr(authority, 'retired', False)):
            raise RuntimeStoreError('stale_kanban_claim')
        with AdmittedObservation(authority, binding.ref, binding.admission_id).lease() as lease:
            _request(lease)

    async def watch():
        try:
            while getattr(authority.runner, '_maker_custody_binding', None) is binding:
                await asyncio.to_thread(check)
                await asyncio.sleep(1)
        except Exception:  # health: allow BLE001 -- idle teardown must discard private exception details
            # No exception text from custody or claim readers enters logs or receipts.
            return
        finally:
            await asyncio.to_thread(forget_custody, authority, expected=binding)
    binding.guard = asyncio.create_task(watch())
