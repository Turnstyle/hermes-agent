"""Regression for #61276: api_server agent entry under multiplex isolation.

When gateway.multiplex_profiles is on, get_secret fails closed without a
profile secret scope. Requests with a ``/p/<profile>/`` prefix are scoped by
``_profile_scope(profile)``, but plain requests on the default listener used
to get ``nullcontext()`` — so agent runs crashed with UnscopedSecretError on
their first credential read (e.g. OPENROUTER_BASE_URL). ``_profile_scope``
now enters the DEFAULT profile's runtime scope when multiplex is active and
no profile was requested.

Adapted from PR #61283 by @giggling-ginger (originally targeting a
pre-``_profile_scope`` helper); no live gateway or network.
"""

from __future__ import annotations

import pytest

from agent import secret_scope as ss
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def adapter():
    return APIServerAdapter(PlatformConfig(enabled=True))


class TestProfileScopeDefaultFallback:
    def test_noop_when_multiplex_off(self, adapter, monkeypatch):
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://from-environ.example/v1")
        with adapter._profile_scope(None):
            # Legacy single-profile path: unscoped get_secret reads os.environ.
            assert ss.get_secret("OPENROUTER_BASE_URL") == "https://from-environ.example/v1"
        assert ss.current_secret_scope() is None


# Regression coverage for #72041: profile-bound API authentication
class TestProfileScopedApiAuthentication:
    @staticmethod
    def _request(token: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"},
            remote="127.0.0.1",
            transport=None,
            method="GET",
            path_qs="/p/worker/v1/models",
        )

    def test_named_profile_rejects_default_listener_key(
        self, adapter, tmp_path, monkeypatch
    ):
        from gateway.platforms.api_server import _api_request_profile

        profile_home = tmp_path / "profiles" / "worker"
        profile_home.mkdir(parents=True)
        profile_key = "worker-profile-api-key-123456"
        default_key = "default-listener-api-key-123456"
        (profile_home / ".env").write_text(
            f"API_SERVER_KEY={profile_key}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: profile_home,
        )
        adapter._api_key = default_key
        ss.set_multiplex_active(True)

        profile_token = _api_request_profile.set("worker")
        try:
            with adapter._profile_scope("worker"):
                assert adapter._check_auth(self._request(profile_key)) is None

                rejected = adapter._check_auth(self._request(default_key))
                assert rejected is not None
                assert rejected.status == 401
        finally:
            _api_request_profile.reset(profile_token)

    def test_switch_off_rejects_host_key(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms.api_server import _api_request_profile

        profile_home = tmp_path / "profiles" / "worker"
        profile_home.mkdir(parents=True)
        default_key = "default-listener-api-key-123456"
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
        monkeypatch.setattr(adapter, "_accept_host_key_for_profiles", lambda: False)
        adapter._api_key = default_key
        ss.set_multiplex_active(True)

        profile_token = _api_request_profile.set("worker")
        try:
            with adapter._profile_scope("worker"):
                # Profile has no key configured, switch is off: reject host key
                rejected = adapter._check_auth(self._request(default_key))
                assert rejected is not None
                assert rejected.status == 401
        finally:
            _api_request_profile.reset(profile_token)

    def test_switch_on_accepts_host_key_with_warning(self, adapter, tmp_path, monkeypatch, caplog):
        import logging
        from gateway.platforms.api_server import _api_request_profile

        profile_home = tmp_path / "profiles" / "worker"
        profile_home.mkdir(parents=True)
        default_key = "default-listener-api-key-123456"
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
        monkeypatch.setattr(adapter, "_accept_host_key_for_profiles", lambda: True)
        adapter._api_key = default_key
        ss.set_multiplex_active(True)

        profile_token = _api_request_profile.set("worker")
        try:
            with adapter._profile_scope("worker"):
                with caplog.at_level(logging.WARNING):
                    caplog.clear()
                    accepted = adapter._check_auth(self._request(default_key))
                    assert accepted is None
                    assert any("API server accepted HOST key for profile 'worker'" in r.message for r in caplog.records)
                    assert any("Bot Maker or Keys Router" in r.message for r in caplog.records)
                    # Must never log key values
                    for r in caplog.records:
                        assert default_key not in r.message
        finally:
            _api_request_profile.reset(profile_token)

    def test_invalid_key_always_denied(self, adapter, tmp_path, monkeypatch):
        from gateway.platforms.api_server import _api_request_profile

        profile_home = tmp_path / "profiles" / "worker"
        profile_home.mkdir(parents=True)
        default_key = "default-listener-api-key-123456"
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
        adapter._api_key = default_key
        ss.set_multiplex_active(True)

        profile_token = _api_request_profile.set("worker")
        try:
            with adapter._profile_scope("worker"):
                for switch_val in (False, True):
                    monkeypatch.setattr(adapter, "_accept_host_key_for_profiles", lambda: switch_val)
                    rejected = adapter._check_auth(self._request("completely-invalid-key-9999999"))
                    assert rejected is not None
                    assert rejected.status == 401
        finally:
            _api_request_profile.reset(profile_token)

    def test_scoped_key_works(self, adapter, tmp_path, monkeypatch, caplog):
        import logging
        from gateway.platforms.api_server import _api_request_profile

        profile_home = tmp_path / "profiles" / "worker"
        profile_home.mkdir(parents=True)
        profile_key = "worker-profile-api-key-123456"
        default_key = "default-listener-api-key-123456"
        (profile_home / ".env").write_text(f"API_SERVER_KEY={profile_key}\n", encoding="utf-8")
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
        adapter._api_key = default_key
        ss.set_multiplex_active(True)

        profile_token = _api_request_profile.set("worker")
        try:
            with adapter._profile_scope("worker"):
                for switch_val in (False, True):
                    monkeypatch.setattr(adapter, "_accept_host_key_for_profiles", lambda: switch_val)
                    with caplog.at_level(logging.WARNING):
                        caplog.clear()
                        assert adapter._check_auth(self._request(profile_key)) is None
                        # Profile key should not trigger host-key warning
                        assert not any("accepted HOST key" in r.message for r in caplog.records)
        finally:
            _api_request_profile.reset(profile_token)


@pytest.mark.asyncio
async def test_profile_middleware_binds_auth_before_handler(
    adapter, tmp_path, monkeypatch
):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import GatewayConfig
    from gateway.platforms.api_server import _api_request_profile

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    profile_key = "a" * 32
    default_key = "b" * 32
    (worker_home / ".env").write_text(
        f"API_SERVER_KEY={profile_key}\n", encoding="utf-8"
    )
    adapter._api_key = default_key
    adapter.gateway_runner = type(
        "_Runner", (), {"config": GatewayConfig(multiplex_profiles=True)}
    )()
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path), ("worker", worker_home)
        ],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path if name == "default" else worker_home,
    )
    ss.set_multiplex_active(True)

    async def authenticated(request):
        auth_error = adapter._check_auth(request)
        if auth_error is not None:
            return auth_error
        return web.json_response(
            {"profile": _api_request_profile.get() or "default"}
        )

    app = web.Application(
        middlewares=[adapter._make_profile_prefix_middleware()]
    )
    app.router.add_get("/v1/test", authenticated)
    app.router.add_get("/p/{profile}/v1/test", authenticated)

    async with TestClient(TestServer(app)) as client:
        default_response = await client.get(
            "/v1/test",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_response.status == 200

        default_alias = await client.get(
            "/p/default/v1/test",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert default_alias.status == 200

        rejected = await client.get(
            "/p/worker/v1/test",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert rejected.status == 401

        accepted = await client.get(
            "/p/worker/v1/test",
            headers={"Authorization": f"Bearer {profile_key}"},
        )
        assert accepted.status == 200
        assert (await accepted.json())["profile"] == "worker"



@pytest.mark.parametrize("value", [None, False, "false", "0", "true", 0, 1, True])
def test_host_key_policy_requires_literal_boolean(adapter, monkeypatch, value):
    from gateway.platforms.api_server import _api_request_profile

    adapter.config = PlatformConfig.from_dict({"accept_host_key_for_profiles": value})
    adapter._api_key = "synthetic-host-key-123456789"
    monkeypatch.setattr(adapter, "_expected_api_key", lambda: "")
    # Auth must never read configuration inside the selected request profile.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: pytest.fail("auth read profile config"))
    selected = _api_request_profile.set("worker")
    try:
        response = adapter._check_auth(TestProfileScopedApiAuthentication._request(adapter._api_key))
        if value is True:
            assert response is None
        else:
            assert response is not None and response.status == 401
    finally:
        _api_request_profile.reset(selected)


@pytest.mark.parametrize("explicit", [None, False, True, "false"])
def test_host_key_policy_uses_gateway_startup_merge(adapter, explicit):
    from gateway.config_loader import merge_platform_sections
    from gateway.platforms.api_server import _api_request_profile

    gateway = {"api_server": {"accept_host_key_for_profiles": True}}
    if explicit is not None:
        gateway["api_server"]["extra"] = {"accept_host_key_for_profiles": explicit}
    merged = merge_platform_sections({"gateway": gateway}, gateway, {})
    adapter.config = PlatformConfig.from_dict(merged["api_server"])
    for profile in (None, "default", "worker"):
        selected = _api_request_profile.set(profile)
        try:
            assert adapter._accept_host_key_for_profiles() is (explicit is None or explicit is True)
        finally:
            _api_request_profile.reset(selected)


def test_host_key_policy_default_is_off(adapter):
    assert adapter._accept_host_key_for_profiles() is False
