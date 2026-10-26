"""Strict profile-local opt-out of implicit OS credential stores (``auth.no_keychain``).

When the active profile sets ``auth.no_keychain: true``, Hermes never reads the OS keychain,
borrowed CLI logins (Claude Code, ``gh``), git credential helpers, or a stored refresh grant on
that profile's behalf: Anthropic auth is access-only (explicit scoped injection or a private
``credentials/anthropic.json`` snapshot) and the Keys Router owns renewal. The owner's own CLI
sign-ins are untouched — this only stops this profile's bots from borrowing them.
"""
from __future__ import annotations

import json
import os
import stat
import time

from hermes_constants import get_hermes_home
from utils import load_yaml_file_readonly

_ACCESS_ONLY_SOURCES = frozenset({"no_keychain_file", "no_keychain_injected"})


class NoKeychainError(RuntimeError):
    """An explicit credential is required; OS-store fallback is forbidden."""


def no_keychain_enabled() -> bool:
    """Read the active profile's policy; a parse error is never a silent default."""
    path = get_hermes_home() / "config.yaml"
    try:
        config = load_yaml_file_readonly(path)
    except FileNotFoundError:
        return False
    except Exception as exc:
        raise NoKeychainError("Cannot read no-keychain policy from the active profile config.yaml") from exc
    if config is None:
        return False
    if not isinstance(config, dict):
        raise NoKeychainError("Profile config.yaml must be a mapping")
    auth = config.get("auth", {})
    if auth is None:
        return False  # Legacy null auth sections have no explicit opt-in.
    if not isinstance(auth, dict):
        raise NoKeychainError("Profile auth policy must be a mapping")
    value = auth.get("no_keychain", False)
    if not isinstance(value, bool):
        raise NoKeychainError("auth.no_keychain must be a YAML boolean")
    return value


def keychain_access_forbidden() -> bool:
    """:func:`no_keychain_enabled` for never-raising probes: an unreadable policy forbids too."""
    try:
        return no_keychain_enabled()
    except NoKeychainError:
        return True


def reject_keychain_operation(operation: str) -> None:
    if no_keychain_enabled():
        raise NoKeychainError(f"auth.no_keychain forbids {operation}; use a Keys Router-managed access token")


def _read_access_file() -> dict:
    """Read a private, non-symlink leaf through a private directory descriptor."""
    directory = get_hermes_home() / "credentials"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    dir_fd = os.open(directory, flags | os.O_DIRECTORY)
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise NoKeychainError("Profile credentials directory must be owned by this user with mode 0700")
        fd = os.open("anthropic.json", flags, dir_fd=dir_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise NoKeychainError("Profile credentials/anthropic.json must be a user-owned regular file with mode 0600")
            with os.fdopen(fd, encoding="utf-8") as stream:
                fd = -1
                text = stream.read(65537)
            if len(text) > 65536:
                raise NoKeychainError("Profile Anthropic access-token file exceeds the size limit")
            data = json.loads(text)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(dir_fd)
    if not isinstance(data, dict) or set(data) - {"accessToken", "expiresAt"}:
        raise NoKeychainError("Profile Anthropic file accepts only accessToken and optional expiresAt; never copy a refresh grant")
    return data


def access_only_anthropic_credential() -> dict:
    """Explicit scoped injection or a fresh profile access snapshot; never auth.json."""
    from agent.secret_scope import get_secret
    from agent.anthropic_credentials import _is_oauth_token

    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY"):
        token = (get_secret(name, "") or "").strip()
        if token:
            return {"access_token": token, "auth_type": "oauth" if _is_oauth_token(token) else "api_key",
                    "source": "no_keychain_injected", "expires_at_ms": None}
    try:
        data = _read_access_file()
    except NoKeychainError:
        raise
    except Exception as exc:
        raise NoKeychainError("auth.no_keychain needs an injected CLAUDE_CODE_OAUTH_TOKEN or private profile credentials/anthropic.json; ask the Keys Router") from exc
    token = data.get("accessToken")
    expiry = data.get("expiresAt", 0)
    if not isinstance(token, str) or not token.strip():
        raise NoKeychainError("Profile Anthropic access token is missing")
    if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 0:
        raise NoKeychainError("Profile Anthropic expiresAt must be epoch milliseconds")
    if expiry and expiry <= time.time() * 1000 + 60000:
        raise NoKeychainError("Profile Anthropic access token expired; the Keys Router must renew it")
    token = token.strip()
    return {"access_token": token, "auth_type": "oauth" if _is_oauth_token(token) else "api_key",
            "source": "no_keychain_file", "expires_at_ms": expiry or None}


def access_only_pool(provider: str):
    """The Anthropic pool under ``auth.no_keychain`` (None otherwise): one access-only entry, so
    auth.json, the root fallback and any copied refresh family are never healed, read or seeded."""
    if provider != "anthropic" or not no_keychain_enabled():
        return None
    from agent.credential_pool import CredentialPool, PooledCredential

    return CredentialPool(provider, [PooledCredential(
        provider=provider, id="no_keychain", label="Explicit access-only Anthropic token",
        priority=0, **access_only_anthropic_credential(),
    )])


def access_only_entry_usable(entry, now: float) -> bool:
    """Pool-selection filter under the policy: a pool loaded before the cutover cannot lease a
    borrowed grant, and an expired access snapshot fails loudly instead of being refreshed."""
    if entry.source not in _ACCESS_ONLY_SOURCES or entry.refresh_token:
        return False
    if entry.expires_at_ms and entry.expires_at_ms <= (now + 60) * 1000:
        raise NoKeychainError("Anthropic access token expired; the Keys Router must renew it")
    return True
