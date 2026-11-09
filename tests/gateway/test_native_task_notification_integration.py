"""Offline notifier -> canonical FIFO proof, with unchanged API writer negative control.

No external model or daemon: the real agent's provider is an owned loopback HTTP endpoint.
Only provider bytes and clock-free admission scheduling are controlled. Persistence, context,
route restoration, API refusal, executor, settlement and local adapter are production code.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import run  # bootstrap before the per-test home-I/O guard
import run_agent  # the real executor has the same early-update bootstrap
from tests.gateway.test_session_end_delegations import _local


def _event(event_id, kind, payload):
    return SimpleNamespace(id=event_id, kind=kind, payload=payload)


def _notification(runner, api, ref, monkeypatch, events):
    from gateway import kanban_watchers_notifier as notifier
    from gateway.config import Platform
    sub = dict(task_id='t_notice', platform='api_server', chat_id=ref.session_id, thread_id='',
               notifier_profile='default', delivery_mode='wake', last_event_id=0, delivery_metadata={})
    task = SimpleNamespace(title='Native notice task', assignee='worker', status='done',
                           session_id='worker-id', result='done')
    claim = dict(sub=sub, task=task, events=events, board='native', old_cursor=0, cursor=events[-1].id,
                 board_identity='/fixture/boards/native/tasks.db')
    # Select the already-created API surface without relying on live adapter configuration.
    monkeypatch.setattr(notifier, '_adapter_for_subscription', lambda *args: api)
    n = notifier._KanbanNotification(runner, claim, platform_cls=Platform, sub_fail_counts={})
    n.advance, n.rewind, n.unsub = AsyncMock(), AsyncMock(), AsyncMock()
    return n


def _notice(owner, event_id=101, payload=None, **overrides):
    return dict(board='/fixture/boards/native/tasks.db', task_id='t_notice', event_id=event_id,
                platform='api_server', chat_id=owner, thread_id='', notifier_profile='default',
                payload=payload if payload is not None else {'kind': 'completed', 'payload': {'summary': 'done'}},
                **overrides)


def _rows(authority, ref):
    from hermes_state_runtime import list_session_admissions
    return list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)


@pytest.fixture
def offline_model():
    peer = ThreadingHTTPServer(('127.0.0.1', 0), _Model)
    peer.requests = []
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    try:
        yield peer
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _api(runner, db, monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    # Store selection only: run_api_turn, API admission and its source guard are unchanged.
    monkeypatch.setattr(adapter, '_ensure_session_db', lambda: db)
    return adapter


class _Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        message = {'role': 'assistant', 'content': 'CANONICAL_NOTICE_ASSISTANT'}
        result = {'id': 'offline-notice', 'object': 'chat.completion', 'created': 1,
                  'model': 'notice-fixture', 'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}],
                  'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}}
        kind, data = 'application/json', json.dumps(result).encode()
        if body.get('stream'):
            data = ('data: ' + json.dumps({'id': 'offline-notice', 'object': 'chat.completion.chunk',
                'created': 1, 'model': 'notice-fixture', 'choices': [{'index': 0, 'delta': message,
                'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('created_source', ['cli', 'api_server'])
async def test_observed_api_refusal_then_real_notifier_persists_context_and_answer(tmp_path, monkeypatch, created_source, offline_model):
    from gateway import kanban_watchers_notifier as notifier, run, wake
    from gateway.session_api_turn import run_api_turn
    from gateway.session_results import admission_result
    from hermes_state_local import local_receipt
    from hermes_state_runtime import RuntimeStoreError
    peer = offline_model
    # This test must never reach a configured external model, proxy or ancillary service.
    import socket
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def permitted(address):
        assert (isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1')
                and address[1] == peer.server_port), ('non-fixture network attempt', address)
    def offline_connect(sock, address):
        permitted(address)
        return connect(sock, address)
    def offline_connect_ex(sock, address):
        permitted(address)
        return connect_ex(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', offline_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', offline_connect_ex)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,::1,localhost')
    cfg = {'model': {'provider': 'custom', 'default': 'notice-fixture',
                    'base_url': f'http://127.0.0.1:{peer.server_port}/v1', 'api_key': 'offline-only'},
           'platform_toolsets': {'cli': []}, 'agent': {'api_max_retries': 0},
           'auxiliary': {'title_generation': {'enabled': False}}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda: cfg)
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-only')
    monkeypatch.setenv('OPENAI_BASE_URL', cfg['model']['base_url'])
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    # The reported Bot Chat has current source=cli but historical created_source=api_server.
    with authority.db._lock:
        authority.db._conn.execute('UPDATE sessions SET created_source=? WHERE id=?',
                                   (created_source, ref.session_id))
        authority.db._conn.commit()
    api = _api(runner, authority.db, monkeypatch)
    post = AsyncMock(side_effect=AssertionError('native notice must not self-POST'))
    monkeypatch.setattr(wake, '_self_post_chat_completion', post)
    try:
        assert authority.db.get_session(ref.session_id)['source'] == 'cli'
        before = authority.db.get_session(ref.session_id)
        before_identity = (before['created_source'], before['session_key'], before['chat_id'], before['user_id'])
        assert before['created_source'] == created_source
        before_live = authority.sessions[ref.session_id]
        binding = lambda live: (live.route, live.source.platform, live.source.chat_id, live.source.profile)
        before_binding = binding(before_live)
        before_receipt = local_receipt(authority.db, ref.session_id)
        before_owner = (before_receipt['session_id'], before_receipt['policy']['source'])
        assert before_owner[1] == 'cli'
        # Observe the actual writer fence that caused the original failure. No mock raises it.
        with pytest.raises(RuntimeStoreError) as rejected:
            await run_api_turn(api, user_message='old self-POST path', session_id=ref.session_id,
                               conversation_history=[], history_from_session=True)
        assert rejected.value.reason == 'permission_denied'
        assert _rows(authority, ref) == [] and peer.requests == []
        authority.db.append_message(ref.session_id, 'user', 'EXISTING_NATIVE_CONTEXT')
        authority.db.append_message(ref.session_id, 'assistant', 'EXISTING_NATIVE_ANSWER')
        n = _notification(runner, api, ref, monkeypatch,
                          [_event(101, 'completed', {'summary': 'NATIVE_EVENT_SUMMARY'})])
        await n.deliver()
        n.advance.assert_awaited_once()
        n.rewind.assert_not_awaited()
        rows = _rows(authority, ref)
        assert len(rows) == 1 and rows[0]['status'] == 'queued'
        assert rows[0]['payload']['local_automation_v1']['task_notice_digest']
        await asyncio.wait_for(authority._drain(ref), 30)
        settled = _rows(authority, ref)[0]
        assert (settled['status'], settled['outcome']) == ('terminal', 'completed'), settled
        result = admission_result(authority.db, settled['admission_id'])
        assert result['result']['final_response'] == 'CANONICAL_NOTICE_ASSISTANT'
        messages = authority.db.get_messages(ref.session_id)
        assert any(m['role'] == 'user' and 'NATIVE_EVENT_SUMMARY' in str(m['content']) for m in messages)
        assert any(m['role'] == 'assistant' and m['content'] == 'CANONICAL_NOTICE_ASSISTANT' for m in messages)
        assert any('EXISTING_NATIVE_CONTEXT' in json.dumps(body.get('messages', [])) for body in peer.requests)
        assert any('EXISTING_NATIVE_ANSWER' in json.dumps(body.get('messages', [])) for body in peer.requests)
        assert any('NATIVE_EVENT_SUMMARY' in json.dumps(body.get('messages', [])) for body in peer.requests)
        # Every ordinary local turn's peer refresh relabels an interactive creation label (cli/tui/gui)
        # to the transport's ``local`` (session_a2a.storage_source; pinned for ordinary turns by
        # test_resume_latest_local_cli). The notice runs that same executor and adds no writer. What it must
        # preserve is creation identity and ownership, so assert those exactly rather than the transport label.
        after = authority.db.get_session(ref.session_id)
        assert after['source'] == 'local'
        assert (after['created_source'], after['session_key'], after['chat_id'], after['user_id']) == before_identity
        receipt = local_receipt(authority.db, ref.session_id)
        assert (receipt['session_id'], receipt['policy']['source']) == before_owner
        assert binding(authority.sessions[ref.session_id]) == before_binding
        # The unchanged API writer still refuses this native row, and the refusal admitted nothing.
        with pytest.raises(RuntimeStoreError) as refused_again:
            await run_api_turn(api, user_message='old self-POST path', session_id=ref.session_id,
                               conversation_history=[], history_from_session=True)
        assert refused_again.value.reason == 'permission_denied'
        assert len(_rows(authority, ref)) == 1
        post.assert_not_awaited()
        count = len(peer.requests)
        await n.deliver()
        n.rewind.assert_not_awaited()
        await authority._drain(ref)
        assert len(_rows(authority, ref)) == 1 and len(peer.requests) == count
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_concurrent_notice_retry_and_same_event_payload_or_text_conflict(tmp_path, monkeypatch):
    from gateway import wake
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    api = _api(runner, authority.db, monkeypatch)
    notice = _notice(ref.session_id)
    try:
        async def deliver(text='immutable', identity=None):
            await wake.deliver_wake(api, session_id=ref.session_id, text=text,
                                    notification_identity=notice if identity is None else identity)
        await asyncio.gather(*(deliver() for _ in range(8)))
        assert len(_rows(authority, ref)) == 1
        for changed_text, changed_identity in [('changed text', notice),
                ('immutable', {**notice, 'payload': {'kind': 'completed', 'payload': {'summary': 'MUTATED'}}})]:
            with pytest.raises(wake.WakeNotAccepted) as refused:
                await deliver(changed_text, changed_identity)
            assert refused.value.reason == 'admission_conflict'
        assert len(_rows(authority, ref)) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_partial_event_commit_rewind_is_individually_idempotent(tmp_path, monkeypatch):
    from gateway import kanban_watchers_notifier as notifier, wake
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    api = _api(runner, authority.db, monkeypatch)
    n = _notification(runner, api, ref, monkeypatch,
                      [_event(101, 'crashed', {}), _event(102, 'completed', {'summary': 'GROUP_RESULT'})])
    original = authority.admit_automation
    fail_second = True
    seen = []
    async def intermittently(adapter, event, identity, **kwargs):
        nonlocal fail_second
        seen.append(event.text)
        if len(seen) == 2 and fail_second:
            fail_second = False
            from hermes_state_runtime import RuntimeStoreError
            raise RuntimeStoreError('runtime_draining')
        return await original(adapter, event, identity, **kwargs)
    monkeypatch.setattr(authority, 'admit_automation', intermittently)
    try:
        await n.deliver()
        n.rewind.assert_awaited_once()
        n.advance.assert_not_awaited()
        assert len(_rows(authority, ref)) == 1
        await n.deliver()
        n.advance.assert_awaited_once()
        rows = _rows(authority, ref)
        assert len(rows) == 2 and rows[0]['request_id'] != rows[1]['request_id']
        assert rows[0]['payload']['local_automation_v1'].get('notification_category') == 'diagnostic'
        assert rows[1]['payload']['local_automation_v1'].get('notification_category', 'result') == 'result'
        assert 'GROUP_RESULT' in rows[1]['payload']['text']
        await n.deliver()
        assert n.advance.await_count == 2 and n.rewind.await_count == 1
        assert len(_rows(authority, ref)) == 2
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_absent_native_authority_and_foreign_profile_are_not_api_fallbacks(tmp_path, monkeypatch):
    from gateway import wake
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    api = _api(runner, authority.db, monkeypatch)
    post = AsyncMock(side_effect=AssertionError('fail closed, no self-POST'))
    monkeypatch.setattr(wake, '_self_post_chat_completion', post)
    try:
        with pytest.raises(wake.WakeNotAccepted) as mismatch:
            await wake.deliver_wake(api, session_id=ref.session_id, text='foreign', profile='foreign',
                                    notification_identity=_notice(ref.session_id))
        assert mismatch.value.reason == 'profile_mismatch'
        # A process with a store but no accepting authority must preserve the native request.
        api.gateway_runner = None
        with pytest.raises(wake.WakeNotAccepted) as absent:
            await wake.deliver_wake(api, session_id=ref.session_id, text='absent',
                                    notification_identity=_notice(ref.session_id))
        assert absent.value.reason == 'runtime_coordination_required'
        assert _rows(authority, ref) == []
        post.assert_not_awaited()
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_restart_queued_and_unknown_receipts_never_readmit(tmp_path, monkeypatch):
    from gateway import wake
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.config import Platform
    from hermes_state_runtime import claim_session_input
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda *args: None)
    api = _api(runner, authority.db, monkeypatch)
    notice = _notice(ref.session_id)
    try:
        await wake.deliver_wake(api, session_id=ref.session_id, text='RESTART_NOTICE', notification_identity=notice)
        runner.adapters.pop(Platform.LOCAL)
        cold = await initialize_session_authority(runner, profile_id='default', instance_id='restart', db=authority.db)
        await wake.deliver_wake(api, session_id=ref.session_id, text='RESTART_NOTICE', notification_identity=notice)
        assert len(_rows(cold, ref)) == 1
        claimed = claim_session_input(cold.db, epoch=cold.epoch, session_id=ref.session_id)
        assert claimed['status'] == 'started'
        from gateway import session_unattended_unknown
        real_settle = session_unattended_unknown.settle_recovered_unattended
        observed = []
        def observe_unknown(db, epoch):
            observed.extend(r['status'] for r in _rows(cold, ref))
            return real_settle(db, epoch)
        monkeypatch.setattr(session_unattended_unknown, 'settle_recovered_unattended', observe_unknown)
        runner.adapters.pop(Platform.LOCAL)
        restarted = await initialize_session_authority(runner, profile_id='default', instance_id='crash-restart', db=cold.db)
        assert observed == ['unknown']
        # Keep the shared unattended-automation contract: boot settles unknown interrupted,
        # never retries inference and never blocks a person behind this lost notification.
        assert (_rows(restarted, ref)[0]['status'], _rows(restarted, ref)[0]['outcome']) == ('terminal', 'interrupted')
        await wake.deliver_wake(api, session_id=ref.session_id, text='RESTART_NOTICE', notification_identity=notice)
        assert len(_rows(restarted, ref)) == 1 and _rows(restarted, ref)[0]['status'] == 'terminal'
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_compression_child_alias_keeps_notice_identity_and_root_owner(tmp_path, monkeypatch):
    from gateway import wake
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_runtime import settle_session_input, claim_session_input
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    api = _api(runner, authority.db, monkeypatch)
    notice = _notice(ref.session_id)
    try:
        await wake.deliver_wake(api, session_id=ref.session_id, text='BEFORE_COMPRESSION', notification_identity=notice)
        # Settle before compression; a busy root must not be compressed around its FIFO lease.
        row = claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)
        settle_session_input(authority.db, epoch=authority.epoch, admission_id=row['admission_id'],
                             generation=row['generation'], outcome='completed')
        target = authority.physical_target(ref)
        child = 'notice-compressed-child'
        assert authority.db.try_acquire_compression_lock(target, 'notice-fixture')
        authority.db.publish_compression_child(parent_session_id=target, child_session_id=child, source='cli',
            messages=[{'role': 'user', 'content': 'COMPRESSED_NATIVE_CONTEXT'}],
            compression_lock_holder='notice-fixture')
        await wake.deliver_wake(api, session_id=child, text='BEFORE_COMPRESSION',
                                notification_identity={**notice, 'chat_id': child})
        assert len(_rows(authority, ref)) == 1
        assert owned_receipt(authority, ref.session_id)[3].session_id == child
        await wake.deliver_wake(api, session_id=child, text='AFTER_COMPRESSION',
                                notification_identity={**notice, 'chat_id': child, 'event_id': 102})
        assert len(_rows(authority, ref)) == 2
        assert _rows(authority, ref)[1]['target_session_id'] == ref.session_id
    finally:
        authority.db.close()


def test_notice_identity_namespaces_and_content_conflicts_are_separate():
    from gateway.session_task_notifications import notice_key
    identity = _notice('owner')
    key, digest = notice_key(identity, 'notice', 'result', owner='owner', profile='default')
    # Modifying any content must retain the admission id, but change its fingerprint.
    for changed, text, category in [({**identity, 'payload': {'changed': True}}, 'notice', 'result'),
                                    (identity, 'different', 'result'), (identity, 'notice', 'diagnostic')]:
        next_key, next_digest = notice_key(changed, text, category, owner='owner', profile='default')
        assert next_key == key and next_digest != digest
    # Destination/producer namespaces cannot collide despite equal board-local event integers.
    for field, value in [('board', '/other/board.db'), ('task_id', 'other-task'), ('event_id', 102),
                         ('platform', 'local'), ('thread_id', 'other-thread'), ('notifier_profile', 'other')]:
        assert notice_key({**identity, field: value}, 'notice', 'result', owner='owner', profile='default')[0] != key
    assert notice_key(identity, 'notice', 'result', owner='other-owner', profile='default')[0] != key
    assert notice_key(identity, 'notice', 'result', owner='owner', profile='other-profile')[0] != key
    # A compression alias is the SAME subscriber's logical native conversation.
    assert notice_key({**identity, 'chat_id': 'compression-child'}, 'notice', 'result',
                      owner='owner', profile='default') == (key, digest)


def test_unbound_text_digest_ignores_mutable_rendering_but_not_event_content():
    from gateway.session_task_notifications import notice_key
    identity = _notice('owner')
    edited = {**identity, 'payload': {'kind': 'completed', 'payload': {'summary': 'edited'}}}
    kwargs = dict(owner='owner', profile='default', bind_text=False)
    key, digest = notice_key(identity, 'Title: before rename', 'result', **kwargs)
    # The notifier renders the task's CURRENT title/assignee each tick; that is not event content.
    assert notice_key(identity, 'Title: after rename', 'result', **kwargs) == (key, digest)
    next_key, next_digest = notice_key(edited, 'Title: before rename', 'result', **kwargs)
    assert next_key == key and next_digest != digest
    assert notice_key(identity, 'Title: before rename', 'diagnostic', **kwargs)[1] != digest
    assert notice_key(identity, 'Title: before rename', 'result', owner='owner', profile='default')[1] != digest


@pytest.mark.asyncio
async def test_same_event_retry_after_title_change_is_one_admission_and_changed_event_conflicts(tmp_path, monkeypatch):
    from gateway import wake
    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    api = _api(runner, authority.db, monkeypatch)
    notice = {**_notice(ref.session_id), 'rendered_from_task': True}
    text = '[kanban] Task t_notice completed. Title: Before rename Assignee: @worker'
    try:
        await wake.deliver_wake(api, session_id=ref.session_id, notification_identity=notice, text=text)
        # The task was renamed and reassigned before the cursor-rewind retry: same event, new rendering.
        await wake.deliver_wake(api, session_id=ref.session_id, notification_identity=notice,
                                text='[kanban] Task t_notice completed. Title: After rename Assignee: @other')
        rows = _rows(authority, ref)
        assert len(rows) == 1 and 'Before rename' in rows[0]['payload']['text']
        digest = rows[0]['payload']['local_automation_v1']['task_notice_digest']
        edited = _notice(ref.session_id, payload={'kind': 'completed', 'payload': {'summary': 'edited'}})
        with pytest.raises(wake.WakeNotAccepted) as changed:
            await wake.deliver_wake(api, session_id=ref.session_id, notification_identity=edited, text=text)
        assert changed.value.reason == 'admission_conflict'
        with pytest.raises(wake.WakeNotAccepted) as recategorized:
            await wake.deliver_wake(api, session_id=ref.session_id, notification_identity=notice,
                                    notification_category='diagnostic', text=text)
        assert recategorized.value.reason == 'admission_conflict'
        rows = _rows(authority, ref)
        assert len(rows) == 1 and rows[0]['payload']['local_automation_v1']['task_notice_digest'] == digest
    finally:
        authority.db.close()
