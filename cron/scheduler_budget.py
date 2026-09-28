"""Per-job wake budgets (``max_tokens`` / ``max_turns``) for agent-backed cron jobs.

Both fields are operator-owned (hand-set in ``jobs.json``) and deliberately not exposed on the
agent-facing ``cronjob_manage`` tool, so a scheduled model cannot raise its own cost limits.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.turn_cron_budget import CRON_MAX_TURNS_EXIT_PREFIX


def positive_job_budget(job: dict, key: str) -> Optional[int]:
    """Return one explicitly configured positive per-job budget.

    These fields are a hard safety boundary, so hand-edited booleans, strings, zero, and negative
    values fail closed at fire time instead of silently falling back to the profile-wide
    interactive budget.
    """
    value = job.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"cron job {key} must be a positive integer")
    return value


def apply_job_budgets_to_agent(agent: Any, *, max_tokens: Optional[int], max_turns: Optional[int]) -> None:
    """Arm the scheduler-owned budget attributes on a freshly constructed cron agent.

    The turn loop clamps each next response to the remaining per-fire token headroom, and the
    finalizer suppresses its otherwise unmetered summary call at the cron turn limit.
    """
    agent._cron_max_tokens = max_tokens
    agent._cron_max_turns = max_turns
    default_output = getattr(agent, "max_tokens", None)
    agent._cron_default_response_max_tokens = (
        default_output
        if isinstance(default_output, int) and not isinstance(default_output, bool) and default_output > 0
        else None
    )
    agent._cron_budget_stopped = False
    agent._cron_budget_stop_reason = None


def is_cron_turn_limit_handoff(result: Any) -> bool:
    """A non-failed fire that ended at its per-job ``max_turns`` with a deterministic message.

    Delivered like the ``max_iterations_reached`` summary handoff instead of failing the run.
    """
    if not isinstance(result, dict):
        return False
    if result.get("failed") is True or result.get("interrupted") is True:
        return False
    if result.get("completed") is not False:
        return False
    reason = result.get("turn_exit_reason")
    if not (isinstance(reason, str) and reason.startswith(CRON_MAX_TURNS_EXIT_PREFIX)):
        return False
    return bool(str(result.get("final_response") or "").strip())


def usage_audit_value(result: dict, key: str, agent: Any, attr: str) -> Any:
    """Prefer returned usage, then the live agent tally on partial failures."""
    value = result.get(key)
    if value is None and agent is not None:
        value = getattr(agent, attr, None)
    return value


def budget_audit_fields(job: dict, agent: Any) -> dict:
    """The budget columns every agent-backed ``usage_audit.jsonl`` row carries."""
    return {
        "max_tokens": job.get("max_tokens"),
        "max_turns": job.get("max_turns"),
        "budget_stopped": bool(getattr(agent, "_cron_budget_stopped", False)),
        "budget_stop_reason": getattr(agent, "_cron_budget_stop_reason", None),
    }
