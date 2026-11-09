"""Kanban's immutable event receipts into an existing local owner's FIFO.

No title lookup, new session, alternate writer, or inference at the delivery edge.
The subscription cursor may rewind after any individual admission committed.
"""
import asyncio
import hashlib
import json
import uuid

from gateway.config import Platform
from hermes_state_runtime import RuntimeStoreError, find_session_admission


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def notice_key(identity, text, category, *, owner=None, profile=None, bind_text=True):
    """The event/destination is the id; content is an independent immutable fingerprint.

    The physical transcript tip is deliberately absent: compression keeps the creation owner.
    Never include payload in the id, which would admit modified retries as fresh work.
    ``bind_text=False`` is for a producer whose rendered text is built from mutable state (the
    Kanban notifier renders the task's current title/assignee each tick): the digest then covers
    only the immutable event content, so a retry after a rename is the same event, not a conflict.
    """
    required = ('board', 'task_id', 'event_id', 'platform', 'chat_id', 'thread_id', 'notifier_profile')
    if (not isinstance(identity, dict) or any(k not in identity for k in required)
            or type(identity['event_id']) is not int or identity['event_id'] <= 0
            or any(not isinstance(identity[k], str) for k in required if k != 'event_id')
            or not identity['board'] or not identity['task_id'] or not identity['chat_id']
            or category not in ('result', 'diagnostic') or not isinstance(text, str)):
        raise RuntimeStoreError('invalid_params')
    try:
        key = {k: identity[k] for k in required}
        if owner is not None:
            # A root id and its compression child are the same registered destination.
            key['chat_id'] = owner
        key.update(owner=owner, profile=profile)
        request = 'kanban-notice:' + hashlib.sha256(_canonical(key).encode()).hexdigest()
        content = dict(payload=identity.get('payload'), category=category)
        if bind_text:
            content['text'] = text
        digest = hashlib.sha256(_canonical(content).encode()).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeStoreError('invalid_params') from exc
    return request, digest


def task_notice_metadata(metadata):
    """Persist only a complete trusted producer id/digest pair; old fingerprints stay unchanged."""
    if not {'task_notice_id', 'task_notice_digest'} & metadata.keys():
        return {}
    import re
    identity, digest = metadata.get('task_notice_id'), metadata.get('task_notice_digest')
    if (not isinstance(identity, str) or not re.fullmatch(r'kanban-notice:[0-9a-f]{64}', identity)
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise RuntimeStoreError('invalid_params')
    return dict(task_notice_id=identity, task_notice_digest=digest)


def replayed_notice(authority, owner, route, identity, text, metadata):
    """An exact immutable retry can retain its receipt after compression changed the snapshot.

    Admission's transaction still arbitrates concurrent first deliveries. Unknown/started rows
    count as admitted, not an invitation to start inference again. The digest is the sole
    content test: it already binds the text when the producer asked for that, and a producer
    whose text is rendered from mutable state must not conflict with its own first rendering.
    """
    if 'task_notice_id' not in metadata:
        return False
    row = find_session_admission(authority.db, principal_id='automation:' + route,
                                 session_id=owner, request_id=identity)
    if row is None:
        return False
    descriptor = row['payload'].get('local_automation_v1') or row['payload'].get('native_text_v1', {}).get('automation', {})
    if (descriptor.get('task_notice_digest') != metadata['task_notice_digest']
            or descriptor.get('notification_category', 'result') != metadata.get('notification_category', 'result')):
        raise RuntimeStoreError('admission_conflict')
    return True


async def _native_notice(authority, session_id, text, category, identity):
    from gateway.session_authorities import admission_owner
    from gateway.session_local_adoption import adopt_before_restore
    from gateway.session_local_recovery import restore_local_session
    from gateway.platforms.event import MessageEvent
    owner = admission_owner(authority, session_id)
    authority._require_admission_open()
    await adopt_before_restore(authority, owner)
    ref = restore_local_session(authority, owner)
    live = authority.sessions[ref.session_id]
    adapter = authority.runner._adapter_for_source(live.source)
    if adapter is None or live.source.platform != Platform.LOCAL or not adapter.authorize_source(live.source):
        raise RuntimeStoreError('permission_denied')
    metadata = {'notification_category': category}
    if identity is None:
        request = json.dumps(['kanban-wake', live.route, uuid.uuid4().hex], separators=(',', ':'))
    else:
        request, digest = notice_key(identity, text, category, owner=ref.session_id, profile=authority.profile_id,
                                     bind_text=identity.get('rendered_from_task') is not True)
        metadata.update(task_notice_id=request, task_notice_digest=digest)
    if replayed_notice(authority, ref.session_id, live.route, request, text, metadata):
        return
    entry = authority.runner.session_store.lookup_by_session_key(live.route)
    if entry is None or entry.suspended:
        raise RuntimeStoreError('not_found')
    event = MessageEvent(text=text, source=live.source, internal=True, message_id=request,
                         metadata={**metadata, 'gateway_session_key': live.route,
                                   'gateway_session_id': entry.session_id})
    await authority.admit_automation(adapter, event, request)


async def deliver_task_notification(adapter, *, session_id, text, category, identity, profile):
    """True for a recognized native destination; False ONLY for a proven ordinary API row.

    Missing authority/store/row is uncertain, not permission to self-POST native history. This
    also preserves subscriptions while a profile is parked or its owner is restarting.
    """
    from gateway.session_authorities import active_authority, served_profile_name
    from gateway.wake import WakeNotAccepted
    runner = getattr(adapter, 'gateway_runner', None)
    if not session_id and identity is None:
        return False  # old raw /loop callers creating their own API conversation
    try:
        authority = active_authority(runner) if runner is not None else None
        if authority is None:
            ensure = getattr(adapter, '_ensure_session_db', None)
            db = ensure() if callable(ensure) else None
            row = await asyncio.to_thread(db.get_session, session_id) if db is not None else None
            if row is not None and row.get('source') in ('api_server', 'bot_room'):
                return False
            raise RuntimeStoreError('runtime_coordination_required')
        if profile and profile != served_profile_name(authority.profile_id):
            raise RuntimeStoreError('profile_mismatch')
        if isinstance(identity, dict) and identity.get('notifier_profile') not in (
                None, '', served_profile_name(authority.profile_id)):
            raise RuntimeStoreError('profile_mismatch')
        ensure = getattr(adapter, '_ensure_session_db', None)
        # Select on the owner loop: a served profile's thread-local scope is not a worker's.
        if callable(ensure) and ensure() is not authority.db:
            raise RuntimeStoreError('profile_mismatch')
        row = await asyncio.to_thread(authority.db.get_session, session_id)
        if row is not None and row.get('source') in ('api_server', 'bot_room'):
            return False
        if row is None:
            raise RuntimeStoreError('not_found')
        # Restores an already bound canonical owner only. Unbound legacy CLI rows must first
        # be adopted by their authenticated native owner, never by a notifier forging a viewer.
        await _native_notice(authority, session_id, text, category, identity)
        return True
    except RuntimeStoreError as exc:
        raise WakeNotAccepted(str(exc), reason=exc.reason) from exc
    except Exception as exc:
        # An uncertain native storage/admission error is retryable, never a dead-chat counter.
        raise WakeNotAccepted('native task notification unavailable', reason='storage_unavailable') from exc
