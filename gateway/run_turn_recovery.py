"""Pending-input recovery for gateway agent turns.

The normal turn runner keeps only the wiring that admits, executes, and delivers a turn.  This
sibling owns the exceptional path where turn-start compaction times out after gateway input was
accepted but before a provider call was made, plus the resend tombstone and queued-turn barriers
needed to recover without replaying input.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from typing import Any, Optional

from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


logger = logging.getLogger("gateway.run")


@dataclass(frozen=True)
class _PendingTurnPreflight:
    """Validated preservation inputs for one no-provider-call preflight exit."""

    is_no_provider_preflight: bool
    unprocessed: Optional[dict]
    input_kind: Optional[str]
    loss_reasons: Any
    normalization_changed: bool
    delivery_ids: tuple
    preservable: bool


class GatewayTurnRecoveryMixin:
    """Recovery state machine mixed into :class:`gateway.run_turn.GatewayTurnMixin`."""

    @staticmethod
    def _hmwa_shape_unprocessed_input(unprocessed: Any, event: Any) -> Any:
        """Restore attachment-bearing gateway shape to a text-normalized core result."""

        if not isinstance(unprocessed, dict) or event is None:
            return unprocessed
        event_type = str(
            getattr(getattr(event, "message_type", None), "value", "") or ""
        ).lower()
        media_types = [
            str(value or "").lower()
            for value in (getattr(event, "media_types", None) or [])
        ]
        has_attachment = bool(
            getattr(event, "media_urls", None)
            or event_type
            in {"photo", "video", "audio", "voice", "document", "sticker"}
        )
        if not has_attachment:
            return unprocessed

        shaped = dict(unprocessed)
        loss_reasons = set(shaped.get("loss_reasons") or [])
        native_image = bool(
            event_type == "photo"
            or any(value.startswith("image/") for value in media_types)
        )
        shaped["input_kind"] = "native_image" if native_image else "structured"
        loss_reasons.add("attachments_not_retained")
        if not native_image:
            loss_reasons.add("structured_components_not_retained")
        shaped["loss_reasons"] = sorted(loss_reasons)
        shaped["exact"] = False
        shaped["lossy"] = True
        shaped["safe_to_replay"] = False
        return shaped

    def _hmwa_pending_turn_preflight(
        self, agent_result: dict, event: Any
    ) -> _PendingTurnPreflight:
        """Classify whether a typed preflight exit can be durably preserved."""

        from gateway.platforms.event import accepted_delivery_ids

        unprocessed = self._hmwa_shape_unprocessed_input(
            agent_result.get("unprocessed_input"), event
        )
        is_no_provider_preflight = bool(
            agent_result.get("turn_exit_reason") == "context_compression_timeout"
            and agent_result.get("api_calls") == 0
        )
        input_kind = (
            unprocessed.get("input_kind") if isinstance(unprocessed, dict) else None
        )
        loss_reasons = (
            unprocessed.get("loss_reasons") if isinstance(unprocessed, dict) else None
        )
        exact = unprocessed.get("exact") if isinstance(unprocessed, dict) else None
        lossy = unprocessed.get("lossy") if isinstance(unprocessed, dict) else None
        normalization_changed = bool(
            isinstance(unprocessed, dict)
            and unprocessed.get("normalization_changed") is True
        )
        required_reason = {
            "structured": "structured_components_not_retained",
            "native_image": "attachments_not_retained",
            "moa": "per_turn_semantics_not_retained",
        }.get(input_kind)
        metadata_valid = bool(
            input_kind in {"plain_text", "structured", "native_image", "moa"}
            and isinstance(exact, bool)
            and isinstance(lossy, bool)
            and exact is (not lossy)
            and (not exact or input_kind == "plain_text")
            and isinstance(loss_reasons, list)
            and all(isinstance(reason, str) for reason in loss_reasons)
            and len(loss_reasons) == len(set(loss_reasons))
            and lossy is bool(loss_reasons)
            and (required_reason is None or required_reason in loss_reasons)
            and normalization_changed
            == ("surrogate_normalization" in loss_reasons)
            and unprocessed.get("safe_to_replay") is exact
            and unprocessed.get("executable") is False
        )
        delivery_ids = accepted_delivery_ids(event) if event is not None else ()
        preservable = bool(
            is_no_provider_preflight
            and isinstance(unprocessed, dict)
            and unprocessed.get("version") == 1
            and unprocessed.get("kind") == "accepted_user_input"
            and unprocessed.get("provider_call_sent") is False
            and metadata_valid
            and isinstance(unprocessed.get("text"), str)
            and unprocessed.get("text", "").strip()
            # The platform delivery id is the replay tombstone. Without one, a redelivery is
            # indistinguishable from a deliberate manual resend after route rotation.
            and delivery_ids
        )
        return _PendingTurnPreflight(
            is_no_provider_preflight=is_no_provider_preflight,
            unprocessed=unprocessed,
            input_kind=input_kind,
            loss_reasons=loss_reasons,
            normalization_changed=normalization_changed,
            delivery_ids=tuple(delivery_ids),
            preservable=preservable,
        )

    @staticmethod
    def _hmwa_refused_preservation_response(response: Any) -> str:
        return (response or "") + (
            "\n\n⚠️ Session reset was refused because your unprocessed message "
            "could not be saved safely. Copy it, then reset and resend it manually."
        )

    async def _hmwa_stage_preflight_recovery(
        self,
        preflight: _PendingTurnPreflight,
        *,
        agent_result: dict,
        session_entry: Any,
        session_key: str,
        source: SessionSource,
    ) -> tuple[Any, Any]:
        """Persist the non-executable recovery generation before rotating the route."""

        from gateway.pending_turn_draft import (
            pending_turn_principal_fingerprint,
            stage_pending_turn_draft,
        )

        db = await self.async_session_store._db_for_key(session_key)
        principal = pending_turn_principal_fingerprint(source)
        draft = await asyncio.to_thread(
            stage_pending_turn_draft,
            db,
            text=preflight.unprocessed["text"],
            predecessor_session_id=session_entry.session_id,
            session_key=session_key,
            principal_fingerprint=principal,
            platform_message_id=preflight.delivery_ids[0],
            platform_message_ids=list(preflight.delivery_ids),
            turn_exit_reason=agent_result.get("turn_exit_reason"),
            input_kind=preflight.input_kind,
            input_loss_reasons=preflight.loss_reasons,
            input_normalization_changed=preflight.normalization_changed,
        )
        return draft, db

    async def _hmwa_reconcile_failed_recovery_reset(
        self,
        *,
        session_key: str,
        session_entry: Any,
        pending_draft: Any,
        pending_db: Any,
    ) -> tuple[Any, Optional[str], Any]:
        """Resolve an exception/None from reset using authoritative route readback."""

        route_readback_confirmed = False
        current_session_id = None
        try:
            current = await self.async_session_store.reconcile_durable_routing_entry(
                session_key,
                expected_session_id=session_entry.session_id,
            )
            current_session_id = str(getattr(current, "session_id", "") or "")
        except Exception:
            current = None
        else:
            route_readback_confirmed = bool(current_session_id)

        if route_readback_confirmed and current_session_id != session_entry.session_id:
            # Publication won even though a post-publish cleanup step raised.
            logger.warning(
                "Compression-exhaustion reset advanced the route despite a later "
                "transition error; retaining recovery %s.",
                pending_draft.draft_id,
            )
            return current, None, session_entry

        if route_readback_confirmed and current_session_id == session_entry.session_id:
            # The predecessor remains authoritative. Delete only this staged generation; a
            # platform retry can then attempt the whole transaction again.
            from gateway.pending_turn_draft import delete_pending_turn_draft

            try:
                await asyncio.to_thread(
                    delete_pending_turn_draft,
                    pending_db,
                    pending_draft.draft_id,
                )
            except Exception:
                logger.error(
                    "Compression-exhaustion reset failed and staged recovery %s could not "
                    "be discarded; it remains unlinked and non-executable.",
                    pending_draft.draft_id,
                )
            logger.error(
                "Compression-exhaustion reset failed before recovery %s was routed.",
                pending_draft.draft_id,
            )
            notice = (
                "\n\n⚠️ Session reset did not complete, so Hermes kept the existing "
                "session and will not replay your message. Copy it, then reset and resend "
                "it manually. Attachments and one-shot or per-turn semantics were not retained."
            )
            return None, notice, current

        # Missing or failed readback is ambiguous. Retain the non-executable row because deleting
        # it could erase the sole record after a successful route publication.
        logger.error(
            "Compression-exhaustion reset outcome is unverified; retaining "
            "non-executable recovery %s.",
            pending_draft.draft_id,
        )
        notice = (
            "\n\n⚠️ Session reset outcome could not be confirmed. A private, "
            "non-executable recovery record remains saved, and Hermes will not replay "
            "it. Reset manually and resend the original request as a new message. "
            "Attachments and one-shot or per-turn semantics were not retained."
        )
        return None, notice, session_entry

    async def _hmwa_reset_with_recovery_reconciliation(
        self,
        *,
        session_key: str,
        session_entry: Any,
        pending_draft: Any,
        pending_db: Any,
    ) -> tuple[Any, Optional[str], Any]:
        """Rotate a session, reconciling any ambiguous post-publication failure."""

        try:
            # Recovery rows are route-independent. The new route must not carry a mutable,
            # single-generation pointer that later resets could displace.
            new_entry = await self.async_session_store.reset_session(session_key)
        except Exception:
            if pending_draft is None:
                raise
            new_entry = None
        if pending_draft is not None and new_entry is None:
            return await self._hmwa_reconcile_failed_recovery_reset(
                session_key=session_key,
                session_entry=session_entry,
                pending_draft=pending_draft,
                pending_db=pending_db,
            )
        return new_entry, None, session_entry

    def _hmwa_pre_reset_recovery_fifo(
        self, session_key: str, pending_draft: Any
    ) -> list:
        """Retain only deliveries admitted before the preflight recovery barrier."""

        if pending_draft is None:
            return []
        return [
            event
            for event in (self._overflow_queue(session_key) or ())
            if getattr(event, "_predates_pending_turn_recovery", False) is True
            and getattr(event, "_gateway_accepted", False) is True
        ]

    async def _hmwa_apply_recovery_reset_boundary(
        self,
        *,
        new_entry: Any,
        session_key: str,
        source: SessionSource,
        pending_draft: Any,
        pending_db: Any,
    ) -> None:
        """Clear conversation state, restore older FIFO input, and finalize recovery audit links."""

        pre_reset_fifo = self._hmwa_pre_reset_recovery_fifo(session_key, pending_draft)
        self._evict_cached_agent(session_key)
        self._clear_conversation_scope(session_key, reason="compression_exhausted_reset")
        if pre_reset_fifo:
            restored_fifo = self._session_state(session_key).conversation.queued_events
            restored_ids = {id(event) for event in restored_fifo}
            for event in pre_reset_fifo:
                if id(event) not in restored_ids:
                    restored_fifo.append(event)
                    restored_ids.add(id(event))

        # Without this resync, Telegram's topic-heal walk would route the next inbound message
        # back to the oversized compressed child and recreate the exhaustion loop.
        await asyncio.to_thread(
            self._sync_telegram_topic_binding,
            source,
            new_entry,
            reason="compression-exhausted-reset",
        )
        if pending_draft is None:
            return

        from gateway.pending_turn_draft import link_pending_turn_draft

        try:
            await asyncio.to_thread(
                link_pending_turn_draft,
                pending_db,
                pending_draft.draft_id,
                predecessor_session_id=pending_draft.predecessor_session_id,
                successor_session_id=new_entry.session_id,
            )
        except Exception:
            # Owner/message-id lookups remain authoritative even if this audit backlink fails.
            logger.error(
                "Pending recovery %s was preserved, but its successor backlink could not be "
                "finalized.",
                pending_draft.draft_id,
            )

    @staticmethod
    def _hmwa_recovery_reset_notice(
        response: Any, pending_draft: Any, new_entry: Any
    ) -> str:
        if pending_draft is not None and new_entry is not None:
            notice = (
                "\n\n🔄 Session auto-reset. A bounded, redacted, private, non-executable "
                "recovery record was saved before reset. Hermes did not run it and will not "
                "replay it. Resend the original request manually as a new message. "
                "Attachments and one-shot or per-turn semantics were not retained."
            )
        elif pending_draft is not None:
            notice = (
                "\n\n⚠️ A private, non-executable recovery record was saved, but the session "
                "reset did not complete. Hermes will not replay it. Reset manually and resend "
                "the original request as a new message. Attachments and one-shot or per-turn "
                "semantics were not retained."
            )
        else:
            notice = (
                "\n\n🔄 Session auto-reset — the conversation exceeded the maximum context size "
                "and could not be compressed further. Your next message will start a fresh session."
            )
        return (response or "") + notice

    async def _hmwa_compression_exhaustion_reset(
        self,
        agent_result: dict,
        response: Any,
        session_entry: Any,
        session_key: str,
        source: SessionSource,
        event: Any = None,
    ) -> tuple[Any, Any]:
        """Preserve an unprocessed preflight turn, then rotate an exhausted session."""

        if agent_result.get("compression_deferred"):
            logger.info(
                "Compression deferred for session %s — the compression lock is held by a "
                "concurrent compressor. Keeping the session intact; the next message retries "
                "normally.",
                session_entry.session_id if session_entry else "?",
            )
            return response, session_entry
        if not (
            agent_result.get("compression_exhausted")
            and session_entry
            and session_key
        ):
            return response, session_entry

        preflight = self._hmwa_pending_turn_preflight(agent_result, event)
        if preflight.is_no_provider_preflight and not preflight.preservable:
            logger.error(
                "Refusing compression-exhaustion reset for session %s because the "
                "no-provider-call result did not carry valid preservable input.",
                session_entry.session_id,
            )
            return self._hmwa_refused_preservation_response(response), session_entry

        pending_draft = None
        pending_db = None
        if preflight.preservable:
            try:
                pending_draft, pending_db = await self._hmwa_stage_preflight_recovery(
                    preflight,
                    agent_result=agent_result,
                    session_entry=session_entry,
                    session_key=session_key,
                    source=source,
                )
            except Exception:
                # Content-free by design: neither input nor a redactor exception crosses logs.
                logger.error(
                    "Refusing compression-exhaustion reset for session %s because the "
                    "accepted input could not be durably preserved.",
                    session_entry.session_id,
                )
                return self._hmwa_refused_preservation_response(response), session_entry

        logger.info(
            "Auto-resetting session %s after compression exhaustion.",
            session_entry.session_id,
        )
        new_entry, failure_notice, failure_entry = (
            await self._hmwa_reset_with_recovery_reconciliation(
                session_key=session_key,
                session_entry=session_entry,
                pending_draft=pending_draft,
                pending_db=pending_db,
            )
        )
        if failure_notice is not None:
            return (response or "") + failure_notice, failure_entry
        if new_entry is not None:
            await self._hmwa_apply_recovery_reset_boundary(
                new_entry=new_entry,
                session_key=session_key,
                source=source,
                pending_draft=pending_draft,
                pending_db=pending_db,
            )
            session_entry = new_entry
        return self._hmwa_recovery_reset_notice(
            response, pending_draft, new_entry
        ), session_entry

    async def _hmwa_pending_turn_redelivery_notice(
        self, event: Any, source: SessionSource, session_entry: Any, session_key: str
    ) -> Optional[str]:
        """Refuse replay and bind a fresh resend before turn preparation/provider entry."""

        event._pending_turn_resend_acknowledgement = None
        if getattr(event, "internal", False):
            return None
        from gateway.pending_turn_draft import (
            MAX_PENDING_TURN_DRAFT_MESSAGE_IDS,
            PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE,
            PENDING_TURN_REDELIVERY_NOTICE,
            PendingTurnDraftError,
            PendingTurnDraftUnauthorized,
            has_pending_turn_draft_tombstone,
            has_unacknowledged_pending_turn_draft,
            pending_turn_principal_fingerprint,
            plan_pending_turn_resend_acknowledgement,
        )
        from gateway.platforms.event import accepted_delivery_ids

        try:
            event_principal = pending_turn_principal_fingerprint(source)
        except PendingTurnDraftUnauthorized:
            # An unauthenticated source cannot bind to or learn another participant's recovery.
            return None

        try:
            db = await self.async_session_store._db_for_key(session_key)
            message_ids = accepted_delivery_ids(event)
            if len(message_ids) > MAX_PENDING_TURN_DRAFT_MESSAGE_IDS:
                raise PendingTurnDraftError(
                    "too many accepted platform message ids for safe recovery"
                )
            if not message_ids:
                has_unacknowledged = await asyncio.to_thread(
                    has_unacknowledged_pending_turn_draft,
                    db,
                    session_key=session_key,
                    principal_fingerprint=event_principal,
                )
                return (
                    PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE
                    if has_unacknowledged
                    else None
                )
            for message_id in message_ids:
                if await asyncio.to_thread(
                    has_pending_turn_draft_tombstone,
                    db,
                    session_key=session_key,
                    principal_fingerprint=event_principal,
                    platform_message_id=message_id,
                ):
                    return PENDING_TURN_REDELIVERY_NOTICE

            is_command = bool(
                callable(getattr(event, "is_command", None)) and event.is_command()
            )
            if is_command or getattr(
                event, "_predates_pending_turn_recovery", False
            ) is True:
                return None

            acknowledgement, replayed = await asyncio.to_thread(
                plan_pending_turn_resend_acknowledgement,
                db,
                session_key=session_key,
                principal_fingerprint=event_principal,
                manual_message_ids=message_ids,
            )
            if replayed:
                return PENDING_TURN_REDELIVERY_NOTICE
            if acknowledgement is not None:
                event._pending_turn_resend_acknowledgement = acknowledgement
            elif await asyncio.to_thread(
                has_unacknowledged_pending_turn_draft,
                db,
                session_key=session_key,
                principal_fingerprint=event_principal,
            ):
                return PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE
        except PendingTurnDraftUnauthorized:
            return None
        except (PendingTurnDraftError, AttributeError, TypeError, OSError):
            return PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE
        except Exception:
            return PENDING_TURN_RECOVERY_UNAVAILABLE_NOTICE
        return None

    async def _hmwa_commit_pending_turn_resend_acknowledgement(
        self, event: Any, source: SessionSource, session_key: str, agent_result: Any
    ) -> Optional[str]:
        """Consume the bound generation only after this exact turn entered a provider."""

        acknowledgement = getattr(
            event, "_pending_turn_resend_acknowledgement", None
        )
        api_calls = (
            agent_result.get("api_calls")
            if isinstance(agent_result, dict)
            else None
        )
        if (
            acknowledgement is None
            or not isinstance(api_calls, int)
            or isinstance(api_calls, bool)
            or api_calls <= 0
        ):
            return None

        from gateway.pending_turn_draft import (
            MAX_PENDING_TURN_DRAFT_MESSAGE_IDS,
            PendingTurnDraftError,
            commit_pending_turn_resend_acknowledgement,
            pending_turn_principal_fingerprint,
        )
        from gateway.platforms.event import accepted_delivery_ids

        try:
            message_ids = accepted_delivery_ids(event)
            if not message_ids or len(message_ids) > MAX_PENDING_TURN_DRAFT_MESSAGE_IDS:
                raise PendingTurnDraftError(
                    "manual resend delivery identity is unavailable"
                )
            principal = pending_turn_principal_fingerprint(source)
            db = await self.async_session_store._db_for_key(session_key)
            await asyncio.to_thread(
                commit_pending_turn_resend_acknowledgement,
                db,
                acknowledgement,
                session_key=session_key,
                principal_fingerprint=principal,
                manual_message_ids=message_ids,
            )
            event._pending_turn_resend_acknowledgement = None
        except (PendingTurnDraftError, AttributeError, TypeError, OSError):
            logger.error(
                "Provider-entered turn could not commit its pending recovery acknowledgement."
            )
            return (
                "⚠️ Your message reached the model, but Hermes could not update its private "
                "recovery state. Ask the operator to inspect state.db before resending it."
            )
        except Exception:
            logger.error(
                "Provider-entered turn could not commit its pending recovery acknowledgement."
            )
            return (
                "⚠️ Your message reached the model, but Hermes could not update its private "
                "recovery state. Ask the operator to inspect state.db before resending it."
            )
        return None

    async def _hmwa_settle_turn_resend_acknowledgement(
        self, event: Any, source: SessionSource, session_key: str, agent_result: Any
    ) -> tuple[Any, bool]:
        """Settle this exact turn before a later queued event may bind its generation."""

        if event is None:
            return agent_result, False
        had_binding = (
            getattr(event, "_pending_turn_resend_acknowledgement", None) is not None
        )
        notice = await self._hmwa_commit_pending_turn_resend_acknowledgement(
            event, source, session_key, agent_result
        )
        binding_remains = bool(
            had_binding
            and getattr(event, "_pending_turn_resend_acknowledgement", None) is not None
        )
        if notice and isinstance(agent_result, dict):
            agent_result = dict(agent_result)
            notices = agent_result.get("_pending_turn_resend_ack_notices", ())
            if not isinstance(notices, (list, tuple)):
                notices = ()
            agent_result["_pending_turn_resend_ack_notices"] = [
                *(item for item in notices if isinstance(item, str) and item),
                notice,
            ]
        return agent_result, binding_remains

    def _hmwa_pop_turn_recovery_metadata(
        self, agent_result: Any
    ) -> tuple[Optional[str], Any, Any, Optional[str]]:
        """Remove private resend notices and terminal provenance from a completed result."""

        notices = []
        terminal_event = None
        terminal_source = None
        terminal_session_key = None
        if isinstance(agent_result, dict):
            queued_notices = agent_result.pop(
                "_pending_turn_resend_ack_notices", ()
            )
            if isinstance(queued_notices, (list, tuple)):
                notices.extend(
                    notice
                    for notice in queued_notices
                    if isinstance(notice, str) and notice
                )
            terminal_event = agent_result.pop("_queued_terminal_event", None)
            terminal_source = agent_result.pop("_queued_terminal_source", None)
            terminal_session_key = agent_result.pop(
                "_queued_terminal_session_key", None
            )
        if not (
            isinstance(terminal_event, MessageEvent)
            and isinstance(terminal_source, SessionSource)
            and isinstance(terminal_session_key, str)
            and terminal_session_key
        ):
            terminal_event = None
            terminal_source = None
            terminal_session_key = None
        return (
            "\n\n".join(notices) or None,
            terminal_event,
            terminal_source,
            terminal_session_key,
        )

    async def _hmwa_reset_completed_turn(
        self,
        *,
        agent_result: dict,
        response: Any,
        session_entry: Any,
        session_key: str,
        source: SessionSource,
        event: MessageEvent,
        terminal_event: Any,
        terminal_source: Any,
        terminal_session_key: Optional[str],
    ) -> tuple[Any, Any]:
        """Apply exhaustion recovery to the terminal turn of a queued chain."""

        reset_event = terminal_event or event
        reset_source = terminal_source or source
        reset_session_key = terminal_session_key or session_key
        reset_session_entry = session_entry
        terminal_route_unavailable = False
        if terminal_event is not None:
            try:
                reset_session_entry = (
                    await self.async_session_store.lookup_by_session_key(
                        reset_session_key
                    )
                )
            except Exception:
                reset_session_entry = None
            terminal_route_unavailable = reset_session_entry is None
        if terminal_route_unavailable and bool(
            agent_result.get("turn_exit_reason") == "context_compression_timeout"
            and agent_result.get("api_calls") == 0
        ):
            logger.error(
                "Refusing queued terminal preflight reset because route %s could not be "
                "reloaded durably.",
                reset_session_key,
            )
            return (response or "") + (
                "\n\n⚠️ Session reset was refused because recovery state is unavailable. "
                "Copy the unprocessed request, start a new session, and resend it manually."
            ), session_entry

        response, reset_session_entry = await self._hmwa_compression_exhaustion_reset(
            agent_result,
            response,
            reset_session_entry,
            reset_session_key,
            reset_source,
            event=reset_event,
        )
        # Queued events normally share the opening route. Publish its fresh entry to the rest of
        # the outer persistence path only when the keys truly match.
        if reset_session_key == session_key and reset_session_entry is not None:
            session_entry = reset_session_entry
        return response, session_entry

    def _hmwa_requeue_pending_followup(
        self,
        *,
        adapter: Any,
        session_key: str,
        source: SessionSource,
        pending_event: Any,
        pending: Optional[str],
        predates_pending_turn_recovery: bool = False,
    ) -> bool:
        """Put a consumed follow-up back at the FIFO head without losing a promoted item."""

        if adapter is None or not session_key:
            return False
        slot = getattr(adapter, "_pending_messages", None)
        if not isinstance(slot, dict):
            return False
        event = pending_event
        if event is None and pending:
            event = MessageEvent(text=str(pending), source=source, message_id=None)
        if event is None:
            return False
        displaced = slot.get(session_key)
        overflow = self._overflow_queue(session_key)
        if displaced is not None and displaced is not event:
            if overflow is None:
                overflow = self._session_state(session_key).conversation.queued_events
            overflow.insert(0, displaced)
        if predates_pending_turn_recovery:
            for accepted_event in (event, *(overflow or ())):
                with suppress(Exception):
                    accepted_event._predates_pending_turn_recovery = True
        slot[session_key] = event
        with suppress(Exception):
            event._gateway_accepted = True
        return True

    async def _hmwa_defer_queued_followup_for_recovery(
        self,
        *,
        adapter: Any,
        session_key: str,
        source: SessionSource,
        pending_event: Any,
        pending: Optional[str],
        result: Any,
    ) -> bool:
        """Restore a follow-up when preflight/recovery must complete before fresh admission."""

        preflight_barrier = bool(
            isinstance(result, dict)
            and result.get("turn_exit_reason") == "context_compression_timeout"
            and result.get("api_calls") == 0
        )
        recovery_barrier = False
        if not preflight_barrier and adapter is not None:
            try:
                probe_event = pending_event or MessageEvent(
                    text=str(pending or ""), source=source, message_id=None
                )
                probe_source = getattr(probe_event, "source", None) or source
                probe_session_key = self._session_key_for_source(probe_source)
                recovery_barrier = (
                    await self._hmwa_pending_turn_redelivery_notice(
                        probe_event,
                        probe_source,
                        None,
                        probe_session_key,
                    )
                    is not None
                )
            except Exception:
                # Fresh outer dispatch retries the same route-independent authorization gate.
                recovery_barrier = True
        if not (preflight_barrier or recovery_barrier):
            return False
        if not self._hmwa_requeue_pending_followup(
            adapter=adapter,
            session_key=session_key,
            source=source,
            pending_event=pending_event,
            pending=pending,
            predates_pending_turn_recovery=preflight_barrier,
        ):
            logger.error(
                "Could not restore deferred follow-up for session %s after recovery barrier.",
                session_key or "?",
            )
        return True

    @staticmethod
    def _hmwa_attach_queued_terminal_provenance(
        merged: Any,
        *,
        pending_event: Any,
        pending: Optional[str],
        next_source: SessionSource,
        next_session_key: str,
        next_inbound_id: Optional[str],
    ) -> Any:
        """Carry terminal queued-turn identity through recursion for the outer recovery path."""

        if not isinstance(merged, dict):
            return merged
        values = {}
        if "queued_terminal_inbound_id" not in merged:
            values["queued_terminal_inbound_id"] = next_inbound_id
        terminal_event = pending_event or MessageEvent(
            text=str(pending or ""), source=next_source, message_id=None
        )
        if "_queued_terminal_event" not in merged:
            values["_queued_terminal_event"] = terminal_event
        if "_queued_terminal_source" not in merged:
            values["_queued_terminal_source"] = next_source
        if "_queued_terminal_session_key" not in merged:
            values["_queued_terminal_session_key"] = next_session_key
        # These process-local keys are consumed below the gateway boundary and never serialized.
        return {**merged, **values} if values else merged
