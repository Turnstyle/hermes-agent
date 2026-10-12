"""Kanban notifier behavior on stateless (api_server) subscriptions.

Covers the wrong-session-wake / silent-loss fixes:
* a SendResult(success=False) return (the API server's send() stub) rewinds
  the cursor instead of advancing past a never-delivered event;
* api_server subscriptions wake their ``chat_id`` delivery destinations via
  the /v1/chat/completions self-post, never task ``session_id`` provenance or
  handle_message (which would derive a different session key).
"""

import asyncio
import logging

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class SoftFailAdapter:
    """Push-capable adapter whose send() returns SendResult(success=False)
    WITHOUT raising — previously treated as delivered (event lost)."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        return SendResult(success=False, error="soft failure")


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self):
        self._host = "127.0.0.1"
        self._port = 8642
        self._api_key = "k"
        self._model_name = "hermes"
        self.handle_message_calls = []
        self.send_calls = 0

    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1
        return SendResult(
            success=False,
            error="API server uses HTTP request/response, not send()",
        )

    async def handle_message(self, event):
        self.handle_message_calls.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _create_completed_subscription(platform, chat_id, session_id=None):
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="notify once", assignee="worker", session_id=session_id,
        )
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        kb.complete_task(conn, tid, summary="done once")
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid, platform, chat_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform=platform,
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_apiserver_sub_wakes_subscription_destination_via_self_post(tmp_path, monkeypatch):
    """An api_server subscription wakes its chat_id destination, not the
    task's worker-session provenance or a build_session_key()-derived session."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver.db"))
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server", "origin-session", session_id="worker-session",
    )

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    adapter = ApiServerLikeAdapter()
    runner = _make_runner({Platform.API_SERVER: adapter})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.handle_message_calls == [], (
        "api_server wake must not go through handle_message (wrong-session bug)"
    )
    assert len(posts) == 1
    assert posts[0]["session_id"] == "origin-session"
    assert all(post["session_id"] != "worker-session" for post in posts)
    wake_text = posts[0]["text"]
    assert tid in wake_text
    # Graph-safe wake turn (#70752): the synthetic turn must carry the
    # worker's completion handoff and the don't-recreate guidance so a
    # woken orchestrator doesn't re-decompose existing work.
    assert "done once" in wake_text, "creator wake must carry the worker handoff"
    assert "not a request to decompose" in wake_text.lower()
    assert "do not recreate" in wake_text.lower()
    # The wake self-post IS the delivery on this path (no separate text-ping
    # fallback is attempted for stateless api_server subs) — cursor advances
    # once the wake succeeds.
    assert _unseen_terminal_events(tid, "api_server", "origin-session") == []


def test_apiserver_subscriptions_have_independent_wake_destinations(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver-multi.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="notify both",
            assignee="worker",
            session_id="worker-session",
        )
        for chat_id in ("origin-a", "origin-b"):
            kbn.add_notify_sub(
                conn,
                task_id=tid,
                platform="api_server",
                chat_id=chat_id,
            )
        kb.complete_task(conn, tid, summary="done once")
    finally:
        conn.close()

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert sorted(post["session_id"] for post in posts) == ["origin-a", "origin-b"]
    assert all(post["session_id"] != "worker-session" for post in posts)
    assert _unseen_terminal_events(tid, "api_server", "origin-a") == []
    assert _unseen_terminal_events(tid, "api_server", "origin-b") == []


def test_apiserver_wake_failure_rewinds_then_retries_destination(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver-retry.db"))
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server", "origin-session", session_id="worker-session",
    )
    attempted_sessions = []

    async def fail_once_then_succeed(adapter, *, text, session_id):
        attempted_sessions.append(session_id)
        if len(attempted_sessions) == 1:
            raise RuntimeError("simulated wake failure")

    import gateway.wake as wake_mod

    monkeypatch.setattr(
        wake_mod,
        "_self_post_chat_completion",
        fail_once_then_succeed,
    )
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert _unseen_terminal_events(tid, "api_server", "origin-session")

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert attempted_sessions == ["origin-session", "origin-session"]
    assert "worker-session" not in attempted_sessions
    assert _unseen_terminal_events(tid, "api_server", "origin-session") == []



@pytest.mark.parametrize("busy", [True, False])
def test_busy_wake_is_deferred_but_real_failures_are_bounded(tmp_path, monkeypatch, caplog, busy):
    from gateway import kanban_watchers_notifier as notifier
    from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
    from tools.bot_relay import TurnBusyError

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "busy-wake.db"))
    monkeypatch.setattr(notifier, "WAKE_MAX_FAILURES", 3, raising=False)
    getattr(notifier, "_BUSY_WAKE_LOGGED", set()).clear()
    kb.init_db()
    tid = _create_completed_subscription("api_server", "origin-session")
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})
    caplog.set_level(logging.INFO, logger=notifier.logger.name)

    async def failing_post(adapter, *, text, session_id):
        if busy:
            raise TurnBusyError("owner", 1)
        raise RuntimeError("real transport failure")

    monkeypatch.setattr("gateway.wake._self_post_chat_completion", failing_post)

    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb,
            notifier_profile=None, gc_due=False, gc_retention_days=30)
        for delivery in deliveries:
            notification = _KanbanNotification(runner, delivery, platform_cls=Platform,
                sub_fail_counts=runner._kanban_sub_fail_counts)
            await notification.deliver()
        return deliveries

    try:
        for attempt in range(13 if busy else 3):
            deliveries = asyncio.run(tick())
            assert len(deliveries) == 1
            if busy:
                assert runner._kanban_sub_fail_counts == {}
            elif attempt < 2:
                assert list(runner._kanban_sub_fail_counts.values()) == [attempt + 1]
            if busy or attempt < 2:
                assert _unseen_terminal_events(tid, "api_server", "origin-session")
        with kbc.connect() as conn:
            subs = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchall()
        if busy:
            assert len(subs) == 1
            assert sum("deferred: the bot is in a turn" in r.message for r in caplog.records) == 1
            notification = _KanbanNotification(runner, deliveries[0], platform_cls=Platform,
                sub_fail_counts=runner._kanban_sub_fail_counts)
            notification.clear_failures()
            asyncio.run(tick())
            assert sum("deferred: the bot is in a turn" in r.message for r in caplog.records) == 2
        else:
            assert subs == []
            assert runner._kanban_sub_fail_counts == {}
            assert f"hermes kanban show {tid}" in caplog.text
    finally:
        getattr(notifier, "_BUSY_WAKE_LOGGED", set()).clear()


@pytest.mark.parametrize("status, body, exception", [
    (409, '{"code":"target_busy"}', "busy"),
    (409, '{"code":"other_conflict"}', "failure"),
    (403, '{"code":"target_busy"}', "failure"),
])
def test_http_self_post_defers_only_target_busy_409(monkeypatch, status, body, exception):
    from gateway.wake import WakeNotAccepted, _self_post_chat_completion
    from unittest.mock import AsyncMock, MagicMock
    import aiohttp

    response = MagicMock(status=status)
    response.text = AsyncMock(return_value=body)
    post_context = MagicMock()
    post_context.__aenter__ = AsyncMock(return_value=response)
    post_context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post.return_value = post_context
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session_context)
    expected = WakeNotAccepted if exception == "busy" else RuntimeError
    with pytest.raises(expected, match="target_busy" if exception == "busy" else f"HTTP {status}"):
        asyncio.run(_self_post_chat_completion(ApiServerLikeAdapter(), text="wake", session_id="origin"))
    assert session.post.call_count == 1


def test_real_wake_failures_warn_at_checkpoints_and_passive_limit_stays_short(tmp_path, monkeypatch, caplog):
    import gateway.kanban_watchers_notifier as notifier
    from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-warning.db"))
    kb.init_db()
    tid = _create_completed_subscription("api_server", "origin-session")
    runner = _make_runner({Platform.API_SERVER: ApiServerLikeAdapter()})
    caplog.set_level(logging.DEBUG, logger=notifier.logger.name)

    async def fail(adapter, *, text, session_id):
        raise RuntimeError("transport failure")
    monkeypatch.setattr("gateway.wake._self_post_chat_completion", fail)

    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb,
            notifier_profile=None, gc_due=False, gc_retention_days=30)
        for delivery in deliveries:
            await _KanbanNotification(runner, delivery, platform_cls=Platform,
                sub_fail_counts=runner._kanban_sub_fail_counts).deliver()

    key = (tid, "api_server", "origin-session", "")
    for prior, level in [(3, logging.DEBUG), (59, logging.WARNING), (719, logging.WARNING)]:
        runner._kanban_sub_fail_counts[key] = prior
        caplog.clear()
        asyncio.run(tick())
        records = [r for r in caplog.records if "wake self-post failed" in r.message]
        assert len(records) == 1
        assert records[0].levelno == level
    assert "owner not woken" in caplog.text
    assert f"hermes kanban show {tid}" in caplog.text

    # The passive delivery boundary retains the 12-failure policy.
    notification = _KanbanNotification(runner, {
        "sub": {"task_id": tid, "platform": "api_server", "chat_id": "origin-session"},
        "task": None, "events": [], "cursor": 0,
    }, platform_cls=Platform, sub_fail_counts={})
    from unittest.mock import AsyncMock
    notification.unsub = AsyncMock()
    notification.rewind = AsyncMock()
    notification.sub_fail_counts[notification.sub_key] = 11
    asyncio.run(notification.delivery_failed("%s %d/%d %s", (tid,), "%s %s %d",
        RuntimeError("ping failed"), False))
    notification.unsub.assert_awaited_once()
    notification.rewind.assert_not_awaited()
