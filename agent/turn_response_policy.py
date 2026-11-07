"""Terminal post-delivery policy for providers that own native effects.

One returned-answer choke precedes invalid/empty/truncation recovery and every
synthetic stop nudge. Complete tool calls are the sole pass-through: their actual
new tool results authorize the next model request. Never confuse missing visible
text with proof that a native prompt was not delivered.
"""
from __future__ import annotations

from typing import Any

from agent.message_metadata import append_message


def no_replay_notice(agent: Any, reason: str) -> str:
    provider = "Cursor" if agent.provider == "cursor-acp" else str(agent.provider)
    return (f"The request reached {provider}, but {reason}. "
            "It was not resent automatically and no fallback provider was activated.")


def terminal_delivered_response(
    agent: Any, *, messages: list, api_calls: int, normalized: Any = None,
    reason: str | None = None, partial_prefix: str = "", conversation_history: Any = None,
) -> dict:
    """Persist usable delivered text or an honest terminal error, without a nudge.

    Also used when local response processing throws after the API returned, and
    when tool JSON is malformed. These are not safe opportunities to resend.
    """
    from agent.trajectory import has_incomplete_scratchpad

    raw = normalized.content if normalized is not None else ""
    raw = raw if isinstance(raw, str) else ""
    visible = agent._strip_think_blocks(raw).strip()
    if has_incomplete_scratchpad(raw):
        visible = ""
        reason = reason or "it returned an incomplete reasoning scratchpad"
    finish = normalized.finish_reason if normalized is not None else None
    if reason is None and not visible:
        reason = "it returned no usable visible answer"
    if reason is None and finish not in (None, "stop", "end_turn", "completed"):
        reason = f"its response ended with {finish!r}"
    error = no_replay_notice(agent, reason) if reason else None
    final = partial_prefix + visible if visible or partial_prefix else error
    # Preserve provider reasoning/signatures but never execute abandoned calls or
    # leak an inline thinking-only payload as the displayed final answer.
    row = (agent._build_assistant_message(normalized, finish)
           if normalized is not None else {"role": "assistant", "content": final})
    row["content"] = final
    row.pop("tool_calls", None)
    append_message(messages, row)
    agent._persist_session(messages, conversation_history)
    if error:
        return {"final_response": final, "messages": messages, "api_calls": api_calls,
                "completed": False, "partial": bool(visible or partial_prefix or has_incomplete_scratchpad(raw)),
                "failed": not bool(visible or partial_prefix or has_incomplete_scratchpad(raw)),
                "error": error, "error_type": "response_replay_forbidden", "retryable": False,
                "partial_reason": "response_replay_forbidden", "failure_reason": "response_replay_forbidden",
                "failure_retryable": False, "_response_replay_policy": True}
    return {"final_response": final, "messages": messages, "api_calls": api_calls,
            "completed": True, "_response_replay_policy": True}


def finish_delivered_response_if_required(
    agent: Any, *, response: Any, response_invalid: bool, error_details: list,
    messages: list, api_calls: int, hook_context: dict, usage_context: dict,
    conversation_history: Any = None,
) -> dict | None:
    """Caller checks provider_forbids_response_replay; None permits real tool work."""
    from agent import relay_llm
    from agent.provider_projection import splice_provider_projection
    from agent.turn_response_intake import _coerce_content_text, _fire_post_api_request_hook
    from agent.turn_truncation import normalize_response_for_agent
    from agent.turn_usage import record_response_usage

    if response_invalid:
        return terminal_delivered_response(
            agent, messages=messages, api_calls=api_calls, conversation_history=conversation_history,
            reason="it returned an invalid response (" + "; ".join(error_details) + ")")
    normalized = normalize_response_for_agent(agent, response)
    if normalized.content is not None and not isinstance(normalized.content, str):
        normalized.content = _coerce_content_text(normalized.content)
    # Truncated calls cannot be safely executed or regenerated. A complete call,
    # however, produces a new tool result and is not an automatic bare replay.
    if (normalized.tool_calls and normalized.finish_reason in (None, "stop", "tool_calls")
            and not getattr(response, "_content_filter_terminated", False)):
        return None
    agent._turn_received_provider_response = True
    record_response_usage(agent, response, messages=messages, **usage_context)
    splice_provider_projection(agent, response, messages)
    _fire_post_api_request_hook(agent, response, normalized, normalized.finish_reason, **hook_context)
    relay_llm.complete_logical_call(hook_context["api_request_id"], outcome="success")
    reason = None
    if getattr(response, "_content_filter_terminated", False):
        reason = "the response stream terminated before completion"
    return terminal_delivered_response(
        agent, messages=messages, api_calls=api_calls, normalized=normalized,
        conversation_history=conversation_history, reason=reason)
