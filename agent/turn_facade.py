"""``AIAgent.run_conversation`` / ``chat`` façade.

Turn admission around ``conversation_loop.run_conversation``: durable cross-process session turn lease +
refresher thread and liveness watchdog (``agent.turn_facade_lease``), relay/accounting/portal scopes, and
balanced start/finish marks. Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s
MRO unchanged.
"""
import logging
import uuid
from contextlib import suppress
from typing import Any, Dict, List, Optional

from agent.lazy_forward import forward as _forward

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("run_agent")


def _settle_undispatched_pending_input(agent: Any, result: Any) -> Any:
    """Return an exact recovery claim when a turn exits before provider entry.

    CLI and ACP claim before this facade acquires the durable session turn lease.
    Every normal early result (lease admission, preflight, rate-limit guard, or
    middleware short-circuit) crosses this boundary.  The durable helper refuses
    started/uncertain dispatches, so invoking it here cannot turn ambiguity into
    a replayable draft.
    """
    recovery_id = getattr(agent, "_pending_input_recovery_claim_id", None)
    resend_id = getattr(agent, "_pending_input_recovery_resend_id", None)
    if not (
        isinstance(recovery_id, str) and recovery_id.strip()
        and isinstance(resend_id, str) and resend_id.strip()
    ):
        return result
    try:
        from agent.turn_api_call import _return_pending_input_recovery_without_dispatch

        returned_resend_id = _return_pending_input_recovery_without_dispatch(
            agent, recovery_id=recovery_id, resend_id=resend_id,
        )
    except Exception:
        # A timeout path may already have atomically replaced this recovery with
        # a successor. Clear only stale live pointers to an exact consumed
        # predecessor; otherwise keep the claim intact and fail closed.
        try:
            from agent.pending_input_recovery import get_pending_input_recovery

            durable = get_pending_input_recovery(
                getattr(agent, "_session_db", None), recovery_id,
            )
        except Exception:
            durable = None
        if (
            isinstance(durable, dict)
            and durable.get("status") == "consumed"
            and durable.get("resend_id") == resend_id
        ):
            if getattr(agent, "_pending_input_recovery_claim_id", None) == recovery_id:
                agent._pending_input_recovery_claim_id = None
            if getattr(agent, "_pending_input_recovery_resend_id", None) == resend_id:
                agent._pending_input_recovery_resend_id = None
        return result
    if not returned_resend_id:
        return result

    from agent.pending_input_recovery import (
        pending_input_admission,
        remove_pending_input_admission_from_messages,
    )

    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        result["messages"], _removed = remove_pending_input_admission_from_messages(
            result["messages"], returned_resend_id,
        )
        result.update(
            recovery_persisted=True,
            recovery_returned_without_dispatch=True,
            provider_call_sent=False,
        )
    session_messages = getattr(agent, "_session_messages", None)
    if isinstance(session_messages, list):
        agent._session_messages, _removed_session = (
            remove_pending_input_admission_from_messages(
                session_messages, returned_resend_id,
            )
        )
    if pending_input_admission(
        getattr(agent, "_pending_cli_user_message", None)
    )[0] == returned_resend_id:
        agent._pending_cli_user_message = None
    return result


class TurnFacadeMixin:
    """run_conversation()/chat() (see module docstring)."""

    def run_conversation(
        self, user_message: Any, system_message: str=None,
        conversation_history: List[Dict[str, Any]]=None, task_id: str=None,
        stream_callback: Optional[callable]=None, persist_user_message: Optional[Any]=None,
        persist_user_timestamp: Optional[float]=None, persist_user_display_kind: Optional[str]=None,
        persist_user_display_metadata: Optional[Dict[str, Any]]=None,
        persist_user_platform_id: Optional[str]=None, moa_config: Optional[dict[str, Any]]=None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.conversation_loop.run_conversation``."""
        # A review shares this session_id for cache parity: fence review startup or interrupt
        # an admitted request and await its exit before opening live-turn instrumentation.
        # Foreground priority is retained if the review does not acknowledge within the bounded deadline
        # (#84423).
        from agent.background_review import cancel_background_review_for_live_turn

        cancel_background_review_for_live_turn(self)

        from agent import relay_runtime
        from agent.aux_accounting import reset_accounting_context, set_accounting_context
        from agent.auxiliary_client import scoped_runtime_main
        from agent.conversation_loop import run_conversation
        from agent.portal_tags import (
            reset_affinity_scope, reset_conversation_context, set_affinity_scope,
            set_conversation_context,
        )
        from agent.prompt_cache_scope import declared_conversation_scope_safe
        from agent.review_idle_queue import QUEUE as _review_queue
        from agent.subagent_lifecycle import bind_subagent_parent
        from agent.turn_facade_lease import admit_durable_turn_lease
        from hermes_cli.observability.relay_shared_metrics import finish_task_run, start_task_run

        effective_task_id = task_id or str(uuid.uuid4())
        session_id = str(getattr(self, "session_id", None) or "")
        task_context = {
            "session_id": session_id,
            "task_id": effective_task_id,
            "platform": getattr(self, "platform", None) or "",
        }
        relay_turn_id = f"{session_id or 'session'}:{effective_task_id}:{uuid.uuid4().hex[:8]}"
        self._relay_pending_turn_id = relay_turn_id
        relay_parent_session_id = (
            str(getattr(self, "_parent_session_id", None) or "")
            if task_context["platform"] == "subagent"
            else ""
        )
        relay_lease = relay_turn = lease = None
        # Scope tokens start None: early returns leave the try before the set_*() calls and
        # the finally resets each one unconditionally.
        token = affinity_token = acct_token = None
        task_started = task_finished = False
        relay_outcome = "failed"

        try:
            # First statement of the try so the finally's note_turn_finished balances every exit.
            _review_queue.note_turn_started()
            admission = admit_durable_turn_lease(
                self, session_id=session_id, relay_turn_id=relay_turn_id, task_context=task_context,
                conversation_history=conversation_history,
            )
            if admission.early_result is not None:
                relay_outcome = (
                    "cancelled" if admission.early_result.get("interrupted") else "timed_out"
                )
                return _settle_undispatched_pending_input(self, admission.early_result)
            lease = admission.lease
            conversation_history = admission.conversation_history

            relay_lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
                profile_key=relay_runtime.current_profile_key(),
                session_id=task_context["session_id"], platform=task_context["platform"],
                parent_session_id=relay_parent_session_id,
                model=str(getattr(self, "model", None) or ""),
            )
            relay_turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
                relay_lease, turn_id=relay_turn_id, task_id=effective_task_id
            )
            # Minimal relay-runtime shims may lack the opt-out flag: default enabled.
            if getattr(relay_turn, "relay_enabled", True):
                start_task_run(
                    **task_context,
                    parent_session_id=getattr(self, "_parent_session_id", None) or "",
                )
                task_started = True
            # Ambient Nous Portal tagging: every LLM call in this turn (loop, compression,
            # vision, MoA, review forks) inherits `conversation=<root>`; host-declared
            # affinity scope falls back to it; accounting handles route aux usage to the session.
            token = set_conversation_context(self._conversation_root_id())
            affinity_token = set_affinity_scope(declared_conversation_scope_safe(self))
            # Publish the session accounting handles the same way so auxiliary calls record their token
            # usage into session_model_usage (task dimension) — the fix for aux spend being invisible in
            # analytics (issue #23270).
            acct_token = set_accounting_context(
                getattr(self, "_session_db", None), getattr(self, "session_id", None)
            )

            # Keep the ContextVar scope local (agent tokens may be observed from another thread).
            with bind_subagent_parent(self), scoped_runtime_main({}):
                try:
                    if lease is not None:
                        lease.start()
                    result = run_conversation(
                        self, user_message, system_message, conversation_history, effective_task_id,
                        stream_callback, persist_user_message,
                        persist_user_timestamp=persist_user_timestamp,
                        persist_user_display_kind=persist_user_display_kind,
                        persist_user_display_metadata=persist_user_display_metadata,
                        persist_user_platform_id=persist_user_platform_id, moa_config=moa_config,
                    )
                finally:
                    # Post-loop relay/task finalization must not receive a late refresh interrupt;
                    # the interrupt clear itself waits for the thread join in the outer finally.
                    if lease is not None:
                        lease.stop_refresher()
            terminal = result if isinstance(result, dict) else {}
            relay_outcome = (
                "cancelled" if terminal.get("interrupted") is True
                else "failed" if terminal.get("failed") is True
                else "success"
            )
            relay_runtime.SESSION_COORDINATOR.finish_logical_calls(relay_turn, outcome=relay_outcome)
            if task_started:
                task_finished = True
                finish_task_run(**task_context, result=result)
            return _settle_undispatched_pending_input(self, result)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, InterruptedError)) or (
                type(exc).__name__ == "CancelledError"
            ):
                relay_outcome = "cancelled"
            elif isinstance(exc, TimeoutError):
                relay_outcome = "timed_out"
            if relay_turn is not None:
                relay_runtime.SESSION_COORDINATOR.finish_logical_calls(
                    relay_turn, outcome=relay_outcome
                )
            if task_started and not task_finished:
                task_finished = True
                finish_task_run(**task_context, error=exc)
            # Preserve the exact claim identity for the owning surface's
            # exception handler.  CLI/ACP each have additional staged history
            # outside the agent object; clearing the IDs here would prevent
            # those handlers from withdrawing that surface-local copy. Normal
            # early results are settled at the shared return boundary above.
            raise
        finally:
            try:
                if relay_turn is not None:
                    relay_runtime.SESSION_COORDINATOR.end_turn(relay_turn, outcome=relay_outcome)
            finally:
                try:
                    if relay_lease is not None:
                        relay_runtime.SESSION_COORDINATOR.release_conversation(relay_lease)
                finally:
                    if lease is not None:
                        lease.stop_refresher()
                        lease.join_threads()
                        lease.clear_interrupt()  # refresher interrupt between stop and join; AFTER join
                        lease.release()
                    # Always clear mid-turn labels on exit — including interrupted early returns
                    # that skip finalize_turn. Keep ts.
                    with suppress(Exception):
                        self._reset_activity_labels_after_turn()
                    if getattr(self, "_relay_pending_turn_id", None) == relay_turn_id:
                        self._relay_pending_turn_id = None
                    if acct_token is not None:
                        reset_accounting_context(acct_token)
                    if token is not None:
                        reset_conversation_context(token)
                    if affinity_token is not None:
                        reset_affinity_scope(affinity_token)
                    # Balance note_turn_started so the idle queue's live-turn count cannot leak.
                    with suppress(Exception):
                        _review_queue.note_turn_finished()

    def chat(self, message: str, stream_callback: Optional[callable] = None) -> str:
        """Final response string of one turn; ``stream_callback`` receives each text delta."""
        return self.run_conversation(message, stream_callback=stream_callback)["final_response"]

    _run_codex_app_server_turn = _forward("agent.codex_runtime", "run_codex_app_server_turn")
