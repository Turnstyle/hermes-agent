"""Native Bot Chat admission of fleet messages: the only executor of a claimed message.

A queued fleet message runs only as a delivery through the recipient's native authority door
(``tools.bot_live_delivery.deliver_to_live_owner`` -> ``bot_relay.deliver`` ->
``gateway/session_bot.py:_deliver``), under ONE delivery id derived from its message id
(``tools.fleet_message_spool.native_delivery_id``). The door answers a repeated ask of an id with
that id's first admission receipt, and the message's execution claim
(``fleet_message_spool.claim_execution``) keeps a second executor (local spool vs cloud claim,
a reclaimed doc) from even asking.

The cloud doc follows the native admission outcome, never a guess:

    admitted, receipt pending   -> doc stays ``read``; later steps re-read the LOCAL receipt file
    receipt settled             -> doc ``done`` (and the spool payload is retired)
    receipt failed / refused    -> doc ``failed``: terminal, the authority owned the one retry
    refused before anything ran -> execution claim released, doc requeued without an attempt
    uncertain                   -> doc kept ``read`` + ``execution_unverified``, payload kept,
                                   the door is never asked again for it

Only a fully recorded outcome lets the recipient's queue advance (``settle_inflight``).
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from tools import fleet_message_drain as fmd
from tools import fleet_message_spool as spool

logger = logging.getLogger(__name__)

_PENDING_RECEIPTS = frozenset({"queued", "claimed", "canonical"})
# A claim that never recorded the door's answer: younger than this it may still be in flight in
# another drain step; older, nothing can prove whether its dial admitted, so it is uncertain.
_STARTED_GRACE = datetime.timedelta(seconds=120)
# An uncertain claim keeps re-reading its local receipt this long (owner restart recovery may
# still publish it); after that it is the operator's.
_UNCERTAIN_WATCH = datetime.timedelta(hours=24)


@dataclasses.dataclass(frozen=True)
class NativeOutcome:
    """``kind``: pending | settled | failed | refused_before | uncertain."""

    kind: str
    delivery_id: str
    reason: str = ""


def outcome_from_receipt(record: dict, delivery_id: str) -> NativeOutcome:
    """The admission's state from its native receipt (``gateway/session_bot.py:_result``)."""
    status = record.get("status")
    if record.get("refused") is True:
        return NativeOutcome("failed", delivery_id, str(record.get("reason") or "refused"))
    if status in _PENDING_RECEIPTS:
        return NativeOutcome("pending", delivery_id)
    if status == "settled":
        return NativeOutcome("settled", delivery_id)
    if status in ("failed", "cancelled"):
        return NativeOutcome("failed", delivery_id, str(record.get("reason") or status))
    return NativeOutcome("uncertain", delivery_id, f"receipt {status or 'unknown'}")


def find_owner(home: Path | str) -> Optional[dict]:
    """The recipient's ready authority (no lease, no admission), or None when it cannot take work."""
    from tools.bot_live_delivery import find_canonical_live_owner

    try:
        return find_canonical_live_owner(home)
    except Exception as exc:
        logger.debug("fleet drain: authority of %s not ready (%s)", home, exc)
        return None


def admit_native(home: Path | str, message_id: str, text: str, author: Optional[dict],
                 category: str = "result", *, owner: Optional[dict] = None) -> NativeOutcome:
    """Ask the recipient's native door to admit ``message_id`` once. Never raises."""
    from gateway.session_bot import _OWNER_STATE_REFUSALS
    from hermes_cli.gateway_client import GatewayRPCError
    from tools.bot_live_delivery import PreDialError, deliver_to_live_owner

    delivery_id = spool.native_delivery_id(message_id)
    owner = owner or find_owner(home)
    if owner is None:
        return NativeOutcome("refused_before", delivery_id, "runtime_unavailable")
    try:
        record = deliver_to_live_owner(home, owner, text, delivery_id=delivery_id, author=author,
                                       notification_category=category)
    except PreDialError as exc:
        # Died before the RPC reached the socket: nothing was admitted anywhere.
        return NativeOutcome("refused_before", delivery_id, f"runtime_unavailable: {exc}")
    except GatewayRPCError as exc:
        reason = str(exc)
        if reason in _OWNER_STATE_REFUSALS:
            # The owner's state, not the delivery (busy, draining, held by a live writer): the door
            # withdrew its pin. Re-asked later under the SAME id, a commit-after-all is answered
            # from its receipt (or ``unknown_execution``), never run twice.
            return NativeOutcome("refused_before", delivery_id, reason)
        if reason == "unknown_execution":
            return NativeOutcome("uncertain", delivery_id, reason)
        return NativeOutcome("failed", delivery_id, reason)  # refused for good (``refuse_pin``)
    except Exception as exc:
        return NativeOutcome("uncertain", delivery_id, f"outcome unknown after the dial: {exc}")
    return outcome_from_receipt(record, delivery_id)


def _sync_doc(store: Any, home: Path, me: str, record: dict, now: Optional[datetime.datetime]) -> None:
    """Project a recorded execution outcome onto the message's registry doc."""
    if record.get("transport") != "cloud" or record.get("doc_synced") is not False:
        return
    claimed = fmd.claimed_from_row(store.get(record["message_id"]), me)
    if claimed is not None:
        _apply(store, claimed, record, now)
    spool.update_execution(home, record["message_id"], doc_synced=True)


def _apply(store: Any, claimed: fmd.Claimed, record: dict, now: Optional[datetime.datetime]) -> None:
    status, reason = record.get("status"), str(record.get("reason") or "")
    if status == "settled":
        fmd.finish(store, claimed, {"status": "settled"}, max_attempts=1, now=now)
    elif status in ("failed", "refused", "cancelled"):
        fmd.fail(store, claimed, f"native {status}: {reason or status}", now=now)
    elif status == "uncertain":
        fmd.hold_uncertain(store, claimed, reason or "unknown", now=now)


def record_outcome(home: Path, message_id: str, outcome: NativeOutcome, *, transport: str,
                   store: Any = None, claimed: Optional[fmd.Claimed] = None,
                   now: Optional[datetime.datetime] = None) -> str:
    """Write the execution claim first (the local, durable truth), then the doc."""
    if outcome.kind == "refused_before":
        spool.release_execution(home, message_id)
        if claimed is not None:
            fmd.release(store, claimed, now=now, last_error=outcome.reason)
        return "refused"
    status = {"pending": "admitted", "settled": "settled", "failed": "failed",
              "uncertain": "uncertain"}[outcome.kind]
    record = spool.update_execution(home, message_id, status=status, delivery_id=outcome.delivery_id,
                                    reason=outcome.reason,
                                    doc_synced=False if transport == "cloud" and status != "admitted" else True)
    if status == "settled":
        spool.retire_envelope(home, message_id)
    if claimed is not None and status != "admitted":
        _apply(store, claimed, record, now)
        spool.update_execution(home, message_id, doc_synced=True)
    return status


def _record_age(record: dict, now: datetime.datetime) -> datetime.timedelta:
    started = fmd.parse_ts(record.get("updated_at") or record.get("started_at"))
    return now - started if started else datetime.timedelta(0)


def _settle_one(home: Path, record: dict, now: datetime.datetime) -> Optional[bool]:
    """Advance one claim from its local receipt. True: still blocks the queue; False: recorded;
    None: nothing to do for it now (an uncertain claim with no receipt yet)."""
    from tools.bot_live_delivery import read_delivery_result

    status = record.get("status")
    if status == "uncertain" and _record_age(record, now) > _UNCERTAIN_WATCH:
        return None
    receipt = read_delivery_result(home, record["delivery_id"])  # a local file: never an RPC
    if receipt is None:
        if status == "admitted" or (status == "started" and _record_age(record, now) < _STARTED_GRACE):
            return True
        if status == "started":
            spool.update_execution(home, record["message_id"], status="uncertain",
                                   reason="execution claim without a recorded admission",
                                   doc_synced=False if record.get("transport") == "cloud" else True)
            return False
        return None
    outcome = outcome_from_receipt(receipt, record["delivery_id"])
    if outcome.kind == "pending":
        return True
    if outcome.kind == "uncertain" and status == "uncertain":
        return None
    record_outcome(home, record["message_id"], outcome, transport=str(record.get("transport")))
    return False


def settle_inflight(home: Path | str, store_factory: Callable[[], Any], me: str,
                    *, now: Optional[datetime.datetime] = None) -> bool:
    """Re-read every unfinished execution claim of this recipient from its native receipt, then
    sync recorded outcomes onto their docs. True while any admission is still pending or any
    outcome is not recorded on its doc yet: the queue does not advance past it."""
    home = Path(home)
    now = now or fmd.utcnow()
    blocked = False
    for record in spool.inflight(home):
        if record.get("status") in ("admitted", "started", "uncertain"):
            state = _settle_one(home, record, now)
            blocked = blocked or bool(state)
            if state is not False:
                continue
            record = spool.execution(home, record["message_id"]) or record
        if record.get("doc_synced") is False:
            try:
                _sync_doc(store_factory(), home, me, record, now)
            except Exception:
                logger.warning("fleet drain: outcome of %s not yet written to its doc", record["message_id"],
                               exc_info=True)
                blocked = True
    return blocked


def drain_local_spool_once(home: Path, me: str, *, busy: Callable[[], bool], owner: dict,
                           now: Optional[datetime.datetime] = None) -> Optional[str]:
    """Admit one unpublished local envelope without contacting the registry (an outage must not
    stop local delivery). None when there was nothing to admit."""
    from tools.fleet_message_enqueue import build_queued_dm

    for message_id, envelope in spool.iter_envelopes(home):
        if envelope.get("recipient") != me or spool.published(home, message_id) is not None:
            continue
        if spool.execution(home, message_id) is not None:
            continue
        if spool.local_expired(envelope, now):
            if spool.claim_execution(home, message_id, transport="local", fields={}):
                spool.update_execution(home, message_id, status="expired", reason="local spool expired")
            continue
        doc = build_queued_dm(sender=envelope["sender"], recipient=me, body=envelope["body"], message_id=message_id)
        if busy() or not spool.claim_execution(home, message_id, transport="local", fields=doc):
            return None
        text, author, metadata = fmd.render_input(fmd.Claimed(message_id, doc, "local", me))
        outcome = admit_native(home, message_id, text, author, metadata.get("notification_category", "result"),
                               owner=owner)
        return record_outcome(home, message_id, outcome, transport="local")
    return None


def drain_once(home: Path | str, *, config: fmd.DrainConfig, store: Any = None,
               busy: Optional[Callable[[], bool]] = None, now: Optional[datetime.datetime] = None) -> str:
    """One bounded drain step for the recipient at ``home``: settle what is in flight, then admit
    at most one message (local spool first, then one cloud claim). Expiry and sender notices run
    even while the Bot Chat is busy. Returns a tag: pending, busy, idle, held, refused,
    admitted, settled, failed, uncertain."""
    home = Path(home)
    me = fmd.bot_identity(home)
    busy = busy or (lambda: False)
    made: list[Any] = []

    def store_factory():
        if store is not None:
            return store
        if not made:
            made.append(fmd.store_for(config))
        return made[0]

    if settle_inflight(home, store_factory, me, now=now):
        return "pending"
    occupied = busy()
    owner = None if occupied else find_owner(home)
    if owner is not None:
        local = drain_local_spool_once(home, me, busy=busy, owner=owner, now=now)
        if local is not None:
            return local
    cloud = store_factory()
    claimed = fmd.claim_next(cloud, me, limit=config.limit, now=now,
                             queued_timeout_seconds=config.queued_timeout_seconds,
                             allow_claim=owner is not None, receiver_up_since=fmd.receiver_up_since(home))
    if claimed is None:
        return "busy" if occupied else "idle"
    if busy():
        fmd.release(cloud, claimed, now=now)
        return "busy"
    if not fmd.mark_read(cloud, claimed, now=now, profile_home=home):
        return "held"
    text, author, metadata = fmd.render_input(claimed)
    try:
        outcome = admit_native(home, claimed.doc_id, text, author,
                               metadata.get("notification_category", "result"), owner=owner)
    except BaseException:
        spool.update_execution(home, claimed.doc_id, status="uncertain",
                               reason="drain interrupted at the door", doc_synced=False)
        raise
    return record_outcome(home, claimed.doc_id, outcome, transport="cloud", store=cloud, claimed=claimed, now=now)
