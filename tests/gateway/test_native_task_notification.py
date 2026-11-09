"""Offline regression contract: immutable task notices use the canonical owner's FIFO."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session_contract import SessionActor
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_automation_notification_category import _authority


def notice(event=17, **changes):
    return dict(board='/fixture/boards/fleet/kanban.db', task_id='task-a',
                event_id=event, platform='api_server', chat_id='owner-destination',
                thread_id='', notifier_profile='default',
                payload={'kind': 'completed', 'summary': 'handoff'}, **changes)


async def local_owner(tmp_path, monkeypatch):
    from gateway.session_local import create_local_session
    runner, authority = await _authority(tmp_path, monkeypatch)
    # The real GatewayRunner owns the async facade, not the sync test store handle.
    from hermes_state import AsyncSessionDB
    runner._session_db = AsyncSessionDB(authority.db)
    actor = SessionActor('test-owner', 'default', frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')
    ref = create_local_session(authority, actor, dict(request_id='native-owner', source='cli', model='fixture', toolsets=[]))
    # Not titled Bot Chat: notifications must target the registered owner, not retarget by title.
    authority.db.append_message(ref.session_id, 'user', 'prior project context')
    authority.db.append_message(ref.session_id, 'assistant', 'prior answer')
    adapter = SimpleNamespace(supports_async_delivery=False, gateway_runner=runner)
    return runner, authority, ref, adapter


async def wake(adapter, ref, identity, text='worker handoff', category='result'):
    from gateway.wake import deliver_wake
    await deliver_wake(adapter, text=text, session_id=ref.session_id,
                       notification_category=category, notification_identity=identity)


@pytest.mark.asyncio
async def test_raw_cli_destination_uses_automation_before_api_write(tmp_path, monkeypatch):
    _, authority, ref, adapter = await local_owner(tmp_path, monkeypatch)
    monkeypatch.setattr('gateway.wake._self_post_chat_completion', AsyncMock(side_effect=AssertionError('second API writer')))
    before = authority.db.get_messages_as_conversation(ref.session_id)
    try:
        await wake(adapter, ref, notice())
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert len(rows) == 1
        assert rows[0]['principal_id'].startswith('automation:')
        assert rows[0]['payload']['text'] == 'worker handoff'
        assert rows[0]['payload']['local_automation_v1']['owner'] == ref.session_id
        assert authority.db.get_messages_as_conversation(ref.session_id) == before
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_replay_and_concurrent_watchers_admit_one_row(tmp_path, monkeypatch):
    _, authority, ref, adapter = await local_owner(tmp_path, monkeypatch)
    try:
        await asyncio.gather(*(wake(adapter, ref, notice()) for _ in range(4)))
        await wake(adapter, ref, deepcopy(notice()))
        assert len(list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['text', 'payload'])
async def test_identity_reuse_with_changed_payload_is_conflict(tmp_path, monkeypatch, change):
    from gateway.wake import WakeNotAccepted
    _, authority, ref, adapter = await local_owner(tmp_path, monkeypatch)
    try:
        await wake(adapter, ref, notice())
        identity = notice()
        if change == 'payload':
            identity['payload']['summary'] = 'tampered'
        with pytest.raises(WakeNotAccepted) as refusal:
            await wake(adapter, ref, identity, text='changed' if change == 'text' else 'worker handoff')
        assert refusal.value.reason == 'admission_conflict'
        assert len(list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_distinct_events_and_categories_remain_distinct_fifo_inputs(tmp_path, monkeypatch):
    _, authority, ref, adapter = await local_owner(tmp_path, monkeypatch)
    try:
        await wake(adapter, ref, notice(16), text='failure', category='diagnostic')
        await wake(adapter, ref, notice(17), text='success')
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert [r['payload']['text'] for r in rows] == ['failure', 'success']
        assert [r['payload']['local_automation_v1'].get('notification_category', 'result') for r in rows] == ['diagnostic', 'result']
        # The ACK for the first group was lost. Retrying the entire claim adds nothing.
        await wake(adapter, ref, notice(16), text='failure', category='diagnostic')
        await wake(adapter, ref, notice(17), text='success')
        assert len(list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)) == 2
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_recognized_native_refusal_never_falls_back_to_api_writer(tmp_path, monkeypatch):
    from gateway.wake import WakeNotAccepted
    _, authority, ref, adapter = await local_owner(tmp_path, monkeypatch)
    monkeypatch.setattr('gateway.wake._self_post_chat_completion', AsyncMock(side_effect=AssertionError('fallback forbidden')))
    async def refuse(*args, **kwargs):
        raise RuntimeStoreError('runtime_draining')
    monkeypatch.setattr(authority, 'admit_automation', refuse)
    try:
        with pytest.raises(WakeNotAccepted) as refusal:
            await wake(adapter, ref, notice())
        assert refusal.value.reason == 'runtime_draining'
        assert not list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_notifier_native_refusal_preserves_exact_subscription_after_twelve_retries(monkeypatch):
    from gateway.kanban_watchers_notifier import _KanbanNotification
    from gateway.wake import WakeNotAccepted
    sub = dict(task_id='task-a', platform='api_server', chat_id='raw-cli-id', thread_id='',
               notifier_profile='default', delivery_mode='wake', last_event_id=0,
               delivery_metadata={'parent_chat_id': 'keep', 'scope_id': 'scope'})
    original = deepcopy(sub)
    event = SimpleNamespace(id=17, kind='completed', payload={'summary': 'handoff'})
    task = SimpleNamespace(title='task', assignee='worker', status='done', session_id='worker-id', result='handoff')
    adapter = SimpleNamespace(supports_async_delivery=False)
    runner = SimpleNamespace(adapters={Platform.API_SERVER: adapter}, config=SimpleNamespace(multiplex_profiles=False),
                             _authorization_adapter=lambda *args: adapter)
    notification = _KanbanNotification(runner, dict(sub=sub, task=task, events=[event], board='fleet', old_cursor=0, cursor=17),
                                      platform_cls=Platform, sub_fail_counts={})
    notification.rewind = AsyncMock()
    notification.advance = AsyncMock()
    notification.unsub = AsyncMock()
    notification.wake = AsyncMock(side_effect=WakeNotAccepted(reason='runtime_coordination_required'))
    for _ in range(15):
        await notification.deliver()
    assert sub == original
    assert notification.rewind.await_count == 15
    notification.advance.assert_not_awaited()
    notification.unsub.assert_not_awaited()
    assert notification.sub_fail_counts == {}


@pytest.mark.asyncio
async def test_notifier_passes_immutable_identity_for_every_event(monkeypatch):
    from gateway.kanban_watchers_notifier import _KanbanNotification
    events = [SimpleNamespace(id=16, kind='crashed', payload={}),
              SimpleNamespace(id=17, kind='completed', payload={'summary': 'done'})]
    sub = dict(task_id='task-a', platform='api_server', chat_id='raw-cli-id', thread_id='',
               notifier_profile='default', delivery_mode='wake', delivery_metadata={'scope_id': 'keep'})
    task = SimpleNamespace(title='task', assignee='worker', status='done', session_id='worker-id', result='done')
    adapter = SimpleNamespace(supports_async_delivery=False)
    runner = SimpleNamespace(adapters={Platform.API_SERVER: adapter}, config=SimpleNamespace(multiplex_profiles=False),
                             _authorization_adapter=lambda *args: adapter)
    n = _KanbanNotification(runner, dict(sub=sub, task=task, events=events, board='fleet', old_cursor=0, cursor=17),
                            platform_cls=Platform, sub_fail_counts={})
    n.advance = AsyncMock()
    n.rewind = AsyncMock()
    calls = []
    async def capture(adapter, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr('gateway.wake.deliver_wake', capture)
    await n.deliver()
    assert len(calls) == 2, 'one immutable event per admission, not mutable claim-window batches'
    assert [c['notification_identity']['event_id'] for c in calls] == [16, 17]
    assert [c['notification_category'] for c in calls] == ['diagnostic', 'result']
    assert all(c['notification_identity']['chat_id'] == sub['chat_id'] for c in calls)
    assert all(c['session_id'] == sub['chat_id'] for c in calls)
    assert calls[0]['notification_identity']['board']
    n.advance.assert_awaited_once()
