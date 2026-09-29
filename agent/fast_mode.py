"""Bounded fast-mode windows and per-entry fallback service tiers.

``agent.service_tier``: ``None`` (normal), ``"priority"`` (static fast, pinned into
``agent.request_overrides`` at build time), ``"auto"`` (every user turn opens a
window of ``agent.fast_auto_seconds``) or ``"cold"`` (only a session's first turn,
no prior history, opens it). The provider's fast override is layered onto request
kwargs only while the window is open; only per-request params (``service_tier`` /
``speed``) vary, so the request body stays byte-identical. Anthropic keeps a separate
prompt cache per speed, so each Anthropic window boundary re-writes the prefix at the
new speed.
"""

from __future__ import annotations

import time
import logging
from typing import Any

BOUNDED_MODES = frozenset({"auto", "cold"})
DEFAULT_WINDOW_SECONDS = 60
# Documented fast-mode rate-limit headers; a limit of 0 means the organization has no fast
# capacity for the model (https://platform.claude.com/docs/en/build-with-claude/fast-mode).
_FAST_LIMIT_HEADERS = ("anthropic-fast-input-tokens-limit", "anthropic-fast-output-tokens-limit")
logger = logging.getLogger(__name__)
_NORMAL_TIERS = frozenset({"", "normal", "default", "standard", "off", "none"})
_PRIORITY_TIERS = frozenset({"fast", "priority", "on"})
ALLOWED_SERVICE_TIERS = "normal/default/standard/off/none/empty, fast/priority/on, auto, cold"


def parse_fallback_service_tier(raw: Any) -> tuple[str | None, bool]:
    """Parse the agent tier vocabulary while distinguishing an invalid fallback value."""
    if not isinstance(raw, str):
        return None, False
    value = raw.strip().lower()
    if value in _NORMAL_TIERS:
        return None, True
    if value in _PRIORITY_TIERS:
        return "priority", True
    if value in BOUNDED_MODES:
        return value, True
    return None, False


def bind_fallback_service_tier(agent: Any, entry: dict[str, Any]) -> None:
    """Bind one activated entry; malformed tiers fail closed and are logged once per entry."""
    tier, ok = parse_fallback_service_tier(entry.get("service_tier", ""))
    agent._fallback_service_tier = tier
    agent._fallback_service_tier_entry = entry
    if not ok:
        logged = getattr(agent, "_invalid_fallback_tier_entries", None)
        if logged is None:
            logged = agent._invalid_fallback_tier_entries = set()
        entry_id = (
            str(entry.get("provider") or "").strip().lower(),
            str(entry.get("model") or "").strip().lower(),
            str(entry.get("base_url") or "").strip().lower(),
            str(entry.get("service_tier")),
        )
        if entry_id not in logged:
            logged.add(entry_id)
            logger.error("Invalid fallback service_tier for %s/%s; using normal (allowed: %s)",
                         entry.get("provider"), entry.get("model"), ALLOWED_SERVICE_TIERS)
    prior_history = [{"role": "user"}] if getattr(agent, "_fast_turn_has_history", False) else []
    begin_turn(agent, prior_history)
    if tier in {"priority", "auto", "cold"}:
        _warn_unsupported_fallback_route(agent, tier)


def _warn_unsupported_fallback_route(agent: Any, tier: str) -> None:
    from hermes_cli.models import resolve_fast_mode_overrides

    route = (getattr(agent, "provider", None), getattr(agent, "model", None))
    base_url = getattr(agent, "base_url", None)
    if getattr(agent, "api_mode", None) == "anthropic_messages":
        base_url = getattr(agent, "_anthropic_base_url", None) or base_url
    if resolve_fast_mode_overrides(route[1], provider=route[0], base_url=base_url):
        return
    warned = getattr(agent, "_unsupported_fallback_tier_routes", None)
    if warned is None:
        warned = agent._unsupported_fallback_tier_routes = set()
    if route not in warned:
        warned.add(route)
        logger.warning("Fallback entry %s/%s requests %s service tier, but this provider/model route does not support fast mode; sending normal",
                       *route, tier)


def activate_initial_fallback(agent: Any, entry: dict[str, Any]) -> None:
    """A credential-resolution fallback starts life on its selected route."""
    agent._initial_fallback_entry = entry
    agent._fallback_activated = True
    bind_fallback_service_tier(agent, entry)


def effective_service_tier(agent: Any) -> str | None:
    if getattr(agent, "_fallback_activated", False) and getattr(agent, "_fallback_service_tier_entry", None) is not None:
        return getattr(agent, "_fallback_service_tier", None)
    return getattr(agent, "service_tier", None)


def record_sent_service_tier(agent: Any, kwargs: dict[str, Any]) -> None:
    """Keep the last physical request's billing field for the turn result."""
    extra_body = kwargs.get("extra_body")
    agent._last_sent_service_tier = kwargs.get("service_tier") or (
        extra_body.get("service_tier") if isinstance(extra_body, dict) else None
    )


def begin_turn(agent: Any, conversation_history: Any) -> None:
    """Open (or refuse) the fast window at a user-turn boundary."""
    agent._fast_turn_has_history = any(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        for m in (conversation_history or ())
    )
    mode = effective_service_tier(agent)
    agent._fast_until = 0.0
    if mode not in BOUNDED_MODES:
        return
    if mode == "cold" and agent._fast_turn_has_history:
        return
    try:
        window = float(getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    agent._fast_until = time.monotonic() + max(window, 0.0)


def effective_request_overrides(agent: Any) -> dict[str, Any]:
    """Build route-safe request overrides without carrying the primary tier into a fallback."""
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    fallback_active = bool(getattr(agent, "_fallback_activated", False) and
                           getattr(agent, "_fallback_service_tier_entry", None) is not None)
    mode = effective_service_tier(agent)
    if fallback_active:
        overrides.pop("service_tier", None)
        overrides.pop("speed", None)
    if (fallback_active and mode == "priority") or (
        mode in BOUNDED_MODES and time.monotonic() < getattr(agent, "_fast_until", 0.0)
    ):
        from hermes_cli.models import resolve_fast_mode_overrides
        base_url = getattr(agent, "base_url", None)
        if getattr(agent, "api_mode", None) == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        fast_override = resolve_fast_mode_overrides(
            getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url)
        if fast_override:
            overrides.update(fast_override)
        elif fallback_active:
            _warn_unsupported_fallback_route(agent, mode)
    if "speed" in overrides and getattr(agent, "model", None) in (getattr(agent, "_fast_mode_unavailable_models", None) or ()):
        overrides.pop("speed", None)
    return overrides


def fast_mode_unprovisioned(api_error: Any, api_kwargs: Any) -> bool:
    """True for a 429 on a ``speed: "fast"`` request whose fast-mode limit header is 0. The
    organization has no fast capacity for the model, so waiting or rotating keys cannot help."""
    if getattr(api_error, "status_code", None) != 429 or not isinstance(api_kwargs, dict):
        return False
    if (api_kwargs.get("extra_body") or {}).get("speed") != "fast":
        return False
    headers = getattr(getattr(api_error, "response", None), "headers", None)
    if headers is None:
        return False
    return any(str(headers.get(name, "")).strip() == "0" for name in _FAST_LIMIT_HEADERS)


def mark_fast_mode_unavailable(agent: Any) -> bool:
    """Stop sending ``speed`` for the current model for the rest of the session. False when the
    model was already marked, so the caller retries at most once per model."""
    model = getattr(agent, "model", None)
    unavailable = getattr(agent, "_fast_mode_unavailable_models", None)
    if not isinstance(unavailable, set):
        unavailable = agent._fast_mode_unavailable_models = set()
    if not model or model in unavailable:
        return False
    unavailable.add(model)
    return True
