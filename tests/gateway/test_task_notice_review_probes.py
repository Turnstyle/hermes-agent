"""Read-only candidate probes. Run through the checkout's documented offline runner."""
import json
import pytest
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_notification_category import _native_owner


def _notice():
    return dict(board='/fixture/board.db', task_id='task', event_id=7,
                platform='discord', chat_id='42', thread_id='', notifier_profile='default',
                payload={'kind': 'completed', 'payload': {'summary': 'done'}})


@pytest.mark.asyncio
@pytest.mark.parametrize('stable_notice', [False, True])
async def test_queued_route_wake_survives_rotation(tmp_path, monkeypatch, stable_notice):
    from gateway.wake import deliver_wake
    from gateway.session_automation import move_route_wakes
    from gateway.session_authority import LiveSession
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'human-1')
    adapter.gateway_runner = runner
    try:
        route, old = metadata['gateway_session_key'], metadata['gateway_session_id']
        await deliver_wake(adapter, source=source, text='queued task result',
                           notification_identity=_notice() if stable_notice else None)
        old_rows = list_session_admissions(authority.db, session_id=old, pending_only=False)
        wake = old_rows[-1]
        assert wake['status'] == 'queued'
        # The production route rotation calls move_route_wakes after updating the entry.
        fresh = runner.session_store.reset_session(route)
        authority.sessions.setdefault(fresh.session_id, LiveSession(source, route))
        await move_route_wakes(runner, route, old)
        moved = list_session_admissions(authority.db, session_id=fresh.session_id, pending_only=False)
        assert any(row['payload']['text'] == 'queued task result' for row in moved), (stable_notice, wake['request_id'], moved)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_malformed_push_notice_is_retryable_refusal(monkeypatch):
    from types import SimpleNamespace
    from gateway.session import SessionSource
    from gateway.wake import deliver_wake, WakeNotAccepted
    adapter = SimpleNamespace(supports_async_delivery=True, gateway_runner=object())
    source = SessionSource(platform=Platform.DISCORD, chat_id='42')
    with pytest.raises(WakeNotAccepted) as refused:
        await deliver_wake(adapter, source=source, text='notice', notification_identity=[])
    assert refused.value.reason == 'invalid_params'
