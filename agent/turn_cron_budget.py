"""Per-fire token and turn budgets for scheduled (cron) agent runs.

The scheduler sets ``_cron_max_tokens`` / ``_cron_max_turns`` on the cron agent only; interactive
agents never carry them, so every helper here is a no-op outside a budgeted cron fire.
"""

from __future__ import annotations

from typing import Any, Optional

CRON_MAX_TOKENS_EXIT = "cron_max_tokens_reached"
CRON_MAX_TURNS_EXIT_PREFIX = "cron_max_turns_reached("


def _positive_cron_int(value: Any) -> Optional[int]:
    """Return a positive, non-boolean integer or ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _mark_stopped(agent: Any, reason: str) -> None:
    agent._cron_budget_stopped = True
    agent._cron_budget_stop_reason = reason


def apply_cron_token_budget(agent: Any, request_pressure_tokens: Any) -> bool:
    """Clamp or stop the next main-model request for a capped cron fire.

    Provider usage is authoritative for prior calls. Before another call, reserve its
    assembled-request pressure and restrict its response allowance to the remaining per-fire
    headroom. Return ``True`` when no output token can fit and the caller must stop before the
    provider request.
    """
    cap = _positive_cron_int(getattr(agent, "_cron_max_tokens", None))
    if cap is None:
        return False
    used = _positive_cron_int(getattr(agent, "session_total_tokens", None)) or 0
    pressure = _positive_cron_int(request_pressure_tokens) or 0
    output_headroom = cap - used - pressure
    if output_headroom <= 0:
        _mark_stopped(agent, "max_tokens")
        return True
    default_output = _positive_cron_int(getattr(agent, "_cron_default_response_max_tokens", None))
    agent.max_tokens = min(default_output, output_headroom) if default_output else output_headroom
    return False


def cron_token_budget_stop_message(agent: Any, request_pressure_tokens: Any) -> str:
    """Deterministic final response for a fire stopped before its next request."""
    cap = _positive_cron_int(getattr(agent, "_cron_max_tokens", None)) or 0
    used = _positive_cron_int(getattr(agent, "session_total_tokens", None)) or 0
    pressure = _positive_cron_int(request_pressure_tokens) or 0
    return (
        "Cron wake stopped at its configured max_tokens budget "
        f"({used}/{cap} provider-reported tokens used; next request pressure ~{pressure})."
    )


def mark_cron_token_budget_after_usage(agent: Any) -> None:
    """Mark a capped cron wake once provider-reported usage reaches its ceiling."""
    cap = _positive_cron_int(getattr(agent, "_cron_max_tokens", None))
    used = getattr(agent, "session_total_tokens", 0)
    if cap is not None and isinstance(used, int) and used >= cap:
        _mark_stopped(agent, "max_tokens")


def cron_turn_limit_stop(agent: Any, api_call_count: int) -> Optional[tuple[str, str]]:
    """``(final_response, turn_exit_reason)`` when a per-job cron turn limit ended the fire.

    A per-job cron limit is a hard cost boundary: the generic iteration fallback would make one
    extra, unmetered summary call, so return a deterministic result instead. ``None`` when the
    agent carries no per-job turn limit (interactive runs and unbudgeted jobs).
    """
    limit = _positive_cron_int(getattr(agent, "_cron_max_turns", None))
    if limit is None:
        return None
    _mark_stopped(agent, "max_turns")
    return (
        f"Cron wake stopped at its configured max_turns limit ({api_call_count}/{limit}).",
        f"{CRON_MAX_TURNS_EXIT_PREFIX}{api_call_count}/{limit})",
    )
