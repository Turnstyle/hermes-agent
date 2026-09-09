"""Regression tests: Codex profile-fallback refresh write-through.

Diagnosis (Conductor B, 2026-09-09): ``resolve_codex_runtime_credentials``'s
refresh path locked and wrote ONLY the active profile's own ``auth.json``
(bare ``_auth_store_lock()`` + ``_save_codex_tokens`` writing to
``_auth_file_path()``), even when the token state it refreshed had actually
been read via the global-root fallback (``_load_provider_state_with_source``,
issue #18594). ``openai-codex`` is one of the ``SINGLE_USE_REFRESH_POOL_PROVIDERS``
— its OAuth refresh_token is single-use and rotates on every redemption. Two
profiles sharing an imported/global Codex grant could therefore each pass
their own (distinct, uncontended) profile lock and both redeem the same
refresh_token concurrently, and whichever refreshed had its result written
only into ITS OWN profile — silently forking a profile-local copy that
shadows the (now-consumed) global grant on every subsequent read, while any
sibling profile still on the global fallback keeps a now-dead refresh_token.

The fix threads the resolved ``source_path`` (see
``_load_provider_state_with_source`` / ``_provider_state_transaction``)
through ``_refresh_codex_auth_tokens`` into ``_save_codex_tokens``, and locks
+ writes through to that SOURCE store — the same write-through pattern
``resolve_nous_access_token`` already uses for "nous" via
``_save_provider_state_to_source``. A profile with its OWN
``providers.openai-codex`` entry (the non-imported/non-fallback case) is
unaffected: ``source_path`` there always resolves to the profile's own file.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth import AuthError, resolve_codex_runtime_credentials


def _make_auth_store(providers: dict | None = None, pool: dict | None = None) -> dict:
    store: dict = {"version": 1}
    if providers is not None:
        store["providers"] = providers
    if pool is not None:
        store["credential_pool"] = pool
    return store


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _codex_provider_block(
    *,
    access_token: str = "OLD-ACCESS",
    refresh_token: str = "FAKE-REFRESH-1",
    last_refresh: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "tokens": {"access_token": access_token, "refresh_token": refresh_token},
        "last_refresh": last_refresh,
        "auth_mode": "chatgpt",
    }


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Global root + a named, active profile — mirrors test_auth_profile_fallback.py.

    * Path.home()  -> tmp_path
    * Global root  -> tmp_path/.hermes
    * Profile      -> tmp_path/.hermes/profiles/coder  (active, HERMES_HOME points here)
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return {"global": global_root, "profile": profile_dir}


def _extra_profile(profile_env, name: str) -> Path:
    """Create a second named profile sharing the same global root."""
    other = profile_env["global"] / "profiles" / name
    other.mkdir(parents=True)
    return other


# ---------------------------------------------------------------------------
# 1. Single-profile refresh writes through to the source (global) store.
# ---------------------------------------------------------------------------


def test_single_profile_refresh_writes_through_to_global_source(profile_env, monkeypatch):
    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(
            providers={"openai-codex": _codex_provider_block()},
            pool={
                "openai-codex": [
                    {
                        "id": "seeded",
                        "source": "device_code",
                        "auth_type": "oauth",
                        "access_token": "OLD-ACCESS",
                        "refresh_token": "FAKE-REFRESH-1",
                    },
                ],
            },
        ),
    )
    # Profile has NO local codex entry — it runs entirely on the global fallback.
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        assert refresh_token == "FAKE-REFRESH-1"
        return {
            "access_token": "FAKE-ACCESS-2",
            "refresh_token": "FAKE-REFRESH-2",
            "expires_in": 3600,
        }

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fake_refresh)

    resolved = resolve_codex_runtime_credentials(force_refresh=True)
    assert resolved["api_key"] == "FAKE-ACCESS-2"

    global_data = _read(profile_env["global"] / "auth.json")
    assert global_data["providers"]["openai-codex"]["tokens"]["access_token"] == "FAKE-ACCESS-2"
    assert global_data["providers"]["openai-codex"]["tokens"]["refresh_token"] == "FAKE-REFRESH-2"
    # credential_pool mirror sync happened on the SOURCE (global) store too.
    seeded = next(e for e in global_data["credential_pool"]["openai-codex"] if e["id"] == "seeded")
    assert seeded["access_token"] == "FAKE-ACCESS-2"

    # The profile must NOT have grown a local providers.openai-codex entry —
    # that would be exactly the fork this fix prevents.
    profile_data = _read(profile_env["profile"] / "auth.json")
    assert "openai-codex" not in (profile_data.get("providers") or {})


# ---------------------------------------------------------------------------
# 2. Two profiles refreshing sequentially converge on one grant.
# ---------------------------------------------------------------------------


def test_two_profiles_refreshing_sequentially_converge_on_global_source(profile_env, monkeypatch):
    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(providers={"openai-codex": _codex_provider_block(refresh_token="GEN-0")}),
    )
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))
    profile_b = _extra_profile(profile_env, "reviewer")
    _write(profile_b / "auth.json", _make_auth_store(providers={}))

    calls = []

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        calls.append(refresh_token)
        n = int(refresh_token.split("-")[1]) + 1
        return {
            "access_token": f"AT-GEN-{n}",
            "refresh_token": f"GEN-{n}",
            "expires_in": 3600,
        }

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fake_refresh)

    # Profile A (the active/default HERMES_HOME=profile "coder") refreshes first.
    resolved_a = resolve_codex_runtime_credentials(force_refresh=True)
    assert resolved_a["api_key"] == "AT-GEN-1"

    # Profile B refreshes next — it must pick up the ROTATED refresh_token
    # profile A just wrote to the global source, not the original "GEN-0".
    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    resolved_b = resolve_codex_runtime_credentials(force_refresh=True)
    assert resolved_b["api_key"] == "AT-GEN-2"

    assert calls == ["GEN-0", "GEN-1"], (
        "profile B redeemed a stale/forked refresh_token instead of the one "
        "profile A's refresh rotated into the shared global source"
    )

    global_data = _read(profile_env["global"] / "auth.json")
    assert global_data["providers"]["openai-codex"]["tokens"]["refresh_token"] == "GEN-2"

    # Neither profile forked a local copy — both stayed converged on the
    # global source throughout.
    for p in (profile_env["profile"], profile_b):
        data = _read(p / "auth.json")
        assert "openai-codex" not in (data.get("providers") or {})


# ---------------------------------------------------------------------------
# 3. A newer/independent grant at the source is not clobbered by an
#    unrelated profile's fallback refresh.
# ---------------------------------------------------------------------------


def test_writethrough_does_not_clobber_unrelated_source_state(profile_env, monkeypatch):
    """The write-through merges only providers.openai-codex (+ its pool
    mirror-sync); it must never wipe sibling provider blocks or independent
    pool rows already present in the source store — including state written
    there after this process last loaded it (the fresh re-read under lock is
    what makes this safe; a stale in-memory copy would silently drop it).
    """
    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(
            providers={
                "openai-codex": _codex_provider_block(),
                # Sibling provider — must survive untouched.
                "nous": {"access_token": "nous-at", "refresh_token": "nous-rt"},
            },
            pool={
                "openai-codex": [
                    {
                        "id": "seeded",
                        "source": "device_code",
                        "auth_type": "oauth",
                        "access_token": "OLD-ACCESS",
                        "refresh_token": "FAKE-REFRESH-1",
                    },
                    # Independent account, added via `hermes auth add openai-codex`
                    # at global scope — distinct token material, must be left alone.
                    {
                        "id": "independent",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "independent-at",
                        "refresh_token": "independent-rt",
                    },
                ],
            },
        ),
    )
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        return {"access_token": "NEW-ACCESS", "refresh_token": "NEW-REFRESH", "expires_in": 3600}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fake_refresh)

    resolve_codex_runtime_credentials(force_refresh=True)

    global_data = _read(profile_env["global"] / "auth.json")
    # Sibling provider untouched.
    assert global_data["providers"]["nous"] == {"access_token": "nous-at", "refresh_token": "nous-rt"}
    # Independent pool row untouched.
    pool = global_data["credential_pool"]["openai-codex"]
    independent = next(e for e in pool if e["id"] == "independent")
    assert independent["access_token"] == "independent-at"
    assert independent["refresh_token"] == "independent-rt"
    # The legitimate singleton mirror WAS refreshed.
    seeded = next(e for e in pool if e["id"] == "seeded")
    assert seeded["access_token"] == "NEW-ACCESS"


# ---------------------------------------------------------------------------
# 4. Lock contention serializes concurrent refreshers on the shared source.
# ---------------------------------------------------------------------------


def test_concurrent_profile_refreshes_serialize_on_source_lock(profile_env, monkeypatch):
    """Two profiles force-refreshing at the same instant must be serialized
    by the global-source lock, not race each other with the SAME single-use
    refresh_token.

    Before the fix, each profile only took its OWN (distinct, uncontended)
    profile lock — nothing here would have blocked the two threads from
    calling the (mocked) provider concurrently with an identical, already
    consumed-by-the-other refresh_token, which a real single-use-refresh
    provider rejects with invalid_grant on the second redemption.
    """
    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(providers={"openai-codex": _codex_provider_block(refresh_token="GEN-0")}),
    )
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))
    profile_b = _extra_profile(profile_env, "reviewer")
    _write(profile_b / "auth.json", _make_auth_store(providers={}))

    state = {"rt": "GEN-0", "n": 0}
    state_lock = threading.Lock()

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        with state_lock:
            current = state["rt"]
        if refresh_token != current:
            raise AuthError(
                "refresh token rejected",
                provider="openai-codex",
                code="invalid_grant",
                relogin_required=True,
            )
        # Widen the race window: if the caller weren't holding the source
        # lock across this whole refresh, the other thread's concurrent read
        # would land here and redeem the SAME (soon-to-be-stale) refresh_token.
        time.sleep(0.05)
        with state_lock:
            state["n"] += 1
            new_rt = f"GEN-{state['n']}"
            state["rt"] = new_rt
        return {"access_token": f"AT-{new_rt}", "refresh_token": new_rt, "expires_in": 3600}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fake_refresh)

    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def run_a():
        try:
            results["a"] = resolve_codex_runtime_credentials(force_refresh=True)
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            errors.append(exc)

    def run_b():
        try:
            import hermes_constants

            token = hermes_constants.set_hermes_home_override(profile_b)
            try:
                results["b"] = resolve_codex_runtime_credentials(force_refresh=True)
            finally:
                hermes_constants.reset_hermes_home_override(token)
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            errors.append(exc)

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not errors, f"unexpected error(s) from concurrent refresh: {errors!r}"
    assert not t_a.is_alive() and not t_b.is_alive(), "a refresher deadlocked"

    # Both succeeded — meaning both were serialized onto the correct,
    # sequentially-rotated refresh_token rather than racing on GEN-0 twice.
    assert {results["a"]["api_key"], results["b"]["api_key"]} == {"AT-GEN-1", "AT-GEN-2"}

    global_data = _read(profile_env["global"] / "auth.json")
    assert global_data["providers"]["openai-codex"]["tokens"]["refresh_token"] == "GEN-2"


# ---------------------------------------------------------------------------
# 5. Non-imported credentials: behavior identical to before the fix.
# ---------------------------------------------------------------------------


def test_profile_owned_credential_refresh_unaffected_by_writethrough(profile_env, monkeypatch, tmp_path):
    """A profile with its OWN providers.openai-codex entry never touches the
    global root — source_path always resolves to the profile's own file, so
    this exercises the SAME single-lock code path as before the fix.
    """
    # No global auth.json at all — must never be created by this refresh.
    global_auth = profile_env["global"] / "auth.json"
    assert not global_auth.exists()

    _write(
        profile_env["profile"] / "auth.json",
        _make_auth_store(providers={"openai-codex": _codex_provider_block(refresh_token="PROFILE-OWN-RT")}),
    )

    def fake_refresh(access_token, refresh_token, *, timeout_seconds=20.0):
        assert refresh_token == "PROFILE-OWN-RT"
        return {"access_token": "PROFILE-NEW-AT", "refresh_token": "PROFILE-NEW-RT", "expires_in": 3600}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fake_refresh)

    resolved = resolve_codex_runtime_credentials(force_refresh=True)
    assert resolved["api_key"] == "PROFILE-NEW-AT"

    profile_data = _read(profile_env["profile"] / "auth.json")
    assert profile_data["providers"]["openai-codex"]["tokens"]["access_token"] == "PROFILE-NEW-AT"
    assert profile_data["providers"]["openai-codex"]["tokens"]["refresh_token"] == "PROFILE-NEW-RT"

    # The global root was never created — this profile never fell back.
    assert not global_auth.exists()
