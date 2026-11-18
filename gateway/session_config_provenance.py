"""Native operator recovery of one adopted local session's frozen config provenance."""

import asyncio
from contextlib import nullcontext
from dataclasses import asdict

from gateway.session_local import publish_local_policy
from gateway.session_local_recovery import local_source
from gateway.session_persistence import authority_persistence
from gateway.session_policy import restore_policy
from gateway.session_policy_credentials import recover_config_secrets
from gateway.session_runtime_workers import track_mutation
from persistence.core.errors import MUTATION_GUARD_REFUSALS, AuthorityRefusal, busy_refusal


async def recover(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise AuthorityRefusal('permission_denied')
    store = authority_persistence(authority)
    if actor.profile_id != authority.profile_id or authority.profile_id != str(store.scope.profile_dir):
        raise AuthorityRefusal('profile_mismatch')
    if (not isinstance(ref.session_id, str) or not ref.session_id
            or not isinstance(params['old_home'], str)
            or params.get('action', 'preview') not in ('preview', 'apply', 'rollback')):
        raise AuthorityRefusal('invalid_params')
    authority._require_admission_open()
    try:
        return await asyncio.shield(track_mutation(authority, _apply(authority, ref, params)))
    except MUTATION_GUARD_REFUSALS as exc:
        raise busy_refusal(exc) from exc


async def _apply(authority, ref, params):
    store = authority_persistence(authority)
    live = authority.sessions.get(ref.session_id)

    def validate(policy):
        candidate = restore_policy(policy)
        recovered = recover_config_secrets(authority, candidate, skip=())
        if getattr(recovered, 'dropped', ()):
            raise AuthorityRefusal('launch_credentials_unavailable', field='config_secret_ref',
                                   detail='a frozen config secret no longer matches')

    async with live.mutation_lock if live is not None else nullcontext():
        receipt = await asyncio.to_thread(store.bindings.receipt, ref.session_id)
        source = local_source(authority, ref.session_id, receipt.principal_id)
        route = authority.runner.session_store._generate_session_key(source)
        outcome = await asyncio.to_thread(
            store.bindings.recover_config_secret_provenance, epoch=authority.epoch,
            session_id=ref.session_id, old_home=params['old_home'], route=route, origin=source.to_dict(),
            action=params.get('action', 'preview'), validate_policy=validate)
        result = {'session_id': ref.session_id, **asdict(outcome)}
        live = authority.sessions.get(ref.session_id)
        if params.get('action', 'preview') != 'preview' and live is not None:
            # Provenance changes no prompt or route; publish the committed policy without eviction.
            # Repeats repair a commit whose first response/publication was interrupted.
            publish_local_policy(authority, ref.session_id, evict=False)
            if outcome.status in ('applied', 'rolled_back'):
                live.event_stream.publish(ref.session_id, result, event_type='session.updated')
            if params.get('action') != 'rollback':
                authority._schedule(ref)
        return result
