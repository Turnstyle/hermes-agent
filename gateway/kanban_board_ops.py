"""Board Ops on the existing dispatcher timer, independent of Conductor chat.

Only passive notices are sent. This handler never starts a model turn, invokes
Jev, or interprets card comment prose as an executable request.
"""

from __future__ import annotations

import asyncio

from gateway.kanban_watchers_common import _board_slugs, _to_thread_process_service, logger
from hermes_cli.kanban_board_ops import open_board_ops
from hermes_cli.kanban_board_ops_policy import Refused

SEND_TIMEOUT_SECONDS = 10


def _collect(kb) -> list[dict]:
    deliveries = []
    for board in _board_slugs(kb):
        try:
            with open_board_ops(board) as api:
                api.process_pending()
                deliveries.extend(api.claim_escalations())
        except Refused:
            # Default off and non-owner gateways are ordinary conditions.
            continue
        except Exception:
            logger.exception("Board Ops tick failed for board %s", board)
    return deliveries


def _finish(delivery: dict, success: bool, detail: str) -> None:
    # Called from the dispatcher timer: a failed readback must not raise into the
    # watcher. The claim stays 'delivering' and becomes delivery_unknown (never retried).
    try:
        with open_board_ops(delivery["board"]) as api:
            api.finish_escalation(delivery["correlation"], delivered=success, detail=detail)
    except Exception:
        logger.exception("Board Ops delivery readback failed for %s", delivery.get("correlation"))


def available_adapter(runner, sub: dict):
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import _adapter_for_subscription
    profile = sub["notifier_profile"]
    platform = Platform(sub["platform"])
    adapter = _adapter_for_subscription(runner, platform, sub, profile)
    if adapter is None or not callable(getattr(adapter, "send", None)):
        raise Refused("locally owned recipient adapter unavailable")
    # Stateless API routes cannot prove outbound delivery without an inference
    # self-post. This deterministic path refuses them rather than waking a model.
    if getattr(adapter, "supports_async_delivery", True) is not True:
        raise Refused("recipient has no passive outbound transport")
    return adapter


async def tick(runner, kb) -> None:
    """One bounded pass for the gateway dispatcher timer; never raises into the watcher
    except cancellation. Per board: at most BATCH_LIMIT actions and deliveries, each send
    bounded by SEND_TIMEOUT_SECONDS. Only a gateway whose process profile is the admitted
    executor acts; every other gateway, and boards without Board Ops, are silent no-ops."""
    deliveries = await _to_thread_process_service(_collect, kb)
    for delivery in deliveries:
        success = False
        try:
            sub = delivery["sub"]
            adapter = available_adapter(runner, sub)
            receipt = delivery["receipt"]
            text = (
                f"Board Ops exception {delivery['correlation']} on {delivery['board']}/{receipt['task_id']}. "
                f"Original event timestamp {receipt['original_event_at']}. "
                f"{receipt['facts'].get('reason', 'Owner decision required')}. "
                "Owning Conductor retains accountability. No maker restarted."
            )
            metadata = dict(sub.get("delivery_metadata") or {})
            if sub.get("thread_id"):
                metadata["thread_id"] = sub["thread_id"]
            from gateway.run import _async_profile_runtime_scope
            from hermes_cli.kanban_board_ops_policy import profile_home
            # The async scope loads the recipient's secret scope off the event loop; BaseAdapter.send
            # takes (chat_id, content) positionally, so a ``text=`` keyword can never deliver.
            async with _async_profile_runtime_scope(profile_home(sub["notifier_profile"])):
                result = await asyncio.wait_for(adapter.send(sub["chat_id"], text, metadata=metadata), timeout=SEND_TIMEOUT_SECONDS)
            success = getattr(result, "success", False) is True
            detail = "transport reported success" if success else "transport did not report success"
        except asyncio.CancelledError:
            # Persisted delivery claim becomes unknown after restart; never retry.
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
        await _to_thread_process_service(_finish, delivery, success, detail)
