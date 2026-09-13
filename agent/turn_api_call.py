"""The provider call for the conversation turn's retry loop: ``nous_rate_limit_guard`` (skip
the attempt while another session's Nous Portal rate limit is active), ``perform_api_call``
(streaming decision, MoA prepared-request handshake, LLM execution middleware wrapper, the
redirect ``_model_request_active`` bracket and the response-vs-redirect crossing check) and
``handle_api_interrupt`` (``InterruptedError`` mid-call). Nothing here imports
``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Dict, Optional

from agent.message_metadata import append_message

logger = logging.getLogger("agent.conversation_loop")


def stop_thinking_spinner(agent: Any, thinking_spinner: Any) -> None:
    """Stop the spinner silently and clear the thinking callback; returns ``None`` so
    callers can rebind ``thinking_spinner = stop_thinking_spinner(agent, thinking_spinner)``."""
    if thinking_spinner:
        thinking_spinner.stop("")
    if agent.thinking_callback:
        agent.thinking_callback("")
    return None


@dataclass
class ApiCallVerdict:
    """``action``: ``"fallthrough"`` (``response`` is ready for verification) or ``"break"``
    (a redirect crossed the response — rebuild armed on ``_retry`` or ``interrupted``)."""

    action: str
    response: Any
    thinking_spinner: Any
    interrupted: Any
    # True only when the terminal provider callback itself returned. Execution
    # middleware can return a synthetic response without ever invoking it.
    provider_call_completed: bool = False
    # Narrower proof used by the retry loop: an explicit recovery generation
    # reached and returned from the physical callback. Invalid/empty responses
    # must not enter ordinary retry/fallback logic and send that intent twice.
    recovered_provider_call_completed: bool = False
    # A recovered resend was definitively not dispatched (middleware/Relay
    # short-circuited before the physical callback) and its claim was returned
    # to the durable pending queue.  The caller must discard the synthetic
    # response and remove the exact resend row from its live transcript.
    recovery_returned_without_dispatch: bool = False
    recovery_resend_id: str = ""


def _pending_recovery_dispatch_ids(agent: Any) -> tuple[str | None, str | None]:
    """Return a real recovery identity, never truthy mock/synthetic attributes."""
    recovery_id = getattr(agent, "_pending_input_recovery_claim_id", None)
    resend_id = getattr(agent, "_pending_input_recovery_resend_id", None)
    return (
        recovery_id if isinstance(recovery_id, str) and recovery_id.strip() else None,
        resend_id if isinstance(resend_id, str) and resend_id.strip() else None,
    )


def _completed_recovery_dispatch_ids(agent: Any) -> tuple[str | None, str | None]:
    """Return the first physical callback's durable-settlement identity."""
    value = getattr(agent, "_pending_input_recovery_provider_completed", None)
    if not (isinstance(value, tuple) and len(value) == 2):
        return None, None
    recovery_id, resend_id = value
    return (
        recovery_id
        if isinstance(recovery_id, str) and recovery_id.strip() else None,
        resend_id if isinstance(resend_id, str) and resend_id.strip() else None,
    )


def _mark_pending_input_recovery_dispatch_started(agent: Any) -> bool:
    recovery_id, resend_id = _pending_recovery_dispatch_ids(agent)
    if not recovery_id or not resend_id:
        return False
    from agent.pending_input_recovery import mark_pending_input_recovery_dispatch_started

    mark_pending_input_recovery_dispatch_started(
        getattr(agent, "_session_db", None), recovery_id, resend_id=resend_id,
        turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None),
    )
    return True


def _ack_pending_input_recovery_after_provider_response(agent: Any) -> bool:
    """Settle a matching recovery only after the terminal provider call returned.

    A persistence failure here must not turn a successful provider response into a
    retried request.  The claimed, dispatch-marked record stays visible as uncertain.
    """
    recovery_id, resend_id = _completed_recovery_dispatch_ids(agent)
    if not recovery_id or not resend_id:
        recovery_id, resend_id = _pending_recovery_dispatch_ids(agent)
    if not recovery_id or not resend_id:
        return False
    try:
        from agent.pending_input_recovery import (
            consume_pending_input_recovery,
            mark_pending_input_recovery_provider_returned,
        )

        # The physical callback may have returned while the first marker write
        # transiently failed. Retry the generation-fenced marker at the durable
        # response boundary before consuming.
        mark_pending_input_recovery_provider_returned(
            getattr(agent, "_session_db", None), recovery_id,
            resend_id=resend_id,
        )
        consume_pending_input_recovery(
            getattr(agent, "_session_db", None), recovery_id,
            resend_id=resend_id, provider_response_received=True,
            require_durable_response=True,
        )
        agent._pending_input_recovery_claim_id = None
        agent._pending_input_recovery_resend_id = None
        agent._pending_input_recovery_provider_completed = None
        return True
    except Exception:
        # The first physical provider callback already returned. Stop carrying
        # this recovery into later tool-loop calls; the tuple above is the
        # retryable local acknowledgement pointer, while the durable record
        # remains claimed/dispatch-started if the process dies.
        if _pending_recovery_dispatch_ids(agent) == (recovery_id, resend_id):
            agent._pending_input_recovery_claim_id = None
            agent._pending_input_recovery_resend_id = None
        logger.error(
            "Provider completed but pending-input acknowledgement failed (session=%s)",
            agent.session_id or "none", exc_info=True,
        )
        return False


def _return_pending_input_recovery_without_dispatch(
    agent: Any, *, recovery_id: str | None = None, resend_id: str | None = None,
) -> str | None:
    """Return one exact live claim after proof that no transport was entered.

    The durable transition withdraws any already-flushed resend row atomically.
    Live attributes are cleared only after that write succeeds; a storage failure
    therefore cannot silently turn a claimed generation into ordinary input.
    """
    current_recovery_id, current_resend_id = _pending_recovery_dispatch_ids(agent)
    recovery_id = recovery_id or current_recovery_id
    resend_id = resend_id or current_resend_id
    if not recovery_id or not resend_id:
        return None
    from agent.pending_input_recovery import (
        pending_input_admission,
        remove_pending_input_admission_from_messages,
        return_pending_input_recovery_without_dispatch,
    )

    # Close/session persistence uses this same lock. Keep the durable transition
    # and all agent-owned projections in one critical section so a close cannot
    # snapshot and reappend the resend between withdrawal and local cleanup.
    persist_lock = getattr(agent, "_session_persist_lock", None)
    with persist_lock if persist_lock is not None else nullcontext():
        return_pending_input_recovery_without_dispatch(
            getattr(agent, "_session_db", None), recovery_id,
            resend_id=resend_id,
            turn_lease_holder=getattr(agent, "_active_session_turn_lease_holder", None),
        )
        session_messages = getattr(agent, "_session_messages", None)
        if isinstance(session_messages, list):
            agent._session_messages, _removed = remove_pending_input_admission_from_messages(
                session_messages, resend_id,
            )
        if pending_input_admission(
            getattr(agent, "_pending_cli_user_message", None)
        )[0] == resend_id:
            agent._pending_cli_user_message = None
        if _pending_recovery_dispatch_ids(agent) == (recovery_id, resend_id):
            agent._pending_input_recovery_claim_id = None
            agent._pending_input_recovery_resend_id = None
        if _completed_recovery_dispatch_ids(agent) == (recovery_id, resend_id):
            agent._pending_input_recovery_provider_completed = None
    return resend_id


def _should_stream(agent: Any) -> bool:
    """Streaming is preferred even without consumers (stale-stream / read-timeout health
    checks); disabled on provider signal, ACP schemes, MoA without a display consumer, or
    Mock clients in tests (SimpleNamespace, not stream iterators)."""
    if getattr(agent, "_disable_streaming", False):
        return False
    _base = str(agent.base_url or "").lower()
    if agent.provider in {"copilot-acp"} or _base.startswith(("acp://", "acp+tcp://")):
        return False
    if not agent._has_stream_consumers():
        if agent.provider == "moa":
            return False
        from unittest.mock import Mock
        if isinstance(getattr(agent, "client", None), Mock):
            return False
    return True


def perform_api_call(
    agent: Any, *, api_kwargs: Any, _original_api_kwargs: Any, _llm_middleware_trace: Any,
    _moa_prepared_request: Any, _retry: Any, thinking_spinner: Any, retry_count: Any,
    api_call_count: Any, api_request_id: Any, effective_task_id: Any, turn_id: Any,
    interrupted: Any,
) -> ApiCallVerdict:
    """Issue the request (see ``_should_stream`` for the streaming decision)."""
    response = None
    provider_call_completed = False
    recovery_returned_without_dispatch = False
    returned_recovery_resend_id = ""
    initial_recovery_id, initial_recovery_resend_id = _pending_recovery_dispatch_ids(agent)
    recovered_dispatch = bool(initial_recovery_id and initial_recovery_resend_id)
    # A Relay execution intercept may invoke ``next_call`` more than once or
    # concurrently.  One admitted recovery generation gets exactly one physical
    # transport entry.  An uncertain first attempt is sticky even when Relay or
    # outer middleware catches it and manufactures a response.
    recovery_dispatch_lock = threading.Lock()
    recovery_dispatch_finished = threading.Event()
    recovery_callback_reserved = False
    recovery_dispatch_gate_closed = False
    recovery_physical_dispatch_started = False
    recovery_dispatch_uncertain = None
    recovery_predispatch_error = None
    recovery_predispatch_returned = False
    recovery_dispatch_result = None
    recovery_callback_control_error = None

    def _verdict(action: str) -> ApiCallVerdict:
        return ApiCallVerdict(
            action=action, response=response, thinking_spinner=thinking_spinner,
            interrupted=interrupted, provider_call_completed=provider_call_completed,
            recovered_provider_call_completed=bool(
                recovered_dispatch and provider_call_completed
            ),
            recovery_returned_without_dispatch=recovery_returned_without_dispatch,
            recovery_resend_id=returned_recovery_resend_id,
        )

    def _stop_spinner():
        nonlocal thinking_spinner
        thinking_spinner = stop_thinking_spinner(agent, thinking_spinner)

    _use_streaming = _should_stream(agent)

    def _perform_api_call(next_api_kwargs):
        nonlocal provider_call_completed
        if agent.api_mode == "codex_responses":
            next_api_kwargs = agent._get_transport().preflight_kwargs(
                next_api_kwargs, allow_stream=False, is_github_responses=agent._is_copilot_url(),
                sanitize_harmony_tokens=agent._is_codex_backend(),
            )
        def _physical_provider_call(callback):
            """Fence recovery at the actual transport callback, not a wrapper.

            Both execution middleware and NeMo Relay may return a managed result
            without invoking their callback.  Such a short-circuit is not
            provider-entry evidence and must leave the recovery claimed.
            """
            nonlocal provider_call_completed, recovery_callback_reserved
            nonlocal recovery_dispatch_gate_closed
            nonlocal recovery_physical_dispatch_started, recovery_dispatch_uncertain
            nonlocal recovery_predispatch_error, recovery_predispatch_returned
            nonlocal recovery_dispatch_result, recovery_callback_control_error
            if recovered_dispatch:
                with recovery_dispatch_lock:
                    if recovery_dispatch_gate_closed and not recovery_callback_reserved:
                        # The execution wrapper already returned before this
                        # detached callback was admitted. Its claim has been (or
                        # is being) returned to pending, so this late callback
                        # must never mark or enter transport.
                        raise RuntimeError(
                            "recovered provider callback arrived after execution boundary"
                        )
                    first_callback = not recovery_callback_reserved
                    if first_callback:
                        recovery_callback_reserved = True
                        try:
                            recovery_dispatch_started = (
                                _mark_pending_input_recovery_dispatch_started(agent)
                            )
                        except BaseException as exc:
                            returned = False
                            settlement_error = None
                            try:
                                returned = bool(
                                    _return_pending_input_recovery_without_dispatch(
                                        agent,
                                        recovery_id=initial_recovery_id,
                                        resend_id=initial_recovery_resend_id,
                                    )
                                )
                            except BaseException as return_exc:
                                settlement_error = return_exc
                                if isinstance(return_exc, Exception):
                                    logger.error(
                                        "Could not return recovery after dispatch-marker "
                                        "failure (session=%s)",
                                        agent.session_id or "none", exc_info=True,
                                    )
                            finally:
                                terminal_error = settlement_error or exc
                                recovery_predispatch_error = terminal_error
                                recovery_predispatch_returned = returned
                                if not isinstance(terminal_error, Exception):
                                    recovery_callback_control_error = terminal_error
                                # Reservation is now terminal even if the
                                # rollback persistence itself was cancelled.
                                recovery_dispatch_finished.set()
                            raise terminal_error
                        recovery_physical_dispatch_started = True
                if not first_callback:
                    # A concurrent Relay retry waits for the first exact
                    # generation rather than entering transport. Sequential
                    # retries observe the same result/error immediately.
                    recovery_dispatch_finished.wait()
                    with recovery_dispatch_lock:
                        if recovery_dispatch_uncertain is not None:
                            raise recovery_dispatch_uncertain
                        if recovery_predispatch_error is not None:
                            raise recovery_predispatch_error
                        return recovery_dispatch_result
            else:
                recovery_dispatch_started = False
            try:
                result = callback()
            except BaseException as exc:
                if not recovery_dispatch_started:
                    if recovered_dispatch:
                        recovery_dispatch_finished.set()
                    raise
                from agent.pending_input_recovery import (
                    PendingInputDispatchUncertainError,
                    quarantine_pending_input_recovery_dispatch,
                )

                recovery_id, resend_id = (
                    initial_recovery_id, initial_recovery_resend_id,
                )
                quarantine_error = None
                try:
                    quarantine_pending_input_recovery_dispatch(
                        getattr(agent, "_session_db", None), recovery_id,
                        resend_id=resend_id,
                        turn_lease_holder=getattr(
                            agent, "_active_session_turn_lease_holder", None
                        ),
                    )
                except BaseException as transition_exc:
                    quarantine_error = transition_exc
                    # The dispatch-start marker is already durable. A later turn's
                    # reconciliation pass quarantines the exact row before request build.
                    if isinstance(transition_exc, Exception):
                        logger.error(
                            "Could not immediately quarantine uncertain recovery dispatch "
                            "(session=%s)", agent.session_id or "none", exc_info=True,
                        )
                uncertain = PendingInputDispatchUncertainError(
                    exc, resend_id=str(resend_id or "")
                )
                if recovered_dispatch:
                    with recovery_dispatch_lock:
                        recovery_dispatch_uncertain = uncertain
                        if (
                            quarantine_error is not None
                            and not isinstance(quarantine_error, Exception)
                        ):
                            recovery_callback_control_error = quarantine_error
                    recovery_dispatch_finished.set()
                    # Dispatch-start is already durable and is now the sole
                    # authority for this ambiguous generation.  Do not carry
                    # its live attribution into a later ordinary turn: that
                    # would exempt the orphaned resend from reconciliation and
                    # could tag/send unrelated input as this recovery.
                    if _pending_recovery_dispatch_ids(agent) == (
                        recovery_id, resend_id,
                    ):
                        agent._pending_input_recovery_claim_id = None
                        agent._pending_input_recovery_resend_id = None
                if (
                    quarantine_error is not None
                    and not isinstance(quarantine_error, Exception)
                ):
                    raise quarantine_error
                raise uncertain from exc
            provider_call_completed = True
            if recovery_dispatch_started:
                recovery_id, resend_id = (
                    initial_recovery_id, initial_recovery_resend_id,
                )
                marker_error = None
                try:
                    from agent.pending_input_recovery import (
                        mark_pending_input_recovery_provider_returned,
                    )

                    mark_pending_input_recovery_provider_returned(
                        getattr(agent, "_session_db", None), recovery_id,
                        resend_id=resend_id,
                    )
                except BaseException as mark_exc:
                    marker_error = mark_exc
                    # Preserve the actual provider result. The immediate consume
                    # below may still settle it; if both writes fail, restart
                    # remains explicitly uncertain instead of inferring from an
                    # assistant-shaped middleware result.
                    if isinstance(mark_exc, Exception):
                        logger.error(
                            "Could not persist recovered-input provider return "
                            "(session=%s)", agent.session_id or "none", exc_info=True,
                        )
                finally:
                    # The physical response is already known. Publish every
                    # in-memory terminal field and release concurrent/detached
                    # waiters even when marker persistence is cancelled.
                    agent._pending_input_recovery_provider_completed = (
                        recovery_id, resend_id,
                    )
                    if _pending_recovery_dispatch_ids(agent) == (
                        recovery_id, resend_id,
                    ):
                        agent._pending_input_recovery_claim_id = None
                        agent._pending_input_recovery_resend_id = None
                    with recovery_dispatch_lock:
                        recovery_dispatch_result = result
                        if marker_error is not None and not isinstance(
                            marker_error, Exception
                        ):
                            recovery_callback_control_error = marker_error
                    recovery_dispatch_finished.set()
                if marker_error is not None and not isinstance(marker_error, Exception):
                    raise marker_error
            elif recovered_dispatch:
                with recovery_dispatch_lock:
                    recovery_dispatch_result = result
                recovery_dispatch_finished.set()
            return result

        if _use_streaming:
            return _physical_provider_call(
                lambda: agent._interruptible_streaming_api_call(
                    next_api_kwargs, on_first_delta=_stop_spinner
                )
            )

        from agent import relay_llm

        return relay_llm.execute(
            next_api_kwargs,
            lambda relay_kwargs: _physical_provider_call(
                lambda: agent._interruptible_api_call(relay_kwargs)
            ),
            session_id=str(agent.session_id or ""),
            name=str(agent.provider or "provider"),
            model_name=str(agent.model or ""),
            metadata={
                "api_mode": agent.api_mode,
                "api_request_id": api_request_id,
                "call_role": (
                    "delegated"
                    if getattr(agent, "is_subagent", False)
                    else "fallback"
                    if int(getattr(agent, "_fallback_index", 0) or 0) > 0
                    else "primary"
                ),
                "retry_count": retry_count,
            },
            defer_logical_completion=True,
        )

    from hermes_cli.middleware import run_llm_execution_middleware

    # The ``_model_request_active`` bracket is taken under the redirect lock when one exists,
    # so redirect() can't observe a half-toggled flag.
    _model_request_active = getattr(agent, "_model_request_active", None)
    _redirect_lock = getattr(agent, "_pending_redirect_lock", None)
    _bracket = nullcontext() if _redirect_lock is None else _redirect_lock
    with _bracket:
        if _model_request_active is not None:
            _model_request_active.set()
    execution_error = None
    detached_callback = False
    try:
        response = run_llm_execution_middleware(
            api_kwargs, _perform_api_call, original_request=_original_api_kwargs,
            task_id=effective_task_id, turn_id=turn_id, api_request_id=api_request_id,
            session_id=agent.session_id or "", platform=agent.platform or "", model=agent.model,
            provider=agent.provider, base_url=agent.base_url, api_mode=agent.api_mode,
            api_call_count=api_call_count, middleware_trace=list(_llm_middleware_trace),
        )
    except BaseException as exc:
        # Delay propagation until the recovered-dispatch state machine has had
        # a chance to turn a proven pre-provider failure back into an inert
        # pending record. Otherwise the generic API retry loop could send the
        # same live row without its claim on a later attempt.
        execution_error = exc
    finally:
        with _bracket:
            if _model_request_active is not None:
                _model_request_active.clear()
            _redirect_crossed_response = (
                bool(agent._pending_redirect) if _redirect_lock is not None
                else agent._has_pending_redirect()
            )
    if recovered_dispatch:
        # Close admission before inspecting callback state. A callback already
        # reserved under the same lock owns this generation; wait for its
        # transport's own bounded completion. A callback that has not reserved
        # yet is rejected at entry, making return-to-pending linearizable.
        with recovery_dispatch_lock:
            recovery_dispatch_gate_closed = True
            callback_reserved = recovery_callback_reserved
            callback_finished = recovery_dispatch_finished.is_set()
        if callback_reserved and not callback_finished:
            detached_callback = True
            recovery_dispatch_finished.wait()
        with recovery_dispatch_lock:
            uncertain = recovery_dispatch_uncertain
            predispatch_error = recovery_predispatch_error
            predispatch_returned = recovery_predispatch_returned
            physical_dispatch_entered = recovery_physical_dispatch_started
            callback_control_error = recovery_callback_control_error
        if callback_control_error is not None:
            # KeyboardInterrupt/SystemExit-style control flow is observable only
            # after the exact reserved generation has published terminal state.
            raise callback_control_error
        if uncertain is not None:
            # Relay/middleware may catch the callback exception and return a
            # valid-looking replacement.  The physical outcome remains
            # uncertain and must win over that synthetic result.
            raise uncertain
        if predispatch_error is not None:
            if not predispatch_returned:
                raise predispatch_error
            recovery_returned_without_dispatch = True
            returned_recovery_resend_id = str(initial_recovery_resend_id or "")
        if not physical_dispatch_entered:
            if not recovery_returned_without_dispatch:
                returned = _return_pending_input_recovery_without_dispatch(
                    agent,
                    recovery_id=initial_recovery_id,
                    resend_id=initial_recovery_resend_id,
                )
                recovery_returned_without_dispatch = bool(returned)
                returned_recovery_resend_id = str(returned or "")
        if (
            provider_call_completed
            and execution_error is not None
            and isinstance(execution_error, Exception)
        ):
            # Relay or execution middleware may raise while post-processing a
            # physical response that already returned. Retrying the ordinary
            # API loop would send the accepted recovery twice. Use the exact
            # cached physical result and keep the durable provider-return marker
            # for settlement after its response projection is persisted.
            response = recovery_dispatch_result
            execution_error = None
        elif provider_call_completed and detached_callback:
            # A premature wrapper return cannot substitute a synthetic value
            # for the physical response it detached.
            response = recovery_dispatch_result
    if execution_error is not None:
        # Cancellation/control-flow BaseExceptions must remain observable after
        # the gate and callback state are safely settled. Ordinary pre-provider
        # Exceptions may be replaced by the explicit returned-to-pending
        # verdict so the retry loop cannot re-admit the same row.
        if not isinstance(execution_error, Exception) or not recovery_returned_without_dispatch:
            raise execution_error
    if _redirect_crossed_response and not (
        recovered_dispatch and provider_call_completed
    ):
        # Response and redirect can cross threads: discard the now-stale
        # response and rebuild from the correction rather than lose it.
        thinking_spinner = stop_thinking_spinner(agent, thinking_spinner)
        if agent.clear_interrupt(preserve_redirect=True):
            _retry.restart_with_redirected_messages = True
        else:
            interrupted = True
        return _verdict("break")
    return _verdict("fallthrough")


@dataclass
class ApiInterruptVerdict:
    """Always ``action == "break"`` (leave the retry loop): either a redirect restart was
    armed on ``_retry`` or the turn is ``interrupted`` with ``final_response`` set."""

    action: str
    thinking_spinner: Any
    interrupted: Any
    final_response: Any


def handle_api_interrupt(
    agent: Any, *, _retry: Any, thinking_spinner: Any, messages: Any, conversation_history: Any,
    api_start_time: Any, interrupted: Any, final_response: Any,
) -> ApiInterruptVerdict:
    """``InterruptedError`` during the provider call: a pending redirect keeps its correction
    queued for the outer-loop rebuild; otherwise keep any streamed partial text so the next
    turn has a record of the half-finished reply."""
    from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX

    thinking_spinner = stop_thinking_spinner(agent, thinking_spinner)
    # redirect() cancelled only this request: keep the correction queued, clear the
    # cancellation bit, let the outer loop rebuild. Never materialize incomplete
    # signed/encrypted reasoning items.
    if agent._has_pending_redirect() and agent.clear_interrupt(preserve_redirect=True):
        _retry.restart_with_redirected_messages = True
        return ApiInterruptVerdict("break", thinking_spinner, interrupted, final_response)
    api_elapsed = time.time() - api_start_time
    agent._vprint(f"{agent.log_prefix}⚡ Interrupted during API call.", force=True)
    interrupted = True
    _partial = agent._strip_think_blocks(
        getattr(agent, "_current_streamed_assistant_text", "") or ""
    ).strip()
    if _partial:
        append_message(messages, {"role": "assistant", "content": _partial})
        final_response = _partial
    else:
        final_response = f"{INTERRUPT_WAITING_FOR_MODEL_PREFIX}{api_elapsed:.1f}s elapsed)."
    agent._persist_session(messages, conversation_history)
    return ApiInterruptVerdict("break", thinking_spinner, interrupted, final_response)


@dataclass
class NousRateGuardVerdict:
    """``action``: ``"fallthrough"`` (no active limit — make the call), ``"break"``
    (fallback armed on ``_retry``) or ``"return"`` (``result``: no fallback available)."""

    action: str
    active_system_prompt: Any
    retry_count: Any
    compression_attempts: Any
    result: Optional[Dict[str, Any]] = None


def nous_rate_limit_guard(
    agent: Any, *, _retry: Any, api_messages: Any, messages: Any, conversation_history: Any,
    active_system_prompt: Any, retry_count: Any, compression_attempts: Any, api_call_count: Any,
) -> NousRateGuardVerdict:
    """Skip the call if another session recorded a Nous Portal rate limit: every attempt (incl.
    SDK retries) counts against RPH. Never lets the guard itself break the agent loop."""
    from agent.conversation_loop import _arm_fallback_restart

    def _verdict(action: str, result: Optional[Dict[str, Any]] = None) -> NousRateGuardVerdict:
        return NousRateGuardVerdict(
            action=action, active_system_prompt=active_system_prompt, retry_count=retry_count,
            compression_attempts=compression_attempts, result=result,
        )

    if agent.provider == "nous":
        try:
            from agent.nous_rate_guard import (
                nous_rate_limit_remaining, format_remaining as _fmt_nous_remaining
            )
            _nous_remaining = nous_rate_limit_remaining()
            if _nous_remaining is not None and _nous_remaining > 0:
                _nous_msg = (
                    f"Nous Portal rate limit active — resets in {_fmt_nous_remaining(_nous_remaining)}."
                )
                agent._buffer_vprint(f"⏳ {_nous_msg} Trying fallback...")
                agent._buffer_status(f"⏳ {_nous_msg}")
                if agent._try_activate_fallback():
                    active_system_prompt = _arm_fallback_restart(
                        agent, api_messages, active_system_prompt, _retry)
                    retry_count = 0
                    compression_attempts = 0
                    return _verdict("break")
                # No fallback — surface the buffered rate-limit context that led here.
                agent._flush_status_buffer()
                agent._persist_session(messages, conversation_history)
                return _verdict("return", {
                    "final_response": (
                        f"⏳ {_nous_msg}\n\n"
                        "No fallback provider available. Try again after the reset, or add a "
                        "fallback provider in config.yaml."
                    ),
                    "messages": messages,
                    "api_calls": api_call_count,
                    "completed": False,
                    "failed": True,
                    "error": _nous_msg,
                })
        except Exception:
            pass  # Never let rate guard break the agent loop
    return _verdict("fallthrough")
