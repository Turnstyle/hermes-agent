"""Inbound message event types shared by every gateway platform adapter.

A leaf module: adapters, helpers and the runner import it, so it must not import from
gateway.platforms.*.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from gateway.session import SessionSource


class MessageType(Enum):
    """Types of incoming messages."""
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"  # /command style


class ProcessingOutcome(Enum):
    """Result classification for message-processing lifecycle hooks."""
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    """Incoming message from a platform — the normalized shape all adapters produce."""
    text: str
    message_type: MessageType = MessageType.TEXT
    # Author, mirrored from ``source`` for per-message prompt builders; None for non-IM sources.
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    # None only in isolated unit tests; production always sets it. Typing it Optional
    # exposes ~60 unguarded ``.source.<attr>`` reads, so that is a separate change.
    source: SessionSource = None
    raw_message: Any = None
    message_id: Optional[str] = None
    # Delivery-ledger identity for the final send, when it differs from ``message_id``. A queued
    # (/queue) chain answers the LAST message of the chain, so its final send has to be ledgered
    # under that message's id. Keyed on the opening event's id instead, two chained turns carrying
    # the same text collide on one obligation id and the earlier turn's row is overwritten (a
    # refused first reply then reads as delivered). Reply routing is unaffected: the reply anchor
    # still comes from this event.
    ledger_message_id: Optional[str] = None
    # Platform update id (Telegram ``update_id``): ``/restart`` records it so the new gateway
    # advances past it even if PTB's shutdown ACK times out.
    platform_update_id: Optional[int] = None
    # Media attachments: local file paths (for vision tool access)
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    # Per-attachment text-inlining contract; None = legacy "text/* already inlined into ``text``".
    media_text_inlined: List[Optional[bool]] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None  # Text of the replied-to message (for context injection)
    reply_to_author_id: Optional[str] = None
    reply_to_author_name: Optional[str] = None
    reply_to_is_own_message: bool = False  # True when the user replied to this bot/assistant's message
    # Structured interactive-prompt reply (relay only): {prompt_id, option_id, label?,
    # prompt_message_id?}; routed to the approval/slash-confirm/clarify resolvers BEFORE dispatch.
    prompt_response: Optional[Dict[str, Any]] = None
    # Auto-loaded skill(s) for topic/channel bindings; a single name or ordered list.
    auto_skill: Optional[str | list[str]] = None
    # Per-channel ephemeral system prompt; applied at API call time, never persisted to transcript.
    channel_prompt: Optional[str] = None
    # History-backfilled channel context (missed under require_mention); kept out of ``text`` so
    # run.py's sender-prefix logic sees only the trigger message.
    channel_context: Optional[str] = None
    # Set for synthetic events (e.g. background-process notifications) that must bypass user authorization.
    internal: bool = False
    # Free-form per-event metadata (e.g. ``whatsapp_from_owner=True``); plugins must ``.get()``.
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    # May this event resolve gateway commands / control prompts? Proactive plugin events set False
    # so untrusted payload text stays conversational. Kept last for positional compat.
    allow_gateway_control: bool = True

    # Ordered platform delivery ids absorbed into this process-local event. Busy text debounce and
    # media/albums can collapse several deliveries into one model turn; recovery must tombstone
    # every constituent id, not only whichever id remains in ``message_id``. Keep at most one
    # overflow sentinel beyond the durable record limit so staging fails closed instead of silently
    # forgetting an accepted delivery. This field is never persisted as routing metadata.
    _accepted_delivery_ids: tuple[str, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )

    # Trusted process-local marker: this delivery was already accepted and queued before another
    # turn on the same route produced a recovery generation. It may run after that reset, but must
    # not masquerade as the user's later manual resend acknowledgement.
    _predates_pending_turn_recovery: bool = field(
        default=False, repr=False, compare=False
    )

    # Process-local admission receipt, never routing metadata or execution acknowledgement.
    _gateway_accepted: bool = field(default=False, init=False, repr=False, compare=False)

    def is_command(self) -> bool:
        """Check if this is a command message (e.g., /new, /reset)."""
        return self.allow_gateway_control and (self.text or "").lstrip().startswith("/")

    def get_command(self) -> Optional[str]:
        """Extract command name if this is a command message."""
        if not self.is_command():
            return None
        raw = (self.text or "").lstrip().split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
        # Reject file paths: valid command names never contain /
        return None if "/" in raw else raw

    def get_command_args(self) -> str:
        """Get the arguments after a command."""
        if not self.is_command():
            return self.text
        parts = (self.text or "").lstrip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        # iOS auto-corrects -- to — (em dash) and - to – (en dash)
        return args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")


# Durable pending-turn rows accept at most 64 distinct platform delivery ids. Retaining one extra
# id is intentional: callers can hand the 65-item tuple to the validator and refuse reset rather
# than truncate a replay tombstone.
_MAX_ACCEPTED_DELIVERY_IDS_WITH_OVERFLOW_SENTINEL = 65


def accepted_delivery_ids(event: MessageEvent) -> tuple[str, ...]:
    """Return the ordered, deduplicated delivery ids represented by ``event``."""

    values = list(getattr(event, "_accepted_delivery_ids", ()) or ())
    message_id = getattr(event, "message_id", None)
    if message_id is not None and str(message_id).strip():
        values.append(str(message_id))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value)
        if not normalized.strip() or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= _MAX_ACCEPTED_DELIVERY_IDS_WITH_OVERFLOW_SENTINEL:
            break
    return tuple(result)


def merge_accepted_delivery_ids(target: MessageEvent, incoming: MessageEvent) -> None:
    """Carry every constituent delivery id into a collapsed pending event, fail-closed bounded."""

    combined: list[str] = []
    seen: set[str] = set()
    for event in (target, incoming):
        for value in accepted_delivery_ids(event):
            if value in seen:
                continue
            seen.add(value)
            combined.append(value)
            if len(combined) >= _MAX_ACCEPTED_DELIVERY_IDS_WITH_OVERFLOW_SENTINEL:
                target._accepted_delivery_ids = tuple(combined)
                return
    target._accepted_delivery_ids = tuple(combined)
