"""Owner delegate rule: the first stored user message of a Claude session carries it.

Why: the owner requires every Claude-routed session to open with ``DELEGATE_RULE``, and
the rule must live in the session's FIRST stored user row so the transcript, replay,
compaction and audits all hold the bytes the model saw. Every writer decides through ONE
function, ``should_inject``: the turn prologue (``apply_turn_start_policy``, called by
``agent.turn_context.build_turn_context`` after the primary runtime is restored) and the
early writers that store the first row before the turn starts (CLI pending stage, Desktop
submit row); the prologue is then a no-op by idempotency.

``check_wire_messages`` (``build_api_messages``) only observes. A Claude route on the wire
whose session-first user message lacks the rule (fallback/voice changed the route after the
first turn, the session predates the rule, or a client resent its own history) logs a WARNING
once per session and bumps ``WIRE_RULE_MISSING_COUNT``; it raises only under the process-wide
opt-in ``HERMES_DELEGATE_RULE_STRICT=1`` and never rewrites the wire, because a wire-only edit
makes the stored transcript diverge from what was sent. A request whose head a compaction
replaced proves nothing about the first message and is not judged.

Known limits:
- Claude route = provider ``anthropic`` OR ``claude`` anywhere in the model id. Qualified
  OpenRouter/Bedrock ids (``anthropic/claude-*``, ``us.anthropic.claude-*``) match; any other
  model whose id contains ``claude`` over-matches; an opaque alias for a Claude model behind
  another provider (``sonnet``, a custom alias) under-matches, and so does a MoA virtual
  session (provider ``moa``, model = preset name).
- First user turn only: later turns, resumed sessions and a mid-session route change never
  rewrite a stored row. A rule an earlier writer already stored is never stripped, even when
  the turn-start route turns out non-Claude (accepted over-inclusion).
- MoA inner proposer/aggregator calls are not stored sessions and are left untouched.
- No credentials are read and no provider is called. A route with no usable model is
  resolved from the active profile's default pin in config.yaml.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

DELEGATE_RULE = "Use 1 to 10 delegate_task sub-agents and don't do the work inside your main model, and pass this same instruction on to anyone you hand work to."
STRICT_ENV = "HERMES_DELEGATE_RULE_STRICT"

# Desktop submit rows record the session they were written under (tui_gateway/session_workdir.py
# ``_SUBMIT_ROW_SESSION_KEY``); agent/ does not import tui_gateway, so the key is spelled here.
_SUBMIT_ROW_SESSION_KEY = "_submit_row_session_id"
_TEXT_BLOCK_TYPES = ("text", "input_text")

# Wire-check state: every detection counts, each (condition, session) warns once per process.
WIRE_RULE_MISSING_COUNT = 0
_wire_warned: set[tuple[str, str]] = set()
_wire_lock = threading.Lock()


def is_claude_route(provider: Any, model: Any) -> bool:
    return str(provider or "").strip().lower() == "anthropic" or "claude" in str(model or "").lower()


def default_pin() -> tuple[str, str]:
    """``(provider, model)`` of the active profile's ``model:`` config, read now through the
    canonical profile-aware loader (a broken file yields what every reader gets: last-known-good
    or defaults). ``("", "")`` when unset or when the loader fails; never raises."""
    try:
        from hermes_cli.config import load_config_readonly, split_model_config_default

        model_cfg = load_config_readonly().get("model")
        if isinstance(model_cfg, str):
            return "", model_cfg.strip()
        if not isinstance(model_cfg, dict):
            return "", ""
        # Legacy ``model.model`` still pins; a dict-valued id carries its own provider, which
        # (as in the loader's normalization) only fills an unset or "auto" outer provider.
        model, nested_provider = split_model_config_default(
            model_cfg.get("default") or model_cfg.get("model"))
        provider = str(model_cfg.get("provider") or "").strip()
        return (provider if provider.lower() not in ("", "auto") else nested_provider or provider), model
    except Exception:
        logger.warning("delegate rule: default model pin unreadable; treating it as unset", exc_info=True)
        return "", ""


def route_requires_rule(provider: Any, model: Any, *, pin: Optional[tuple[str, str]] = None) -> bool:
    """Claude by the resolved route; only a route without a usable model consults the profile
    pin (``pin`` lets a caller that already read it inside the right profile scope pass it)."""
    # A virtual provider's model is a preset name, not an inference model.
    if str(provider or "").strip().lower() == "moa":
        return False
    if is_claude_route(provider, model):
        return True
    if isinstance(model, str) and model.strip():
        return False
    return is_claude_route(*(pin if pin is not None else default_pin()))


def admission_route(agent: Any) -> tuple[Any, Any]:
    """The route an early writer prices: the primary snapshot the next turn start restores, else
    the live route. Never restores clients or reads credentials."""
    primary = getattr(agent, "_primary_runtime", None)
    if isinstance(primary, dict) and primary:
        return primary.get("provider"), primary.get("model")
    return getattr(agent, "provider", None), getattr(agent, "model", None)


def has_rule(content: Any) -> bool:
    if isinstance(content, str):
        return DELEGATE_RULE in content
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") in _TEXT_BLOCK_TYPES
            and isinstance(block.get("text"), str) and DELEGATE_RULE in block["text"]
            for block in content
        )
    return False


def append_rule(content: Any) -> Any:
    """Content with the rule appended once; never mutates the input. List content keeps every
    block (media included) and gains a final text block."""
    if has_rule(content):
        return content
    if isinstance(content, str):
        return content + "\n\n" + DELEGATE_RULE if content else DELEGATE_RULE
    if isinstance(content, list):
        return copy.deepcopy(content) + [{"type": "text", "text": DELEGATE_RULE}]
    raise TypeError(f"delegate rule needs str or list content, got {type(content).__name__}")


def has_prior_user_message(history: Any) -> bool:
    return any(isinstance(msg, dict) and msg.get("role") == "user" for msg in history or ())


def strip_rule(content: Any) -> Any:
    """Clean consumer copy; never alter media or the durable/wire transcript."""
    if isinstance(content, str):
        return content.replace(DELEGATE_RULE, "").strip()
    if isinstance(content, list):
        result = copy.deepcopy(content)
        for part in result:
            if isinstance(part, dict) and part.get("type") in _TEXT_BLOCK_TYPES and isinstance(part.get("text"), str):
                part["text"] = strip_rule(part["text"])
        return result
    return content


def clean_messages(messages: Any) -> Any:
    """Rule-free copies for external memory and toolless reference models."""
    result = copy.deepcopy(messages)
    for message in result or ():
        if isinstance(message, dict) and message.get("role") == "user":
            for key in ("content", "api_content"):
                if key in message:
                    message[key] = strip_rule(message[key])
    return result


def session_has_user_row(db: Any, session_id: Any, *, exclude_row_id: Optional[int] = None) -> bool:
    """Whether ANY earlier user row exists, including typed, inactive and archived rows.

    Renderer/undo projections omit real stored inputs. They cannot decide whether
    this is the first durable user message. Exclude only the staged row this turn
    will adopt; a read failure permits over-inclusion and logs the failed probe.
    """
    if db is None or not session_id:
        return False
    try:
        from hermes_state_common import QUEUED_PROMPT_METADATA_KEY
        import json
        rows = db._read_all(
            "SELECT id, display_metadata FROM messages WHERE session_id = ? AND role = 'user' "
            "AND (? IS NULL OR id != ?)",
            (session_id, exclude_row_id, exclude_row_id),
        )
        for row in rows:
            metadata = row["display_metadata"]
            metadata = json.loads(metadata) if isinstance(metadata, str) and metadata else metadata or {}
            if not isinstance(metadata, dict) or not metadata.get(QUEUED_PROMPT_METADATA_KEY):
                return True
        return False
    except Exception:
        logger.warning("delegate rule: user-row probe failed for session=%s", session_id, exc_info=True)
        return False


def is_first_user_turn(
    history: Any, db: Any, session_id: Any, *, exclude_row_id: Optional[int] = None,
) -> bool:
    """No prior user message in the passed history (the DB is read only then) and none stored."""
    return not has_prior_user_message(history) and not session_has_user_row(
        db, session_id, exclude_row_id=exclude_row_id)


def should_inject(
    provider: Any, model: Any, history: Any, db: Any, session_id: Any, *,
    exclude_row_id: Optional[int] = None, pin: Optional[tuple[str, str]] = None,
) -> bool:
    """THE decision every writer shares: the session's first user turn on a Claude route.
    Cheapest first: the passed history, then the route (config only without a usable model),
    then the DB."""
    return (
        not has_prior_user_message(history)
        and route_requires_rule(provider, model, pin=pin)
        and not session_has_user_row(db, session_id, exclude_row_id=exclude_row_id)
    )


def _rule_if_text(content: Any) -> Any:
    return append_rule(content) if isinstance(content, (str, list)) else content


def apply_to_turn(user_message: Any, persist_user_message: Any) -> tuple[Any, Any]:
    """Rule the wire value and the clean durable override alike, so the stored row is what the
    model saw; ``None`` stays ``None`` and other types pass through (never raise on a live turn)."""
    return _rule_if_text(user_message), _rule_if_text(persist_user_message)


def _staged_row_of_this_turn(agent: Any, raw: Any) -> Optional[dict[str, Any]]:
    """The early writer's dict for THIS turn, raw or already ruled. ``_stage_turn_user_message``
    adopts the pending dict only when it equals this turn's expected content; anything else is
    stale input it drops, so it must not shield the first-turn check."""
    pending = getattr(agent, "_pending_cli_user_message", None)
    if not isinstance(pending, dict) or not isinstance(raw, (str, list)):
        return None
    content = pending.get("content")
    return pending if content == raw or (has_rule(content) and content == append_rule(raw)) else None


def _int_row_id(row: Optional[dict[str, Any]]) -> Optional[int]:
    row_id = row.get("_row_id") if row is not None else None
    return row_id if isinstance(row_id, int) and not isinstance(row_id, bool) else None


def _rule_staged_row(agent: Any, staged: dict[str, Any], db: Any, row_id: Optional[int]) -> None:
    """Rule the staged first row so ``_stage_turn_user_message`` still adopts the same dict (a
    mismatch would drop it and write a second row). A row the early writer already made durable
    is rewritten too, under the persist lock like ``turn_context._append_multimodal_context``:
    a close flush holds that lock while it commits and stamps ``_row_id``."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.session_persistence import _durable_content, _persist_lock

    with _persist_lock(agent):
        staged["content"] = append_rule(staged["content"])
        if db is None or row_id is None or not staged.get(_DB_PERSISTED_MARKER):
            return
        owner = staged.get(_SUBMIT_ROW_SESSION_KEY) or agent.session_id
        try:
            updated = db.set_user_message_content(owner, row_id, _durable_content(staged["content"]))
        except Exception:
            logger.warning(
                "delegate rule: staged first row %s not updated (session=%s)", row_id, owner, exc_info=True)
            return
    if not updated:
        logger.warning("delegate rule: staged first row %s no longer active (session=%s)", row_id, owner)


def _rule_carried_first_row(agent: Any, history: list[Any], db: Any, session_id: Any) -> None:
    """Input whose turn was interrupted before admission rides the next turn's history
    (``turn_facade_lease.carry_unadmitted_user_message``) and is stored by ``build_turn_context``
    after this chokepoint. When nothing is stored yet it becomes the session's first row, so rule
    it first; it was never sent, so no cached prefix changes."""
    from agent.session_persistence import _PERSIST_AFTER_ADMISSION_INTERRUPT

    # Carried input is appended after stored history, so a stored first row ends this at once.
    users = (msg for msg in history if isinstance(msg, dict) and msg.get("role") == "user")
    first = next(users)
    if not first.get(_PERSIST_AFTER_ADMISSION_INTERRUPT) or not all(
            msg.get(_PERSIST_AFTER_ADMISSION_INTERRUPT) for msg in users):
        return
    if not isinstance(first.get("content"), (str, list)) or has_rule(first["content"]):
        return
    if not route_requires_rule(agent.provider, agent.model) or session_has_user_row(db, session_id):
        return
    first["content"] = append_rule(first["content"])
    # A historical row replays its api_content sidecar on the wire.
    if isinstance(first.get("api_content"), str) and first["api_content"]:
        first["api_content"] = append_rule(first["api_content"])


def apply_turn_start_policy(
    agent: Any, user_message: Any, persist_user_message: Any, conversation_history: Any,
) -> tuple[Any, Any]:
    """``build_turn_context`` chokepoint: after the primary runtime is restored (the resolved
    route decides) and before the persist override is bound. Returns the turn's
    ``(user_message, persist_user_message)``."""
    turn = _turn_start_values(agent, user_message, persist_user_message, conversation_history)
    # Reset every turn: the loop's caller still holds the raw message, and
    # ``turn_context.export_current_turn_boundary`` must prove this turn's row in its ruled form.
    agent._delegate_rule_turn_message = turn[0] if turn[0] is not user_message else None
    return turn


def _turn_start_values(
    agent: Any, user_message: Any, persist_user_message: Any, conversation_history: Any,
) -> tuple[Any, Any]:
    raw = persist_user_message if persist_user_message is not None else user_message
    staged = _staged_row_of_this_turn(agent, raw)
    if (staged is not None and staged.get("_delegate_rule_speculative")
            and not route_requires_rule(agent.provider, agent.model)
            and not has_prior_user_message([msg for msg in conversation_history or () if msg is not staged])):
        from agent.session_persistence import _durable_content, _persist_lock
        db = getattr(agent, "_session_db", None)
        with _persist_lock(agent):
            clean = strip_rule(staged["content"])
            row_id = _int_row_id(staged)
            if db is not None and row_id is not None:
                if not db.set_user_message_content(agent.session_id, row_id, _durable_content(clean)):
                    raise RuntimeError("speculative delegate rule row could not be reconciled")
            staged["content"] = clean
            staged.pop("_delegate_rule_speculative", None)
        return strip_rule(user_message), strip_rule(persist_user_message)
    if staged is not None and has_rule(staged.get("content")):
        # An earlier writer stored the rule: never strip it, and keep this turn equal to the
        # staged row so it is adopted rather than written twice.
        return apply_to_turn(user_message, persist_user_message)
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if has_prior_user_message(conversation_history):
        _rule_carried_first_row(agent, conversation_history, db, session_id)
        return user_message, persist_user_message
    row_id = _int_row_id(staged)
    if not should_inject(
        agent.provider, agent.model, conversation_history, db, session_id, exclude_row_id=row_id,
    ):
        return user_message, persist_user_message
    if staged is not None:
        _rule_staged_row(agent, staged, db, row_id)
    return apply_to_turn(user_message, persist_user_message)


def _warn_once(condition: str, session_key: str, message: str, *args: Any) -> None:
    with _wire_lock:
        if (condition, session_key) in _wire_warned:
            return
        _wire_warned.add((condition, session_key))
    logger.warning(message, *args)


def _count_missing() -> None:
    global WIRE_RULE_MISSING_COUNT
    with _wire_lock:
        WIRE_RULE_MISSING_COUNT += 1


def _head_compacted(head: list[Any]) -> bool:
    """A compaction handoff at or before the anchor means the session's first message left the
    wire (head protection decays after the first compression), so its absence proves nothing."""
    from agent.context_compressor import ContextCompressor

    return any(ContextCompressor._is_context_summary_message(msg) for msg in head)


def check_wire_messages(agent: Any, api_messages: list[Any]) -> None:
    """``build_api_messages`` observer (never mutates). The FIRST user message is the presence
    anchor (the session's first message); the LAST user message is only type-checked so an odd
    current-turn shape never crashes the send."""
    # build_api_messages' callers pass duck-typed agents that need not carry a route.
    provider, model = getattr(agent, "provider", None), getattr(agent, "model", None)
    if not route_requires_rule(provider, model):
        return
    session_key = str(getattr(agent, "session_id", None) or f"agent-{id(agent)}")
    users = [
        (idx, msg) for idx, msg in enumerate(api_messages or ())
        if isinstance(msg, dict) and msg.get("role") == "user"
    ]
    if not users:
        _warn_once("no_user", session_key, "delegate rule: Claude request without a user message "
                   "(session=%s); rule presence not checked", session_key)
        return
    anchor_idx, anchor = users[0]
    if not all(isinstance(msg.get("content"), (str, list)) for msg in (anchor, users[-1][1])):
        _warn_once("odd_content", session_key, "delegate rule: user content is neither text nor parts "
                   "(session=%s); rule presence not checked", session_key)
        return
    if has_rule(anchor["content"]) or _head_compacted(api_messages[: anchor_idx + 1]):
        return
    _count_missing()
    _warn_once(
        "missing", session_key,
        "delegate rule: Claude route %s/%s is on the wire but the first user message of session=%s lacks "
        "the rule (route changed after the first turn, or the session predates it); %s=1 fails closed",
        provider, model, session_key, STRICT_ENV,
    )
    if os.environ.get(STRICT_ENV) == "1":  # health: allow HX002 -- owner brief mandates this opt-in strict switch
        raise RuntimeError(
            f"delegate rule missing from the first user message of Claude session {session_key} ({STRICT_ENV}=1)")
