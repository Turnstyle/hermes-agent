"""Owner-reserved delegated children of a verified worker execution (#141793).

A worker never mints a child. It asks on its OWN fenced receipt stream (epoch, assignment,
generation, sequence), and the owner creates the child's session row and execution, binds it to
the parent's verified process identity and the worker's fresh secret, and enforces the frozen
policy's delegation caps. The child then persists through the ordinary ``worker.persist`` path
under its own claim. Expected refusals (caps, draining) are receipts, not errors: an error would
leave the parent's journal pending and refuse every later write of its turn.
"""
import secrets
from functools import partial

from hermes_state_runtime import RuntimeStoreError, apply_worker_receipt
from hermes_state_runtime_workers import CHILD_SEPARATOR, descendant_range

_DEFAULTS = {'max_concurrent_children': 10, 'max_spawn_depth': 1}


def _limit(section, key):
    try:
        return max(1, int(section.get(key, _DEFAULTS[key])))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def delegation_limits(authority, execution_id, session_id):
    """``(max_concurrent_children, max_spawn_depth)`` from the root conversation's frozen policy
    (the profile's config for a session with no local policy), never from the asking worker."""
    from gateway.session_policy import policy_for_source, profile_config
    root = execution_id.split(CHILD_SEPARATOR, 1)[0]
    if root != execution_id:
        with authority.db._read_ctx() as conn:
            row = conn.execute('SELECT session_id FROM worker_executions WHERE execution_id=?', (root,)).fetchone()
        session_id = row[0] if row else session_id
    live = authority.sessions.get(authority.logical_owner(session_id))
    policy = policy_for_source(authority.runner, live.source) if live is not None else None
    config = policy.config(authority) if policy is not None else profile_config(authority)
    section = config.get('delegation') or {}
    return _limit(section, 'max_concurrent_children'), _limit(section, 'max_spawn_depth')


def reserve_child_execution(db, conn, session_id, payload, *, epoch, execution_id, identity, principal, limits, refused):
    from hermes_state_ids import new_session_id
    from hermes_state_keys import admission_fingerprint
    from hermes_state_runtime import _secret_digest
    from hermes_state_worker_lifecycle import insert_session_row_in_transaction
    secret = payload.get('secret') if isinstance(payload, dict) else None
    if set(payload) != {'secret'} or not isinstance(secret, str) or not 16 <= len(secret) <= 256:
        raise RuntimeStoreError('invalid_params')
    max_children, max_depth = limits
    prefix = execution_id + CHILD_SEPARATOR
    live = conn.execute("SELECT COUNT(*) FROM worker_executions WHERE execution_id>=? AND execution_id<? "
                        "AND instr(substr(execution_id,?),'/')=0 AND status!='terminal'",
                        (*descendant_range(execution_id), len(prefix) + 1)).fetchone()[0]
    if refused is None and prefix.count(CHILD_SEPARATOR) > max_depth:
        refused = 'delegation_depth_exceeded'
    if refused is None and live >= max_children:
        refused = 'delegation_capacity'
    if refused is not None:
        return {'refused': refused}
    parent = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (session_id,)).fetchone()
    child_sid, child_eid = new_session_id(), prefix + secrets.token_hex(8)
    insert_session_row_in_transaction(db, conn, child_sid, 'subagent', model_config={'_delegate_from': session_id},
                                      parent_session_id=session_id, profile_name=parent['profile_name'])
    scope = dict(identity, session_id=child_sid, execution_id=child_eid, generation=0, secret=secret)
    claim = admission_fingerprint(canonical_target=child_sid, payload=scope | {'principal': principal})
    conn.execute("INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
                 "VALUES(?,?,'child',?,0,'running',?)", (child_eid, child_sid, epoch, _secret_digest(claim)))
    return {'child': {'session_id': child_sid, 'execution_id': child_eid, 'generation': 0}}


async def worker_child_request(connection, ref, params):
    """worker.persist ``child.reserve``: the parent's full claim and assignment are verified first."""
    from gateway.session_worker import _SCOPE, persist_off_loop, verify_worker_claim
    if set(params) != _SCOPE | {'epoch', 'sequence', 'operation', 'payload'}:
        raise RuntimeStoreError('invalid_params')
    custody = params['operation'] == 'custody.maker_observation'
    if custody:
        from gateway.session_custody_operation import require_custody_actor
        require_custody_actor(connection)
    verify_worker_claim(connection, ref, params)
    authority = connection.authority

    def reserve():
        if custody:
            from gateway.session_custody_operation import custody_receipt
            return custody_receipt(connection, ref, params)
        refused, limits = None, (0, 0)
        try:
            authority._require_admission_open()
            limits = delegation_limits(authority, params['execution_id'], params['session_id'])
        except RuntimeStoreError as exc:
            refused = exc.reason
        handler = partial(reserve_child_execution, epoch=params['epoch'], execution_id=params['execution_id'],
                          identity={key: params[key] for key in ('profile_id', 'pid', 'birth')},
                          principal=connection.actor.subject, limits=limits, refused=refused)
        return apply_worker_receipt(authority.db, **{key: params[key] for key in (
            'epoch', 'execution_id', 'session_id', 'generation', 'sequence', 'operation', 'payload')},
            handlers={'child.reserve': handler})
    if custody:
        from gateway.session_custody_operation import arm_custody_guard
        from gateway.session_runtime_workers import tracked_write
        def armed(result):
            arm_custody_guard(authority)
            return result
        return await tracked_write(authority, reserve, then=armed)
    return await persist_off_loop(reserve, params=params)


def reserve_reference_child(authority, launch, scope, identity, principal):
    """Owner-only fixed operation, absent from RPC dispatch and delegated worker payloads."""
    from gateway.session_custody_child import _validate_parent
    _validate_parent(authority, launch, scope)
    authority._require_admission_open()
    limits = delegation_limits(authority, scope['execution_id'], scope['session_id'])
    def write(conn):
        from hermes_state_runtime import _epoch
        _epoch(conn, scope['epoch'])
        _validate_parent(authority, launch, scope, conn=conn)
        result = reserve_child_execution(authority.db, conn, scope['session_id'],
            {'secret': secrets.token_urlsafe(32)}, epoch=scope['epoch'], execution_id=scope['execution_id'],
            identity=dict(profile_id=scope['profile_id'], pid=identity[0], birth=identity[1]),
            principal=principal, limits=limits, refused=None)
        if 'refused' in result:
            raise RuntimeStoreError(result['refused'])
        return result['child']
    return authority.db._execute_write(write, patience_s=0)
