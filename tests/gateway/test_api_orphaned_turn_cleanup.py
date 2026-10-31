"""Ended API DM turns must not become immortal coordinated-restart blockers.

Exercise the real task cleanup, running-slot generation guard and active-work
projection. No provider, live gateway, or age-based cancellation is involved.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


RUN_KEY = "agent:shld-cndr:api_server:dm:run_8539cf11179d4e2896c5f668b5a838a6"
OTHER_KEY = "agent:other-seat:api_server:dm:run_live_control"
SAME_SEAT_KEY = "agent:shld-cndr:api_server:dm:run_live_control"


def _runner(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    # Keep runtime writes in memory; leave real active-work and slot logic intact.
    runner._persist_active_agents = Mock()
    monkeypatch.setattr("cron.scheduler.get_running_job_ids", lambda: [])
    monkeypatch.setattr("cron.scheduler.get_running_job_details", lambda: [])
    monkeypatch.setattr("cron.scheduler.get_wedged_job_ids", lambda: set())
    return runner


def _claim(runner, key):
    generation = runner._begin_session_run_generation(key)
    agent = SimpleNamespace(model="test-model")
    state = runner._session_state(key)
    state.turn.agent = agent
    # Deliberately very old: genuinely live controls must never be expired by age.
    state.turn.started_ts = 1.0
    state.turn.lease = Mock()
    return generation, agent, state.turn.lease


def _names(runner):
    return {unit["session"] for unit in runner._describe_active_work() if unit["kind"] == "chat"}


async def _wait_forever():
    await asyncio.Event().wait()


async def _cleanup(runner, ctx, tracking, stream=None):
    await runner._run_agent_cleanup_turn_tasks(
        ctx, progress_task=None, log_task=None, interrupt_monitor=None,
        _notify_task=None, tracking_task=tracking, stream_task=stream,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stream", "no_consumer", "tts_abort", "cancelled"])
async def test_ended_api_turn_releases_own_slot_despite_teardown_failure(monkeypatch, failure):
    runner = _runner(monkeypatch)
    generation, _, lease = _claim(runner, RUN_KEY)
    _, other_agent, other_lease = _claim(runner, OTHER_KEY)
    _, same_agent, same_lease = _claim(runner, SAME_SEAT_KEY)
    ctx = TurnContext(session_key=RUN_KEY, run_generation=generation)
    tracking = asyncio.create_task(_wait_forever())
    stream = asyncio.get_running_loop().create_future()
    error = asyncio.CancelledError() if failure == "cancelled" else RuntimeError("teardown failed")
    if failure == "tts_abort":
        stream.set_result(None)
        ctx.streaming_tts_consumer_holder[0] = SimpleNamespace(done=False, abort=Mock(side_effect=error))
    else:
        stream.set_exception(error)
    if failure != "no_consumer":
        ctx.stream_consumer_holder[0] = object()
    if failure == "cancelled":
        # Cancellation of the cleanup coroutine itself, not normal consumer cancellation.
        runner._await_stream_task = AsyncMock(side_effect=error)
        stream.exception()  # consume the deliberately injected future error
    try:
        with pytest.raises(type(error)):
            await _cleanup(runner, ctx, tracking, stream)
        assert _names(runner) == {OTHER_KEY, SAME_SEAT_KEY}
        assert runner._active_work_count() == 2
        lease.release.assert_called_once_with()
        assert tracking.done() and tracking.cancelled()
        assert runner._session_state(OTHER_KEY).turn.agent is other_agent
        assert runner._session_state(SAME_SEAT_KEY).turn.agent is same_agent
        other_lease.release.assert_not_called()
        same_lease.release.assert_not_called()
    finally:
        tracking.cancel()
        await asyncio.gather(tracking, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_stream_cannot_resurrect_api_dm_after_outer_turn_finalizer(monkeypatch):
    """The outer handler already releases the slot. A leaked tracker can put it BACK.

    Model completion beats the tracker's next 50ms poll; a failed stream then
    bypasses its cancellation. This runs the actual inbound finalizer too.
    """
    runner = _runner(monkeypatch)
    _claim(runner, OTHER_KEY)
    source = SessionSource(platform=Platform.API_SERVER, chat_id=RUN_KEY.rsplit(":", 1)[-1],
                           user_id="api", chat_type="dm")
    event = MessageEvent(text="bounded regression", source=source)
    runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
    runner._hm_estop_gate = Mock(return_value=None)
    runner._session_key_for_source = Mock(return_value=RUN_KEY)
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_authority_admission = AsyncMock(return_value=(False, None))
    runner._hm_evict_idle_stale_agent = Mock()
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._is_telegram_topic_root_lobby = Mock(return_value=False)
    runner._claim_active_session_slot = Mock(return_value=(Mock(), None))
    runner._hm_rescue_orphaned_fifo = Mock(return_value=(event, source, False))
    runner._hm_post_admission_consume = AsyncMock(return_value=(False, None))
    runner._clear_durable_active_turn = AsyncMock()
    runner._restore_pending_one_turn_model_override = Mock()
    tracker = None

    async def ended_turn(event, source, key, generation):
        nonlocal tracker
        ctx = TurnContext(session_key=key, run_generation=generation)
        tracker = asyncio.create_task(runner._run_agent_track_agent(ctx))
        await asyncio.sleep(0)  # tracker enters its real 50ms agent-holder poll
        ctx.agent_holder[0] = SimpleNamespace(model="finished-model")
        stream = asyncio.get_running_loop().create_future()
        stream.set_exception(RuntimeError("stream failed after model completion"))
        # With no stream consumer, awaiting this already-failed future doesn't yield.
        await _cleanup(runner, ctx, tracker, stream)

    runner._handle_message_with_agent = ended_turn
    try:
        with pytest.raises(RuntimeError, match="stream failed"):
            await runner._handle_message(event)
        # This is the real outer finalizer, not an absence of a running slot in the fixture.
        assert RUN_KEY not in _names(runner)
        # Bound the regression wait; on old code the leaked tracker now resurrects the slot.
        await asyncio.sleep(0.075)
        assert _names(runner) == {OTHER_KEY}
        assert runner._active_work_count() == 1
        assert tracker is not None and tracker.done() and tracker.cancelled()
    finally:
        if tracker is not None:
            tracker.cancel()
            await asyncio.gather(tracker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_stale_cleanup_never_releases_newer_generation(monkeypatch, failure):
    runner = _runner(monkeypatch)
    old_generation, _, old_lease = _claim(runner, RUN_KEY)
    ctx = TurnContext(session_key=RUN_KEY, run_generation=old_generation)
    new_generation, new_agent, new_lease = _claim(runner, RUN_KEY)
    tracking = asyncio.create_task(_wait_forever())
    stream = asyncio.get_running_loop().create_future()
    if failure:
        stream.set_exception(RuntimeError("old stream failed"))
    else:
        stream.set_result(None)
    try:
        if failure:
            with pytest.raises(RuntimeError, match="old stream failed"):
                await _cleanup(runner, ctx, tracking, stream)
        else:
            await _cleanup(runner, ctx, tracking, stream)
        assert _names(runner) == {RUN_KEY}
        assert runner._session_state(RUN_KEY).turn.agent is new_agent
        assert runner._current_session_run_generation(RUN_KEY) == new_generation
        old_lease.release.assert_not_called()
        new_lease.release.assert_not_called()
        assert tracking.done() and tracking.cancelled()
    finally:
        tracking.cancel()
        await asyncio.gather(tracking, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_normal_cleanup_releases_ended_turn_for_any_model_outcome(monkeypatch, outcome):
    runner = _runner(monkeypatch)
    generation, _, lease = _claim(runner, RUN_KEY)
    ctx = TurnContext(session_key=RUN_KEY, run_generation=generation,
                      result_holder=[{"status": outcome}])
    tracking = asyncio.create_task(_wait_forever())
    await _cleanup(runner, ctx, tracking)
    assert _names(runner) == set()
    assert runner._active_work_count() == 0
    lease.release.assert_called_once_with()
    assert tracking.done() and tracking.cancelled()
