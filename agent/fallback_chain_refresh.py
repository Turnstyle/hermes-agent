"""Live fallback-chain refresh for long-lived agents (H20).

The CLI, the Desktop/TUI gateway and the messaging gateway re-read ``fallback_providers`` at turn
start and hand the chain here. A refresh is not a new failure: the primary cooldown, the
reset-aware pool gate and the entitlement gate stay in force (``restore_primary_runtime`` owns
them), and an unrelated edit must not make an unchanged known-bad route eligible again. What a
refresh may not do is leave the agent on a route the operator removed: when neither the primary
nor another configured route is eligible, the turn is refused before anything is staged or sent.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ``chain`` default: read the active profile's config. An explicit ``None`` means "no chain".
_READ_PROFILE_CONFIG = object()


class FallbackRouteUnavailableError(RuntimeError):
    """The active fallback was removed from config and no configured route is eligible this turn."""

    user_message = (
        "Your message was not sent to the model. The active fallback was removed, "
        "and no configured route is eligible. Wait for the primary cooldown or "
        "choose an available model, then resend your message."
    )
    # ``agent.error_surface`` shape: a client can offer a retry, nothing ran.
    error_surface = {"layer": "runtime", "code": "fallback_route_unavailable", "retryable": True}


def _profile_fallback_chain() -> list | None:
    """The active profile's chain; None when config.yaml cannot be read (a torn mid-edit write must
    keep the last known-good chain, never read as "chain removed")."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.fallback_config import get_fallback_chain
    try:
        return get_fallback_chain(load_user_config_effective(fail_closed=True))
    except Exception:
        logger.debug("fallback chain refresh: config read failed; keeping last known-good chain", exc_info=True)
        return None


def _forget_changed_route_failures(agent, old_chain: list, new_chain: list) -> None:
    """Only a changed or removed route may forget its session failure memo: an unrelated edit, a
    reorder or removing the active entry must not retry unchanged known-bad routes."""
    unavailable = getattr(agent, "_unavailable_fallback_keys", None)
    if not unavailable:
        return
    from agent.chat_completion_helpers import _fallback_entry_key
    unavailable.intersection_update({_fallback_entry_key(e) for e in new_chain if e in old_chain})


def _route_index(agent, chain: list) -> int | None:
    """Index of the entry the agent runs on. Entries omit provider-resolved endpoints and may use
    vendor-prefixed names, so models compare in runtime space; an explicit endpoint still binds."""
    from hermes_cli.fallback_config import _entry_identity
    from hermes_cli.model_normalize import normalize_model_for_provider
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    model = str(getattr(agent, "model", "") or "").strip().lower()
    base_url = str(getattr(agent, "base_url", "") or "").strip().lower().rstrip("/")
    for index, (e_provider, e_model, e_base_url) in enumerate(_entry_identity(e) for e in chain):
        if (provider == e_provider and model == normalize_model_for_provider(e_model, e_provider).lower()
                and (not e_base_url or base_url == e_base_url)):
            return index
    return None


def _leave_removed_route(agent) -> None:
    """Move off an active fallback the operator removed. The primary comes back only through its
    own gates (cooldown, pool reset, entitlement); otherwise the next eligible route, else refuse."""
    logger.info("Active fallback %s (%s) is no longer in fallback config; switching off it.",
                getattr(agent, "model", ""), getattr(agent, "provider", ""))
    if agent._restore_primary_runtime():
        return
    agent._fallback_index = 0
    if not agent._try_activate_fallback():
        raise FallbackRouteUnavailableError(
            "Active fallback removed; primary is unavailable and no configured fallback is eligible")


def apply_fallback_chain_to_agent(agent: Any, chain: Any = _READ_PROFILE_CONFIG) -> bool:
    """Align a long-lived agent's fallback chain with current config; True when the chain changed.

    ``chain`` is the caller's freshly read chain (``None``: no fallback); omitted, the active
    profile's config is read fail-closed. An owner-pinned chain (delegated child, cron job;
    ``hermes_cli.fallback_config.scoped_fallback_chain``) is never replaced by profile config.
    Raises :class:`FallbackRouteUnavailableError` when the active fallback was removed and no route
    is eligible: the caller refuses the turn unsent.
    """
    if agent is None or getattr(agent, "_fallback_chain_pinned", False):
        return False
    new_chain = _profile_fallback_chain() if chain is _READ_PROFILE_CONFIG else list(chain or [])
    if new_chain is None:
        return False
    old_chain = list(getattr(agent, "_fallback_chain", None) or [])
    changed = new_chain != old_chain
    if changed:
        _forget_changed_route_failures(agent, old_chain, new_chain)
        agent._fallback_chain = new_chain
        agent._fallback_model = new_chain[0] if new_chain else None
    if not getattr(agent, "_fallback_activated", False):
        if changed:
            agent._fallback_index = 0
        return changed
    active = _route_index(agent, new_chain)
    if active is not None:
        agent._fallback_index = active + 1
    elif _route_index(agent, old_chain) is not None:
        # Only a route that WAS a chain entry is "removed"; a `/model --once` or voice route that
        # never was one stays with restore_primary_runtime, as before.
        _leave_removed_route(agent)
    return changed
