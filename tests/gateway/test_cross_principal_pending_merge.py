"""Pending follow-ups on a shared route must retain their sender boundary."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


def _source(
    principal: str,
    *,
    legacy_user_id: str | None = None,
    platform: Platform = Platform.TELEGRAM,
) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="shared-room",
        chat_type="group",
        user_id=legacy_user_id or principal,
        user_id_alt=principal,
    )


def _photo(principal: str, delivery_id: str) -> MessageEvent:
    return MessageEvent(
        text=f"caption {delivery_id}",
        message_type=MessageType.PHOTO,
        source=_source(principal, legacy_user_id=f"legacy-{delivery_id}"),
        message_id=delivery_id,
        media_urls=[f"/tmp/{delivery_id}.png"],
        media_types=["image/png"],
    )


@pytest.mark.asyncio
async def test_priority_media_queue_fifos_another_principal_on_the_same_shared_route():
    """Same-principal albums coalesce; another participant gets its own FIFO turn."""
    from gateway.run import GatewayRunner

    first = _photo("principal-a", "a-1")
    same_principal = _photo("principal-a", "a-2")
    other_principal = _photo("principal-b", "b-1")
    session_key = build_session_key(first.source, group_sessions_per_user=False)
    assert session_key == build_session_key(
        other_principal.source, group_sessions_per_user=False
    )

    adapter = SimpleNamespace(_pending_messages={session_key: first})
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._sessions = {}
    runner._adapter_for_source = lambda _source: adapter

    assert await runner._hm_busy_slash_or_photo(
        same_principal, same_principal.source, session_key
    ) == (True, None)
    assert await runner._hm_busy_slash_or_photo(
        other_principal, other_principal.source, session_key
    ) == (True, None)

    head = adapter._pending_messages[session_key]
    assert head is first
    assert head.media_urls == ["/tmp/a-1.png", "/tmp/a-2.png"]
    assert head._accepted_delivery_ids == ("a-1", "a-2")
    assert runner._session_state(session_key).conversation.queued_events == [other_principal]
    assert other_principal._accepted_delivery_ids == ()
    assert same_principal._gateway_accepted is True
    assert other_principal._gateway_accepted is True


def test_pending_text_merge_refuses_another_principal_without_mutating_the_head():
    """The non-debounce text merge uses the same alt-id-first principal contract."""
    first = MessageEvent(
        text="first",
        source=_source("principal-a", legacy_user_id="old-a"),
        message_id="a-1",
    )
    other_principal = MessageEvent(
        text="not-a",
        source=_source("principal-b", legacy_user_id="old-a"),
        message_id="b-1",
    )
    same_principal = MessageEvent(
        text="second",
        source=_source("principal-a", legacy_user_id="new-a"),
        message_id="a-2",
    )
    session_key = build_session_key(first.source, group_sessions_per_user=False)
    pending = {session_key: first}

    assert merge_pending_message_event(
        pending, session_key, other_principal, merge_text=True
    ) is False
    assert pending[session_key] is first
    assert first.text == "first"
    assert first._accepted_delivery_ids == ()
    assert other_principal._gateway_accepted is False

    assert merge_pending_message_event(
        pending, session_key, same_principal, merge_text=True
    ) is True
    assert first.text == "first\nsecond"
    assert first._accepted_delivery_ids == ("a-1", "a-2")


@pytest.mark.parametrize("adapter_kind", ["discord", "simplex", "weixin"])
@pytest.mark.asyncio
async def test_text_batching_partitions_a_shared_route_before_runner_ingress(adapter_kind):
    """Production enqueue paths keep independent sender batches and timers."""
    if adapter_kind == "discord":
        from plugins.platforms.discord.adapter import DiscordAdapter

        adapter_class = DiscordAdapter
        platform = Platform.DISCORD
    elif adapter_kind == "simplex":
        from tests.gateway._plugin_adapter_loader import load_plugin_adapter

        adapter_class = load_plugin_adapter("simplex").SimplexAdapter
        platform = Platform("simplex")
    else:
        from gateway.platforms.weixin import WeixinAdapter

        adapter_class = WeixinAdapter
        platform = Platform.WEIXIN

    adapter = object.__new__(adapter_class)
    adapter.config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"group_sessions_per_user": False},
    )
    adapter.platform = platform
    adapter._owner_profile = None
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    release_flush = asyncio.Event()

    async def _hold_flush(_key: str) -> None:
        await release_flush.wait()

    adapter._flush_text_batch = _hold_flush

    first = MessageEvent(
        text="first-a",
        source=_source("principal-a", legacy_user_id="old-a", platform=platform),
        message_id="a-1",
    )
    other_principal = MessageEvent(
        text="only-b",
        source=_source("principal-b", legacy_user_id="old-a", platform=platform),
        message_id="b-1",
    )
    same_principal = MessageEvent(
        text="second-a",
        source=_source("principal-a", legacy_user_id="new-a", platform=platform),
        message_id="a-2",
    )
    route_key = build_session_key(first.source, group_sessions_per_user=False)
    assert route_key == build_session_key(
        other_principal.source, group_sessions_per_user=False
    )

    first_key = adapter._text_batch_key(first)
    other_key = adapter._text_batch_key(other_principal)
    assert first_key == adapter._text_batch_key(same_principal)
    assert first_key != other_key

    tasks = []
    try:
        adapter._enqueue_text_event(first)
        tasks.append(adapter._pending_text_batch_tasks[first_key])
        adapter._enqueue_text_event(other_principal)
        tasks.append(adapter._pending_text_batch_tasks[other_key])
        adapter._enqueue_text_event(same_principal)
        tasks.append(adapter._pending_text_batch_tasks[first_key])
        await asyncio.sleep(0)

        assert set(adapter._pending_text_batches) == {first_key, other_key}
        assert adapter._pending_text_batches[first_key] is first
        assert first.text == "first-a\nsecond-a"
        assert first._accepted_delivery_ids == ("a-1", "a-2")
        assert adapter._pending_text_batches[other_key] is other_principal
        assert other_principal.text == "only-b"
        assert other_principal._accepted_delivery_ids == ()
        assert set(adapter._pending_text_batch_tasks) == {first_key, other_key}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
