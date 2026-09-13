"""Integration regressions for the preflight-recovery admission state machine."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.pending_turn_draft import (
    PENDING_TURN_DRAFT_PREFIX,
    PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
    PENDING_TURN_REDELIVERY_NOTICE,
    link_pending_turn_draft,
    load_pending_turn_draft,
    pending_turn_principal_fingerprint,
    stage_pending_turn_draft,
)
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, SessionStore


def _source(user_id: str = "U1") -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK,
        chat_id="C1",
        chat_type="channel",
        user_id=user_id,
        scope_id="W1",
    )


def _preflight_result(text: str = "must survive") -> dict:
    return {
        "failed": True,
        "compression_exhausted": True,
        "turn_exit_reason": "context_compression_timeout",
        "api_calls": 0,
        "unprocessed_input": {
            "version": 1,
            "kind": "accepted_user_input",
            "text": text,
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "loss_reasons": [],
            "safe_to_replay": True,
            "provider_call_sent": False,
            "executable": False,
        },
    }


def _store(tmp_path, monkeypatch, *, shared: bool = False) -> SessionStore:
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    if shared:
        config.group_sessions_per_user = False
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _gateway(store: SessionStore):
    from gateway.run import GatewayRunner

    gateway = GatewayRunner.__new__(GatewayRunner)
    gateway.config = store.config
    gateway.session_store = store
    gateway._evict_cached_agent = MagicMock()
    gateway._clear_conversation_scope = MagicMock()
    gateway._sync_telegram_topic_binding = MagicMock()
    return gateway


def _install_recovery(store: SessionStore, source: SessionSource, *, message_id="old-id"):
    old = store.get_or_create_session(source)
    key = old.session_key
    principal = pending_turn_principal_fingerprint(source)
    draft = stage_pending_turn_draft(
        store._db_for_key(key),
        text="accepted but not run",
        predecessor_session_id=old.session_id,
        session_key=key,
        principal_fingerprint=principal,
        platform_message_id=message_id,
    )
    new = store.reset_session(key)
    link_pending_turn_draft(
        store._db_for_key(key),
        draft.draft_id,
        predecessor_session_id=old.session_id,
        successor_session_id=new.session_id,
    )
    return old, new, draft


@pytest.mark.asyncio
async def test_preflight_timeout_requeues_followup_before_recursive_provider_entry():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = _source()
    queued = MessageEvent(text="later work", source=source, message_id="later-id")
    adapter = SimpleNamespace(_pending_messages={})
    result = _preflight_result()
    turn_ctx = SimpleNamespace(
        source=source,
        session_id="old-session",
        session_key="route-key",
        run_generation=1,
        _interrupt_depth=0,
        history=[],
        _status_thread_metadata={},
        result_holder=[result],
    )
    runner._run_agent = AsyncMock(side_effect=AssertionError("provider must not run"))

    returned = await runner._run_agent_queued_followup(
        turn_ctx,
        adapter,
        queued.text,
        queued,
        result,
        result,
        None,
    )

    assert returned is result
    assert adapter._pending_messages["route-key"] is queued
    assert queued._predates_pending_turn_recovery is True
    runner._run_agent.assert_not_awaited()


def test_requeue_restores_fifo_head_without_losing_promoted_item():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = _source()
    first = MessageEvent(text="first", source=source, message_id="m1")
    second = MessageEvent(text="second", source=source, message_id="m2")
    third = MessageEvent(text="third", source=source, message_id="m3")
    overflow = [third]
    runner._overflow_queue = MagicMock(return_value=overflow)
    adapter = SimpleNamespace(_pending_messages={"route-key": second})

    assert runner._hmwa_requeue_pending_followup(
        adapter=adapter,
        session_key="route-key",
        source=source,
        pending_event=first,
        pending=first.text,
    )
    assert adapter._pending_messages["route-key"] is first
    assert overflow == [second, third]


@pytest.mark.asyncio
async def test_active_tombstone_requeues_normal_queued_turn_for_outer_authorization():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = _source()
    queued = MessageEvent(text="later work", source=source, message_id="later-id")
    adapter = SimpleNamespace(_pending_messages={})
    result = {"final_response": "first done", "api_calls": 1, "messages": []}
    runner._session_key_for_source = MagicMock(return_value="route-key")
    runner._hmwa_pending_turn_redelivery_notice = AsyncMock(
        return_value=PENDING_TURN_REDELIVERY_NOTICE
    )
    runner._run_agent = AsyncMock(side_effect=AssertionError("provider must not run"))
    turn_ctx = SimpleNamespace(
        source=source,
        session_id="new-session",
        session_key="route-key",
        run_generation=1,
        _interrupt_depth=0,
        history=[],
        _status_thread_metadata={},
        result_holder=[result],
    )

    returned = await runner._run_agent_queued_followup(
        turn_ctx,
        adapter,
        queued.text,
        queued,
        result,
        result,
        None,
    )

    assert returned is result
    assert adapter._pending_messages["route-key"] is queued
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_idless_event_cannot_bypass_active_owner_tombstone(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _, new, _draft = _install_recovery(store, source)
    gateway = _gateway(store)

    notice = await gateway._hmwa_pending_turn_redelivery_notice(
        MessageEvent(text="accepted but not run", source=source, message_id=None),
        source,
        new,
        new.session_key,
    )

    assert notice == PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_pre_reset_queued_followup_does_not_acknowledge_lost_turn(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _, new, draft = _install_recovery(store, source)
    gateway = _gateway(store)
    queued = MessageEvent(text="already queued B", source=source, message_id="queued-b")
    queued._predates_pending_turn_recovery = True

    admitted = await gateway._hmwa_pending_turn_redelivery_notice(
        queued, source, new, new.session_key
    )

    assert admitted is None
    retained = load_pending_turn_draft(
        store._db_for_key(new.session_key), draft.draft_id
    )
    assert retained.manual_resend_acknowledged_at is None
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_manual_resend_acknowledges_retry_but_retains_old_id_tombstone(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _, new, draft = _install_recovery(store, source)
    gateway = _gateway(store)

    resend = MessageEvent(text="manual resend", source=source, message_id="new-id")
    admitted = await gateway._hmwa_pending_turn_redelivery_notice(
        resend,
        source,
        new,
        new.session_key,
    )
    assert load_pending_turn_draft(
        store._db_for_key(new.session_key), draft.draft_id
    ).manual_resend_acknowledged_at is None
    assert await gateway._hmwa_commit_pending_turn_resend_acknowledgement(
        resend, source, new.session_key, {"api_calls": 1}
    ) is None
    current = store.lookup_by_session_key(new.session_key)
    retry_reply = await gateway._handle_pending_turn_draft_retry(
        MessageEvent(
            text="/retry",
            source=source,
            message_type=MessageType.COMMAND,
            message_id="retry-id",
        ),
        current,
    )
    replay_notice = await gateway._hmwa_pending_turn_redelivery_notice(
        MessageEvent(text="old redelivery", source=source, message_id="old-id"),
        source,
        current,
        new.session_key,
    )

    assert admitted is None
    acknowledged = load_pending_turn_draft(
        store._db_for_key(new.session_key), draft.draft_id
    )
    assert acknowledged.manual_resend_acknowledged_at is not None
    assert retry_reply is None
    assert replay_notice == PENDING_TURN_REDELIVERY_NOTICE
    assert load_pending_turn_draft(store._db_for_key(new.session_key), draft.draft_id)
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_reset_failure_discards_only_new_generation_and_preserves_old_route(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _, current, first = _install_recovery(store, source)
    key = current.session_key
    real_reset = store.reset_session
    store.reset_session = MagicMock(side_effect=OSError("reset refused"))
    gateway = _gateway(store)

    response, returned = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result("second accepted input"),
        "compression timed out",
        current,
        key,
        source,
        event=MessageEvent(text="second", source=source, message_id="second-id"),
    )

    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert returned.session_id == current.session_id
    assert len(rows) == 1
    assert load_pending_turn_draft(store._db_for_key(key), first.draft_id).text == (
        "accepted but not run"
    )
    assert "kept the existing session" in response
    store.reset_session = real_reset
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_gateway_attachment_shape_cannot_be_recorded_as_exact_plain_text(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    old = store.get_or_create_session(source)
    gateway = _gateway(store)
    event = MessageEvent(
        text="extracted document text",
        source=source,
        message_type=MessageType.DOCUMENT,
        message_id="doc-id",
        media_urls=["/tmp/input.pdf"],
        media_types=["application/pdf"],
    )

    _response, new = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result("extracted document text"),
        "compression timed out",
        old,
        old.session_key,
        source,
        event=event,
    )

    rows = store._db_for_key(old.session_key).list_meta_prefix(
        PENDING_TURN_DRAFT_PREFIX
    )
    assert len(rows) == 1
    draft = load_pending_turn_draft(
        store._db_for_key(old.session_key),
        rows[0][0].removeprefix(PENDING_TURN_DRAFT_PREFIX),
    )
    assert draft.input_kind == "structured"
    assert draft.exact is False
    assert set(draft.loss_reasons) >= {
        "attachments_not_retained",
        "structured_components_not_retained",
    }
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_multiple_recovery_generations_survive_later_pointerless_resets(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _old, first_successor, first = _install_recovery(
        store, source, message_id="first-old-id"
    )
    key = first_successor.session_key
    gateway = _gateway(store)

    _response, second_successor = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result("second accepted input"),
        "compression timed out",
        first_successor,
        key,
        source,
        event=MessageEvent(
            text="second accepted input", source=source, message_id="second-old-id"
        ),
    )
    current = store.reset_session(key)

    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 2
    assert load_pending_turn_draft(store._db_for_key(key), first.draft_id)
    assert second_successor.session_id != first_successor.session_id
    assert current.session_id != second_successor.session_id
    for old_id in ("first-old-id", "second-old-id"):
        notice = await gateway._hmwa_pending_turn_redelivery_notice(
            MessageEvent(text="late platform replay", source=source, message_id=old_id),
            source,
            current,
            key,
        )
        assert notice == PENDING_TURN_REDELIVERY_NOTICE
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_collapsed_media_turn_stages_every_constituent_delivery_id(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    first = MessageEvent(
        text="first caption",
        source=source,
        message_type=MessageType.PHOTO,
        message_id="album-1",
        media_urls=["/tmp/one.png"],
        media_types=["image/png"],
    )
    second = MessageEvent(
        text="second caption",
        source=source,
        message_type=MessageType.PHOTO,
        message_id="album-2",
        media_urls=["/tmp/two.png"],
        media_types=["image/png"],
    )
    pending = {key: first}
    merge_pending_message_event(pending, key, second)
    collapsed = pending[key]
    gateway = _gateway(store)

    await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(collapsed.text),
        "compression timed out",
        old,
        key,
        source,
        event=collapsed,
    )

    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 1
    draft = load_pending_turn_draft(
        store._db_for_key(key), rows[0][0].removeprefix(PENDING_TURN_DRAFT_PREFIX)
    )
    assert draft.platform_message_ids == ["album-1", "album-2"]
    assert draft.exact is False
    assert draft.executable is False
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_retry_returns_to_ordinary_semantics_after_manual_resend_ack(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _old, current, _draft = _install_recovery(store, source)
    key = current.session_key
    store.append_to_transcript(current.session_id, {"role": "user", "content": "retry me"})
    store.append_to_transcript(
        current.session_id, {"role": "assistant", "content": "old answer"}
    )
    gateway = _gateway(store)
    gateway._handle_message = AsyncMock(return_value="new answer")

    resend = MessageEvent(
        text="manual resend", source=source, message_id="manual-new-id"
    )
    admitted = await gateway._hmwa_pending_turn_redelivery_notice(
        resend,
        source,
        current,
        key,
    )
    assert await gateway._hmwa_commit_pending_turn_resend_acknowledgement(
        resend, source, key, {"api_calls": 1}
    ) is None
    result = await gateway._handle_retry_command(
        MessageEvent(
            text="/retry",
            source=source,
            message_type=MessageType.COMMAND,
            message_id="retry-command-id",
        )
    )

    assert admitted is None
    assert result == "new answer"
    gateway._handle_message.assert_awaited_once()
    retry_event = gateway._handle_message.await_args.args[0]
    assert retry_event.text == "retry me"
    assert retry_event.source == source
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_shared_route_database_failure_reveals_same_generic_notice(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch, shared=True)
    owner = _source("U1")
    unrelated = _source("U2")
    current = store.get_or_create_session(owner)
    key = current.session_key
    gateway = _gateway(store)
    store._db_for_key = MagicMock(side_effect=OSError("database unavailable"))

    notices = []
    for source in (owner, unrelated):
        notices.append(
            await gateway._hmwa_pending_turn_redelivery_notice(
                MessageEvent(text="message", source=source, message_id="delivery-id"),
                source,
                current,
                key,
            )
        )

    assert notices == [
        PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
        PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
    ]
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_malformed_generation_is_collected_without_wedging_valid_owner(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    _old, current, valid = _install_recovery(store, source)
    key = current.session_key
    db = store._db_for_key(key)
    malformed_key = f"{PENDING_TURN_DRAFT_PREFIX}malformed"
    db.set_meta(malformed_key, "not-json")
    gateway = _gateway(store)

    resend = MessageEvent(text="manual resend", source=source, message_id="fresh-id")
    admitted = await gateway._hmwa_pending_turn_redelivery_notice(
        resend,
        source,
        current,
        key,
    )
    assert await gateway._hmwa_commit_pending_turn_resend_acknowledgement(
        resend, source, key, {"api_calls": 1}
    ) is None

    assert admitted is None
    assert db.get_meta(malformed_key) is None
    assert load_pending_turn_draft(db, valid.draft_id).manual_resend_acknowledged_at
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_overflowing_constituent_ids_refuse_reset_without_truncation(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    source = _source()
    current = store.get_or_create_session(source)
    key = current.session_key
    gateway = _gateway(store)
    event = MessageEvent(
        text="collapsed burst",
        source=source,
        message_id="delivery-0",
        _accepted_delivery_ids=tuple(f"delivery-{index}" for index in range(65)),
    )
    reset_spy = MagicMock(wraps=store.reset_session)
    store.reset_session = reset_spy

    response, returned = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result("collapsed burst"),
        "compression timed out",
        current,
        key,
        source,
        event=event,
    )

    reset_spy.assert_not_called()
    assert returned.session_id == current.session_id
    assert "could not be saved safely" in response
    assert store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX) == []
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_outer_handler_preserves_queued_terminal_event_provenance(
    tmp_path, monkeypatch
):
    import gateway.run_heartbeat_acceptance as heartbeat_acceptance

    store = _store(tmp_path, monkeypatch)
    source = _source()
    opening_event = MessageEvent(
        text="opening turn", source=source, message_id="opening-id"
    )
    terminal_event = MessageEvent(
        text="queued turn that timed out", source=source, message_id="queued-id"
    )
    current = store.get_or_create_session(source)
    key = current.session_key
    gateway = _gateway(store)
    gateway.hooks = SimpleNamespace(emit=AsyncMock())
    gateway._hmwa_resolve_session = AsyncMock(return_value=(source, current, key))
    gateway._hmwa_pending_turn_redelivery_notice = AsyncMock(return_value=None)
    gateway._hmwa_prepare_turn = AsyncMock(
        return_value=(
            gateway._PreparedTurn(
                history=[],
                context_prompt="context",
                message_text="opening turn",
                persist_user_message="opening turn",
                persist_user_timestamp=None,
                persist_user_display_kind=None,
                persistence_session_id=current.session_id,
                persistence_owner="owner",
            ),
            (),
        )
    )
    terminal_result = {
        **_preflight_result("queued turn that timed out"),
        "final_response": "compression timed out",
        "messages": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "_queued_terminal_event": terminal_event,
        "_queued_terminal_source": source,
        "_queued_terminal_session_key": key,
    }
    gateway._run_agent = AsyncMock(return_value=terminal_result)
    gateway._reply_anchor_for_event = MagicMock(return_value=None)
    gateway._hmwa_stop_typing_for_turn = AsyncMock()
    gateway._is_session_run_current = MagicMock(return_value=True)
    gateway._hmwa_shape_agent_response = AsyncMock(
        return_value=("compression timed out", False, [])
    )
    gateway._hmwa_prepend_reasoning = MagicMock(
        side_effect=lambda _result, response, _source, _silence: response
    )
    gateway._hmwa_runtime_footer_line = MagicMock(return_value=None)
    gateway._hmwa_post_turn_hooks = AsyncMock()
    gateway._hmwa_persist_turn_transcript = AsyncMock()
    gateway._hmwa_deliver_turn_response = AsyncMock(
        side_effect=lambda _event, _source, _entry, _key, _generation,
        _result, _messages, response, _footer, _silence: response
    )
    gateway._clear_session_env = MagicMock()
    monkeypatch.setattr(
        heartbeat_acceptance, "heartbeat_owner_is_current", lambda *_args: True
    )

    response = await gateway._handle_message_with_agent(
        opening_event, source, key, 1
    )

    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 1
    draft = load_pending_turn_draft(
        store._db_for_key(key), rows[0][0].removeprefix(PENDING_TURN_DRAFT_PREFIX)
    )
    assert draft.platform_message_ids == ["queued-id"]
    assert draft.text == "queued turn that timed out"
    assert opening_event.ledger_message_id is None
    assert "_queued_terminal_event" not in terminal_result
    assert "_queued_terminal_source" not in terminal_result
    assert "_queued_terminal_session_key" not in terminal_result
    assert "Session auto-reset" in response
    store.close_all_db_handles()
