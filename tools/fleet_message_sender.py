"""Sender side of the fleet DM queue (``message_agent``).

A local teammate's native Bot Chat door may give a DEFINITE no-send (no authority up, a dial
that died before the RPC, another session writer holding the chat). Instead of only "NOT sent,
resend later", a recipient that opted into the fleet queue gets the message durably spooled
(``tools.fleet_message_spool``: local envelope first, then the registry) and its own gateway
admits it through that same native door when it is free. An UNCERTAIN door outcome is never
queued: a second copy could run the message twice.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def sender_handle(author: Optional[dict]) -> str:
    """The fleet handle of a ``message_agent`` author: its ``bot:<profile>`` id, else a safe name."""
    from tools.fleet_message_enqueue import fleet_handle

    author_id = str((author or {}).get("id") or "") if isinstance(author, dict) else ""
    if author_id.startswith("bot:"):
        sender = author_id.removeprefix("bot:").split(":", 1)[0]
        if sender:
            return fleet_handle(sender, fallback="sender")
    if isinstance(author, dict):
        return fleet_handle(author.get("name") or author.get("id"), fallback="sender")
    return "sender"


def queue_after_no_send(refusal_json: str, *, recipient_home: Path | str, body: str,
                        author: Optional[dict], label: str) -> str:
    """The sender's answer after the native door refused with nothing admitted: a durable queued
    ack when the recipient drains its fleet queue, else the refusal plus its typed queue admission."""
    from tools.fleet_message_drain import bot_identity, inspect_recipient_queue
    from tools.fleet_message_enqueue import queued_ack
    from tools.fleet_message_spool import spool_busy_dm

    refusal = json.loads(refusal_json)
    if refusal.get("status") == "ambiguous" or not refusal.get("reason"):
        return refusal_json  # outcome unknown: a queued copy could run twice
    home = Path(recipient_home)
    recipient = bot_identity(home)
    admission = inspect_recipient_queue(recipient, profile_home=home)
    if not admission.enabled:
        return json.dumps({**refusal, "queue_admission": admission.reason, "reply_relayed": False})
    try:
        spooled = spool_busy_dm(home, sender=sender_handle(author), recipient=recipient, body=body)
    except Exception as exc:
        logger.warning("message_agent: could not queue the refused DM for %s (%s)", label, type(exc).__name__)
        return json.dumps({**refusal, "queue_admission": "enabled", "queue_error": type(exc).__name__,
                           "reply_relayed": False})
    message_id = spooled["message_id"]
    return json.dumps({
        "status": "queued", "message_id": message_id, "to": label, "reason": "target_busy",
        "refusal_reason": refusal["reason"], "queue_admission": "enabled",
        "published": spooled["published"], "reply_relayed": False, "reply": queued_ack(message_id),
        "detail": (f"{label} could not take a Bot Chat turn now ({refusal['reason']}). The message is durably "
                   f"queued ({message_id}) and becomes their next Bot Chat turn when they are free. "
                   "Do NOT resend. Any reply arrives later as a new message from them."),
    })


def worker_one_way(agent: Any, *, me: str, home: str, raw_target: str, root: Path, roster_homes: dict,
                   content: str, body: str) -> str:
    """A kanban worker's one-way message: queued for the recipient's drain (a worker has no Bot Chat
    to receive a reply in), its id commented on the worker's own card. A roster recipient's copy is
    spooled locally first, so a registry outage cannot lose it."""
    from tools.bot_mode_dm import _resolve_local_name
    from tools.bot_mode_probe import _handle
    from tools.fleet_message_drain import recipient_drain_enabled
    from tools.fleet_message_enqueue import enqueue_busy_dm, queued_ack
    from tools.fleet_message_spool import spool_busy_dm

    recipient = _resolve_local_name(raw_target, list(roster_homes), root) or raw_target
    if recipient == me:
        return json.dumps({"error": "You can't message yourself. Pick a teammate from the roster.",
                           "reason": "invalid_params"})
    recipient_home, handle = roster_homes.get(recipient), _handle(recipient)

    card_id = getattr(agent, "task_id", None) or getattr(agent, "_kanban_task_id", None) or os.environ.get(
        "HERMES_KANBAN_TASK")
    card_id = card_id.strip() if isinstance(card_id, str) else card_id
    if not card_id:
        return json.dumps({"error": "Kanban worker lacks a card id, so the one-way message has no provenance "
                                    "or reply route. Do not retry.", "reason": "invalid_params"})
    origin_board = os.environ.get("HERMES_KANBAN_DB") or "profile default board"
    text = (f"{content.split(': ', 1)[0]} [card {card_id}; board {origin_board}; profile home {home}]: {body}\n"
            f"One-way worker message. Reply route hint: card {card_id} on the named board; "
            "automated replies are not relayed to the worker.")
    verified = recipient_drain_enabled(recipient, profile_home=recipient_home)
    warning = None if verified else (
        "Queued storage is not confirmed delivery. No usable recipient drain was verified. Next step: the "
        "target machine's Bot Maker or Conductor checks fleet_messages.drain_on_turn_end before relying on it.")
    try:
        if recipient_home is not None and verified:
            message_id = spool_busy_dm(recipient_home, sender=card_id, recipient=recipient, body=text)["message_id"]
        else:
            message_id = enqueue_busy_dm(sender=card_id, recipient=recipient, body=text)
    except Exception as exc:
        return json.dumps({"error": f"Failed to queue the message ({type(exc).__name__}). Next step: check the "
                                    "fleet message writer configuration and retry after repair.",
                           "reason": "runtime_unavailable"})
    try:
        from tools.kanban_tools import _board
        with _board(None, quiet_close=True) as (kb, conn):
            comment = f"Sent one-way message to @{handle} (queued {message_id}): {body}"
            kb.add_comment(conn, card_id, author=me or "worker", body=comment + (f"\n{warning}" if warning else ""))
    except Exception as exc:
        logger.warning("Queued message %s has no origin-card comment (%s); post its id on card %s.",
                       message_id, type(exc).__name__, card_id)
    return json.dumps({"status": "queued", "message_id": message_id, "to": f"@{handle}", "reply_route": card_id,
                       "reply_relayed": False, "delivery_owner_verified": verified, "warning": warning,
                       "reply": queued_ack(message_id)})
