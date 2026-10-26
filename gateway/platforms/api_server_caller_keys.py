"""Named bearer identities for the API server; credentials never enter logs or request bodies.

``API_SERVER_CALLER_KEYS`` (a secret, read from the profile's ``.env`` / secret sources) is a JSON
object of caller name -> URL-safe key (16+ characters). Names are ASCII identifiers, not display
text. An invalid or ambiguous map grants no named access. Each scope (host root, routed profile)
is independent of the other and of the primary/host keys.

A verified identity labels the turn and replaces any author the body asserts; it never widens
access: room grants, run ownership and session admission stay with their native gates.
"""

import hmac
import json
import re
from contextvars import ContextVar
from functools import wraps
from typing import Any, Optional

_SETTING = "API_SERVER_CALLER_KEYS"
# The identity bound to a request authenticated by the shared primary/host key while named callers
# are configured for that scope: it is labelled too, so a shared-key holder cannot pass its text off
# as a named caller's. A scope with no named callers keeps the unlabelled native shape.
SHARED_CALLER = "host-key (shared)"
_MAX_CALLERS = 16
_MAX_ENCODED_LENGTH = 70000
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_KEY = re.compile(r"[A-Za-z0-9._~+/=-]{16,4096}\Z")
_verified_caller: ContextVar[Optional[str]] = ContextVar("api_server_verified_caller", default=None)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate caller")
        result[name] = value
    return result


def parse_caller_keys(raw):
    """Return a fully validated ``((name, key), ...)``, or None; never accept a partial map.
    Duplicate names and one key shared by two names are ambiguous and fail closed."""
    from hermes_cli.auth import has_usable_secret

    if raw is None or raw == "":
        return ()
    if not isinstance(raw, str) or len(raw) > _MAX_ENCODED_LENGTH:
        return None
    try:
        values = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None
    if not isinstance(values, dict) or len(values) > _MAX_CALLERS:
        return None
    result = []
    seen = set()
    for name, key in values.items():
        if (not _NAME.fullmatch(name) or not isinstance(key, str)
                or not _KEY.fullmatch(key) or key in seen
                or not has_usable_secret(key, min_length=16)):
            return None
        seen.add(key)
        result.append((name, key))
    return tuple(result)


def read_host_caller_keys():
    """Snapshot the launch home's ``.env``/secret sources: the profile the default listener serves,
    never a routed profile's scope (the adapter may be built inside one). The process environment
    is that launch home's own injection (systemd, ``op run``), so it is the fallback."""
    from agent.secret_scope import build_profile_secret_scope
    from hermes_constants import get_routing_process_hermes_home
    import os

    try:
        raw = build_profile_secret_scope(get_routing_process_hermes_home()).get(_SETTING)
        if raw is None:
            raw = os.environ.get(_SETTING, "")
        return parse_caller_keys(raw)
    except Exception:  # health: allow BLE001 -- an unreadable host map grants no named access
        return None


def _profile_caller_keys():
    from agent.secret_scope import get_secret

    try:
        return parse_caller_keys(get_secret(_SETTING, ""))
    except Exception:  # health: allow BLE001 -- an unscoped/unreadable map grants no named access
        return None


def eligible_caller_keys(adapter, profile) -> tuple:
    """``(name, key)`` pairs a request to *profile* may present: the host root's map on the default
    listener (or a routed profile under the explicit host-key opt-in), plus the routed profile's own."""
    from gateway.platforms.api_server_bearer_keys import accept_host_key_for_profiles

    routed = bool(profile and profile != "default")
    pairs = []
    if not routed or accept_host_key_for_profiles(adapter):
        pairs.extend(getattr(adapter, "_host_caller_keys", None) or ())
    if routed:
        pairs.extend(_profile_caller_keys() or ())
    return tuple(pairs)


def match_caller(token, eligible):
    """Compare every eligible key. Conflicting identities fail closed (None)."""
    if not token:
        return None
    matches = set()
    presented = token.encode()
    for name, key in eligible:
        if hmac.compare_digest(presented, key.encode()):
            matches.add(name)
    return next(iter(matches)) if len(matches) == 1 else None


def mark_request(name):
    _verified_caller.set(name)


def verified_caller() -> Optional[str]:
    return _verified_caller.get()


def caller_request_context(handler):
    """Reset identity at request entry/exit; detached tasks keep their own copy."""
    @wraps(handler)
    async def wrapped(self, request, *args, **kwargs):
        token = _verified_caller.set(None)
        try:
            return await handler(self, request, *args, **kwargs)
        finally:
            _verified_caller.reset(token)
    return wrapped


def request_turn_author(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The server-verified caller when one is bound, else the normalized body ``author`` (None when
    absent or null, ValueError when not an object). The author only labels memory."""
    _, verified = bind_verified_message("")
    if verified is not None:
        return verified
    raw = body.get("author")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("author must be an object")
    from agent.turn_author import parse_turn_author
    return parse_turn_author(raw)


def bind_verified_message(message, author=None):
    """Override every asserted author field and prefix the agent-visible message."""
    name = _verified_caller.get()
    if name is None:
        return message, author
    verified = {"id": "api-caller:" + name, "name": name, "is_bot": True}
    prefix = "[verified caller: " + name + "] "
    # Session chat binds before Bot Chat / authority admission, then _run_agent binds again.
    # Only an already server-bound author permits the idempotent (no second) prefix.
    if isinstance(message, str):
        if author != verified or not message.startswith(prefix):
            message = prefix + message
    elif isinstance(message, list):
        marker = {"type": "text", "text": prefix}
        if author != verified or not message or message[0] != marker:
            message = [marker, *message]
    return message, verified
