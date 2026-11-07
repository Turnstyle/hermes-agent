"""Which persisted session model may pin an API turn's runtime (fallback-aware).

The native turn runner persists the model a turn actually SERVED onto its session row, with
``model_config.gateway_runtime.fallback_active`` when a provider fallback served it
(``GatewayTurnPrepareMixin._sync_session_model_from_agent``). Authority-owned API sessions run
through that runner, so a fallback-served row model must not become the session's standing
selection (the session would stay on the fallback for good), and a row model the current
single-vendor provider cannot serve (``claude-...`` persisted on an ``openai-codex`` session) must
not be replayed onto it. Either way the turn falls through to the normal request/global selection.
"""

import logging

logger = logging.getLogger(__name__)


def served_on_fallback(model_config) -> bool:
    """True when the row's persisted runtime says a provider fallback served its model."""
    runtime = model_config.get("gateway_runtime") if isinstance(model_config, dict) else None
    return isinstance(runtime, dict) and bool(runtime.get("fallback_active"))


def stored_model_pins(stored, model_config) -> bool:
    if stored and served_on_fallback(model_config):
        logger.info("api_server session row model skipped: model=%s reason=fallback_active", stored)
        return False
    return bool(stored)


def row_model_pins(row_model, provider, session_key) -> bool:
    """Whether a session-persisted raw model may pin this turn on *provider*."""
    if not row_model:
        return False
    from hermes_cli.models_detect import model_foreign_to_provider
    if not model_foreign_to_provider(row_model, provider or ""):
        return True
    logger.info("api_server session row model skipped: session=%s row_model=%s provider=%s reason=foreign_provider",
                session_key or "", row_model, provider)
    return False
