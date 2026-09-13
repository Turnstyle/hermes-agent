"""Failure-boundary regressions for private pending-turn recovery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.pending_turn_draft import (
    PENDING_TURN_DRAFT_PREFIX,
    PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
    PENDING_TURN_REDELIVERY_NOTICE,
    has_unacknowledged_pending_turn_draft,
    load_pending_turn_draft,
    pending_turn_principal_fingerprint,
    stage_pending_turn_draft,
)
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


def _preflight_result() -> dict:
    return {
        "failed": True,
        "compression_exhausted": True,
        "turn_exit_reason": "context_compression_timeout",
        "api_calls": 0,
        "unprocessed_input": {
            "version": 1,
            "kind": "accepted_user_input",
            "text": "accepted but not processed",
            "input_kind": "plain_text",
            "exact": True,
            "lossy": False,
            "loss_reasons": [],
            "safe_to_replay": True,
            "provider_call_sent": False,
            "executable": False,
        },
    }


def _gateway(store: SessionStore):
    from gateway.run import GatewayRunner

    gateway = GatewayRunner.__new__(GatewayRunner)
    gateway.config = store.config
    gateway.session_store = store
    gateway._evict_cached_agent = MagicMock()
    gateway._clear_conversation_scope = MagicMock()
    gateway._sync_telegram_topic_binding = MagicMock()
    return gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["post_publish", "ambiguous_readback"])
async def test_reset_error_preserves_recovery_unless_old_route_is_confirmed(
    tmp_path, monkeypatch, failure_mode
):
    """Late failure follows the published route; unknown state retains the safe record."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    gateway = _gateway(store)

    if failure_mode == "post_publish":
        # The real transition finisher must absorb even resolver failures itself.
        original_db_for_key = store._db_for_key
        original_finish = store._finish_route_transition
        monkeypatch.setattr(
            store,
            "_db_for_key",
            MagicMock(side_effect=OSError("session database unavailable")),
        )
        original_finish(
            key,
            end_session_id=old.session_id,
            end_reason="session_reset",
            create_kwargs={"session_id": "unwritten-successor"},
            origin=source,
            display_name=None,
        )
        monkeypatch.setattr(store, "_db_for_key", original_db_for_key)

        # Exercise the caller's independent defense against any unexpected late
        # transition exception after reset_session has published the durable route.
        monkeypatch.setattr(
            store,
            "_finish_route_transition",
            MagicMock(side_effect=OSError("late transition failure")),
        )
    else:
        monkeypatch.setattr(
            store,
            "reset_session",
            MagicMock(side_effect=OSError("route publication unavailable")),
        )
        monkeypatch.setattr(
            store,
            "reconcile_durable_routing_entry",
            MagicMock(side_effect=OSError("route readback unavailable")),
        )

    response, returned = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(
            text="accepted but not processed",
            source=source,
            message_id="delivery-1",
        ),
    )

    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 1
    draft = load_pending_turn_draft(
        store._db_for_key(key),
        rows[0][0].removeprefix(PENDING_TURN_DRAFT_PREFIX),
    )
    assert draft.executable is False

    if failure_mode == "post_publish":
        assert returned.session_id != old.session_id
        assert store.peek_session_id(key) == returned.session_id
        assert draft.successor_session_id == returned.session_id
        assert "Session auto-reset" in response
        assert "kept the existing session" not in response
        assert "outcome could not be confirmed" not in response
        gateway._evict_cached_agent.assert_called_once_with(key)
        gateway._clear_conversation_scope.assert_called_once()
    else:
        assert returned.session_id == old.session_id
        assert store.peek_session_id(key) == old.session_id
        assert draft.successor_session_id is None
        assert "Session reset outcome could not be confirmed" in response
        assert "kept the existing session" not in response
        gateway._evict_cached_agent.assert_not_called()
        gateway._clear_conversation_scope.assert_not_called()

    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_reset_readback_adopts_database_successor_after_ambiguous_save_error(
    tmp_path, monkeypatch
):
    """A committed DB route wins even when memory and the JSON mirror stayed old."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _source()
    old = store.get_or_create_session(source)
    key = old.session_key
    gateway = _gateway(store)
    routing_db = store._routing_db
    original_replace = routing_db.replace_gateway_routing_entries

    def _commit_then_raise(entries, *, scope):
        original_replace(entries, scope=scope)
        raise OSError("commit settlement unknown")

    monkeypatch.setattr(routing_db, "replace_gateway_routing_entries", _commit_then_raise)
    monkeypatch.setattr(
        store,
        "_save_sessions_json",
        MagicMock(side_effect=OSError("legacy mirror unavailable")),
    )

    response, returned = await gateway._hmwa_compression_exhaustion_reset(
        _preflight_result(),
        "compression timed out",
        old,
        key,
        source,
        event=MessageEvent(
            text="accepted but not processed",
            source=source,
            message_id="delivery-ambiguous-commit",
        ),
    )

    assert returned.session_id != old.session_id
    assert store.lookup_by_session_key(key).session_id == returned.session_id
    rows = store._db_for_key(key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 1
    draft = load_pending_turn_draft(
        store._db_for_key(key), rows[0][0].removeprefix(PENDING_TURN_DRAFT_PREFIX)
    )
    assert draft.successor_session_id == returned.session_id
    assert "Session auto-reset" in response
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_retry_database_failure_is_principal_indistinguishable_and_text_free(
    monkeypatch,
):
    """Both retry entry points fail closed before either retained-text read path."""
    import gateway.pending_turn_draft as pending_turn_draft
    from gateway.run import GatewayRunner

    config = GatewayConfig(group_sessions_per_user=False)
    backing_store = MagicMock()
    session_entry = SimpleNamespace(session_key="shared-route", session_id="current-session")
    facade = SimpleNamespace(
        _store=backing_store,
        get_or_create_session=AsyncMock(return_value=session_entry),
        _db_for_key=AsyncMock(side_effect=OSError("session database unavailable")),
        load_transcript=AsyncMock(
            side_effect=AssertionError("retry must not load transcript text")
        ),
    )
    gateway = GatewayRunner.__new__(GatewayRunner)
    gateway.config = config
    gateway.session_store = backing_store
    gateway._async_session_store = facade

    recovery_loader = MagicMock(
        side_effect=AssertionError("retry must not load retained recovery text")
    )
    monkeypatch.setattr(pending_turn_draft, "load_pending_turn_draft", recovery_loader)

    owner = _source("U1")
    unrelated = _source("U2")
    direct_results = []
    command_results = []
    for source in (owner, unrelated):
        event = MessageEvent(
            text="/retry",
            message_type=MessageType.COMMAND,
            source=source,
            message_id=f"retry-{source.user_id}",
        )
        direct_results.append(
            await gateway._handle_pending_turn_draft_retry(event, session_entry)
        )
        command_results.append(await gateway._handle_retry_command(event))

    assert direct_results == [
        PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
        PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
    ]
    assert command_results == direct_results
    facade.load_transcript.assert_not_awaited()
    recovery_loader.assert_not_called()


@pytest.mark.asyncio
async def test_each_manual_resend_acknowledges_one_generation_without_blocking(
    tmp_path, monkeypatch
):
    """A committed oldest-generation ack admits the resend while later work remains."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _source()
    current = store.get_or_create_session(source)
    key = current.session_key
    db = store._db_for_key(key)
    principal = pending_turn_principal_fingerprint(source)
    drafts = [
        stage_pending_turn_draft(
            db,
            text=f"lost generation {index}",
            predecessor_session_id=f"predecessor-{index}",
            session_key=key,
            principal_fingerprint=principal,
            platform_message_id=f"old-delivery-{index}",
        )
        for index in (1, 2)
    ]
    gateway = _gateway(store)

    first_event = MessageEvent(
        text="first manual resend", source=source, message_id="manual-1"
    )
    first_notice = await gateway._hmwa_pending_turn_redelivery_notice(
        first_event,
        source,
        current,
        key,
    )
    first_plan = first_event._pending_turn_resend_acknowledgement
    restored = [load_pending_turn_draft(db, draft.draft_id) for draft in drafts]
    assert all(draft.manual_resend_acknowledged_at is None for draft in restored)
    first_result, first_unsettled = (
        await gateway._hmwa_settle_turn_resend_acknowledgement(
            first_event, source, key, {"api_calls": 1}
        )
    )

    restored = [load_pending_turn_draft(db, draft.draft_id) for draft in drafts]
    assert first_notice is None
    assert first_result["api_calls"] == 1
    assert first_unsettled is False
    assert sum(draft.manual_resend_acknowledged_at is not None for draft in restored) == 1
    assert (
        load_pending_turn_draft(db, first_plan.draft_id).manual_resend_message_id_hash
        == first_plan.manual_resend_message_id_hash
    )
    assert has_unacknowledged_pending_turn_draft(
        db,
        session_key=key,
        principal_fingerprint=principal,
    )

    second_event = MessageEvent(
        text="second manual resend", source=source, message_id="manual-2"
    )
    second_notice = await gateway._hmwa_pending_turn_redelivery_notice(
        second_event,
        source,
        current,
        key,
    )
    second_plan = second_event._pending_turn_resend_acknowledgement
    second_result, second_unsettled = (
        await gateway._hmwa_settle_turn_resend_acknowledgement(
            second_event, source, key, {"api_calls": 1}
        )
    )

    assert second_notice is None
    assert second_plan.draft_id != first_plan.draft_id
    assert second_result["api_calls"] == 1
    assert second_unsettled is False
    assert (
        load_pending_turn_draft(db, second_plan.draft_id).manual_resend_message_id_hash
        == second_plan.manual_resend_message_id_hash
    )
    assert not has_unacknowledged_pending_turn_draft(
        db,
        session_key=key,
        principal_fingerprint=principal,
    )
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_resend_ack_waits_for_provider_and_replay_cannot_consume_next_generation(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _source()
    current = store.get_or_create_session(source)
    key = current.session_key
    db = store._db_for_key(key)
    principal = pending_turn_principal_fingerprint(source)
    drafts = [
        stage_pending_turn_draft(
            db,
            text=f"lost generation {index}",
            predecessor_session_id=f"predecessor-{index}",
            session_key=key,
            principal_fingerprint=principal,
            platform_message_id=f"old-delivery-{index}",
        )
        for index in (1, 2)
    ]
    oldest, remaining = sorted(
        drafts, key=lambda draft: (draft.created_at, draft.draft_id)
    )
    gateway = _gateway(store)
    resend = MessageEvent(
        text="first manual resend", source=source, message_id="manual-1"
    )

    assert await gateway._hmwa_pending_turn_redelivery_notice(
        resend, source, current, key
    ) is None
    _rejected_result, rejected_unsettled = (
        await gateway._hmwa_settle_turn_resend_acknowledgement(
            resend,
            source,
            key,
            {"api_calls": 0},
        )
    )
    assert rejected_unsettled is True
    assert (
        load_pending_turn_draft(db, oldest.draft_id).manual_resend_acknowledged_at
        is None
    )

    assert await gateway._hmwa_pending_turn_redelivery_notice(
        resend, source, current, key
    ) is None
    _accepted_result, accepted_unsettled = (
        await gateway._hmwa_settle_turn_resend_acknowledgement(
            resend,
            source,
            key,
            {"api_calls": 1},
        )
    )
    assert accepted_unsettled is False
    acknowledged = load_pending_turn_draft(db, oldest.draft_id)
    assert acknowledged.manual_resend_message_id_hash
    assert load_pending_turn_draft(
        db, remaining.draft_id
    ).manual_resend_acknowledged_at is None

    replay_notice = await gateway._hmwa_pending_turn_redelivery_notice(
        MessageEvent(
            text="first manual resend", source=source, message_id="manual-1"
        ),
        source,
        current,
        key,
    )
    assert replay_notice == PENDING_TURN_REDELIVERY_NOTICE
    assert load_pending_turn_draft(
        db, remaining.draft_id
    ).manual_resend_acknowledged_at is None
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_recursion_cap_requeues_cross_principal_event_without_losing_head():
    """A refused principal merge becomes FIFO state instead of dropping either event."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = _source("U1")
    pending_event = MessageEvent(
        text="participant two",
        source=_source("U2"),
        message_id="participant-two-id",
    )
    newer_head = MessageEvent(
        text="participant one newer",
        source=source,
        message_id="participant-one-id",
    )
    session_key = "shared-route"
    overflow = []
    adapter = SimpleNamespace(
        _pending_messages={session_key: newer_head},
        _active_sessions={},
    )
    result = {"final_response": "earlier result", "messages": []}
    turn_ctx = SimpleNamespace(
        source=source,
        session_id="current-session",
        session_key=session_key,
        run_generation=1,
        _interrupt_depth=runner._MAX_INTERRUPT_DEPTH,
        history=[],
        _status_thread_metadata={},
        result_holder=[result],
    )
    runner._session_key_for_source = MagicMock(return_value=session_key)
    runner._hmwa_pending_turn_redelivery_notice = AsyncMock(return_value=None)
    runner._adapter_for_source = MagicMock(return_value=adapter)
    runner._overflow_queue = MagicMock(return_value=overflow)

    returned = await runner._run_agent_queued_followup(
        turn_ctx,
        adapter,
        pending_event.text,
        pending_event,
        result,
        result,
        None,
    )

    assert returned is result
    assert adapter._pending_messages[session_key] is pending_event
    assert overflow == [newer_head]
    assert pending_event._gateway_accepted is True


@pytest.mark.asyncio
async def test_queued_followup_passes_exact_event_to_recursive_turn():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    source = _source()
    session_key = "shared-route"
    pending_event = MessageEvent(
        text="queued provider turn",
        source=source,
        message_id="queued-delivery",
    )
    opening_result = {
        "interrupted": True,
        "api_calls": 0,
        "messages": [],
    }
    queued_result = {
        "final_response": "queued turn finished",
        "api_calls": 1,
        "messages": [],
    }
    adapter = SimpleNamespace(
        _pending_messages={},
        _active_sessions={},
        send_typing=AsyncMock(),
    )
    turn_ctx = SimpleNamespace(
        source=source,
        session_id="current-session",
        session_key=session_key,
        run_generation=1,
        _interrupt_depth=0,
        history=[],
        context_prompt=None,
        _status_thread_metadata={},
        result_holder=[opening_result],
    )
    runner._session_key_for_source = MagicMock(return_value=session_key)
    runner._hmwa_pending_turn_redelivery_notice = AsyncMock(return_value=None)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        return_value=pending_event.text
    )
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._adapter_for_source = MagicMock(return_value=adapter)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._run_agent = AsyncMock(return_value=queued_result)
    runner._is_goal_continuation_event = MagicMock(return_value=False)

    returned = await runner._run_agent_queued_followup(
        turn_ctx,
        adapter,
        pending_event.text,
        pending_event,
        "opening response",
        opening_result,
        None,
    )

    assert returned["api_calls"] == 1
    assert runner._run_agent.await_args.kwargs["turn_event"] is pending_event


@pytest.mark.asyncio
async def test_preflight_barrier_marks_promoted_overflow_before_recovery(
    tmp_path, monkeypatch
):
    """B and promoted C predate A's reset, so C cannot acknowledge A's lost turn."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _source()
    old = store.get_or_create_session(source)
    session_key = old.session_key
    gateway = _gateway(store)
    # Exercise the production boundary funnel: it clears conversation state, and the
    # compaction-reset path must restore only the accepted preflight FIFO.
    del gateway._clear_conversation_scope
    opening_event = MessageEvent(
        text="A that times out",
        source=source,
        message_id="delivery-a",
    )
    queued_b = MessageEvent(
        text="preaccepted B",
        source=source,
        message_id="delivery-b",
    )
    overflow_c = MessageEvent(
        text="preaccepted C",
        source=source,
        message_id="delivery-c",
    )
    adapter = SimpleNamespace(
        _pending_messages={},
        _active_sessions={},
    )
    adapter.get_pending_message = MagicMock(
        side_effect=lambda key: adapter._pending_messages.pop(key, None)
    )
    gateway._enqueue_fifo(session_key, queued_b, adapter)
    gateway._enqueue_fifo(session_key, overflow_c, adapter)
    assert adapter._pending_messages[session_key] is queued_b
    assert gateway._overflow_queue(session_key) == [overflow_c]
    result = _preflight_result()
    turn_ctx = SimpleNamespace(
        source=source,
        session_id=old.session_id,
        session_key=session_key,
        run_generation=1,
        _interrupt_depth=0,
        history=[],
        _status_thread_metadata={},
        result_holder=[result],
    )

    pending_event, pending = await gateway._run_agent_drain_pending(
        result, adapter, source, session_key
    )
    assert pending_event is queued_b
    assert adapter._pending_messages[session_key] is overflow_c

    returned = await gateway._run_agent_queued_followup(
        turn_ctx,
        adapter,
        pending,
        pending_event,
        result,
        result,
        None,
    )

    assert returned is result
    assert adapter._pending_messages[session_key] is queued_b
    assert gateway._overflow_queue(session_key) == [overflow_c]
    assert queued_b._predates_pending_turn_recovery is True
    assert overflow_c._predates_pending_turn_recovery is True

    late_event = MessageEvent(
        text="arrived after the preflight barrier",
        source=source,
        message_id="delivery-late",
    )
    late_event._gateway_accepted = True
    rejected_event = MessageEvent(
        text="not accepted",
        source=source,
        message_id="delivery-rejected",
    )
    rejected_event._predates_pending_turn_recovery = True
    gateway._session_state(session_key).conversation.queued_events.extend(
        [late_event, rejected_event]
    )

    _response, successor = await gateway._hmwa_compression_exhaustion_reset(
        result,
        "compression timed out",
        old,
        session_key,
        source,
        event=opening_event,
    )
    assert adapter._pending_messages[session_key] is queued_b
    assert gateway._overflow_queue(session_key) == [overflow_c]
    admitted = await gateway._hmwa_pending_turn_redelivery_notice(
        overflow_c,
        source,
        successor,
        session_key,
    )

    rows = store._db_for_key(session_key).list_meta_prefix(PENDING_TURN_DRAFT_PREFIX)
    assert len(rows) == 1
    draft = load_pending_turn_draft(
        store._db_for_key(session_key),
        rows[0][0].removeprefix(PENDING_TURN_DRAFT_PREFIX),
    )
    assert admitted is None
    assert draft.manual_resend_acknowledged_at is None
    store.close_all_db_handles()
