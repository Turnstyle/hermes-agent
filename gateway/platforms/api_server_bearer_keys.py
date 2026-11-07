"""API server bearer-key policy: primary key, explicit host-key opt-in, named callers, extra keys.

``API_SERVER_EXTRA_KEYS`` (a secret) accepts a JSON array of at most 16 URL-safe keys, or one raw
URL-safe key for a native secret-source mapping. The entire supplemental set is validated before
any comparison; invalid configuration grants no extra access and the primary key and explicit
host fallback stay unchanged. Extra keys apply only to a routed (``/p/<profile>/``) profile scope.
"""

import hmac
import json
import re

_MAX_KEYS = 16
_MAX_ENCODED_LENGTH = 70000
_KEY = re.compile(r"[A-Za-z0-9._~+/=-]{16,4096}\Z")


def accept_host_key_for_profiles(adapter) -> bool:
    """The host adapter's startup policy, never the request profile's config.

    Only a literal YAML boolean true (``platforms.api_server.extra.accept_host_key_for_profiles``)
    opts in; strings and numbers keep profile isolation.
    """
    return (adapter.config.extra or {}).get("accept_host_key_for_profiles") is True


def _profile_extra_keys() -> tuple[str, ...] | None:
    """Read only the active profile scope; None means invalid supplemental config."""
    from agent.secret_scope import get_secret
    from hermes_cli.auth import has_usable_secret

    try:
        raw = get_secret("API_SERVER_EXTRA_KEYS", "")
    except Exception:  # health: allow BLE001 -- an unscoped read grants no extra access
        return None
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, str) or len(raw) > _MAX_ENCODED_LENGTH:
        return None
    # Check the raw form first: an all-digit key is also a JSON scalar.
    if _KEY.fullmatch(raw):
        values = [raw]
    else:
        try:
            values = json.loads(raw)
        except (ValueError, RecursionError):
            return None
    if not isinstance(values, list) or len(values) > _MAX_KEYS:
        return None
    keys = []
    for key in values:
        if (not isinstance(key, str) or not _KEY.fullmatch(key)
                or not has_usable_secret(key, min_length=16)):
            return None
        keys.append(key)
    return tuple(keys)


def _matches_profile_extra_key(token: str, profile: str | None, primary: str) -> bool:
    if not token or not primary or not profile or profile == "default":
        return False
    keys = _profile_extra_keys()
    if keys is None:
        from gateway.platforms.api_server import logger

        logger.warning(
            "Invalid supplemental API key configuration for profile %r; extra access denied", profile)
        return False
    matched = False
    presented = token.encode()
    for key in keys:
        # Do not short-circuit: compare every validated supplemental key.
        matched |= hmac.compare_digest(presented, key.encode())
    return matched


def _host_key_accepted(adapter, request, token, profile, expected_key) -> bool:
    """The host key on a routed profile, only under the explicit opt-in (logged loudly)."""
    host_key = adapter._api_key or ""
    routed = bool(profile and profile != "default")
    if not (routed and token and host_key and accept_host_key_for_profiles(adapter)):
        return False
    if not hmac.compare_digest(token.encode(), host_key.encode()):
        return False
    from gateway.platforms.api_server import logger
    logger.warning(
        "API server accepted HOST key for profile %r (api_server.accept_host_key_for_profiles is "
        "enabled; profile-scoped key %s). Next step: configure profile-scoped API_SERVER_KEY; %s",
        profile, "differs" if expected_key else "missing", adapter._request_audit_log_suffix(request))
    return True


def check_auth(adapter, request):
    """``APIServerAdapter._check_auth``: None when the bearer is accepted, else the 401.

    Order: the URL-selected profile's own key; the host key for a routed profile only under the
    explicit opt-in; a named caller; a routed profile's supplemental key. The no-key branch
    (``connect()`` refuses to start without API_SERVER_KEY) exists for tests/manual wiring on the
    default listener only; named profiles fail closed rather than inherit the owner's key.
    """
    from gateway.platforms.api_server import _api_request_profile, logger
    from gateway.platforms.api_server_caller_keys import (
        SHARED_CALLER, eligible_caller_keys, mark_request, match_caller)

    mark_request(None)
    profile = _api_request_profile.get()
    expected_key = adapter._expected_api_key()
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    eligible = eligible_caller_keys(adapter, profile) if token else ()
    shared = SHARED_CALLER if eligible else None
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str, and the token is raw
    # client input — a stray byte must 401, not 500.
    if token and expected_key and hmac.compare_digest(token.encode(), expected_key.encode()):
        mark_request(shared)
        return None
    if _host_key_accepted(adapter, request, token, profile, expected_key):
        mark_request(shared)
        return None
    caller = match_caller(token, eligible)
    if caller is not None:
        mark_request(caller)
        return None
    if _matches_profile_extra_key(token, profile, expected_key):
        return None
    if not expected_key:
        if not (profile and profile != "default"):
            return None
        logger.warning(
            "API server rejected request for profile %r: no profile-scoped "
            "API_SERVER_KEY is configured; %s",
            profile, adapter._request_audit_log_suffix(request))
        return adapter._auth_failed_response()
    logger.warning("API server rejected invalid API key: %s", adapter._request_audit_log_suffix(request))
    return adapter._auth_failed_response()
