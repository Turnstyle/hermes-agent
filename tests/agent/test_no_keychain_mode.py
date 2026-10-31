"""``auth.no_keychain``: a profile's bots never read an OS credential store or a borrowed login.

Synthetic only: no live auth, keychain, subprocess or network (``subprocess.run`` is armed to fail).
"""
import json
import os
import sys
import time
from unittest.mock import patch

import pytest

import hermes_constants as hc
from agent import keychain_policy as policy
from agent import anthropic_credentials as ac
from agent import credential_pool as cp
from agent.secret_scope import set_secret_scope, reset_secret_scope
from hermes_cli import git_credentials as gc, copilot_auth as ca, main_desktop_tcc as tcc
from hermes_cli import _subprocess_compat as compat

SYNTHETIC = "sk-ant-oat01-synthetic-unit-test-not-a-real-token"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Private credential-file policy is POSIX-specific")


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    home_token = hc.set_hermes_home_override(home)
    scope_token = set_secret_scope({})
    (home / "config.yaml").write_text("auth:\n  no_keychain: true\n")
    with patch("subprocess.run", side_effect=AssertionError("No subprocess is permitted in synthetic tests")):
        yield home
    reset_secret_scope(scope_token)
    hc.reset_hermes_home_override(home_token)


def _write_access_file(home, data=None):
    directory = home / "credentials"
    directory.mkdir(mode=0o700)
    target = directory / "anthropic.json"
    target.write_text(json.dumps(data if data is not None else {"accessToken": SYNTHETIC}))
    target.chmod(0o600)
    return target


def test_missing_source_fails_loudly(home):
    with pytest.raises(policy.NoKeychainError, match="Keys Router"):
        ac.resolve_anthropic_token()


def test_injected_source_wins_without_borrowing(home):
    token = set_secret_scope({"CLAUDE_CODE_OAUTH_TOKEN": SYNTHETIC})
    try:
        with patch.object(ac, "read_claude_code_credentials", side_effect=AssertionError("borrowed read")), \
                patch.object(cp, "read_credential_pool", side_effect=AssertionError("root pool read")):
            assert ac.resolve_anthropic_token(model="synthetic") == SYNTHETIC
            selected = cp.load_pool("anthropic").select()
            assert selected is not None and selected.access_token == SYNTHETIC
    finally:
        reset_secret_scope(token)


def test_private_access_file_works(home):
    _write_access_file(home, {"accessToken": SYNTHETIC, "expiresAt": int(time.time() * 1000) + 3600000})
    assert ac.resolve_anthropic_token() == SYNTHETIC
    with patch.object(cp, "read_credential_pool", side_effect=AssertionError("shared pool read")), \
            patch.object(cp.auth_mod, "heal_forked_single_use_oauth_grants", side_effect=AssertionError("shared heal")), \
            patch.object(cp, "persist_pool_entries", side_effect=AssertionError("shared persist")):
        pool = cp.load_pool("anthropic")
        selected = pool.select()
        assert selected is not None and selected.source == "no_keychain_file"
        pool._persist()


def test_no_claude_reads(home):
    assert ac._find_claude_code_keychain_item() is None
    assert ac._read_claude_code_keychain_payload() is None
    assert ac._read_claude_code_credentials_from_file() is None
    assert ac.read_claude_code_credentials() is None
    assert ac.read_hermes_oauth_credentials() is None
    assert ac._resolve_claude_code_token_from_credentials({"accessToken": SYNTHETIC}) is None
    ac._mirror_claude_code_credentials_to_keychain("synthetic", "synthetic", 0, spent_refresh_token="synthetic")


@pytest.mark.parametrize("fn,args", [
    (ac.refresh_anthropic_oauth_pure, ("synthetic-refresh",)),
    (ac._refresh_oauth_token, ({"refreshToken": "synthetic"},)),
    (ac._write_claude_code_credentials, ("synthetic", "synthetic", 0)),
    (ac._write_hermes_oauth_credentials, ("synthetic", "synthetic", 0)),
    (ac.run_oauth_setup_token, ()),
], ids=lambda value: getattr(value, "__name__", "args"))
def test_no_rotation_or_writes(home, fn, args):
    with pytest.raises(policy.NoKeychainError):
        fn(*args)


def test_stale_pool_cannot_lease_or_refresh_borrowed_grant(home):
    row = cp.PooledCredential(provider="anthropic", id="old", label="synthetic", auth_type="oauth", priority=0,
                              source="claude_code", access_token=SYNTHETIC, refresh_token="synthetic-refresh")
    pool = cp.CredentialPool("anthropic", [row])
    assert pool._available_entries() == ([], [])
    with pytest.raises(policy.NoKeychainError):
        pool._refresh_entry(row, force=True)


def test_refresh_material_is_rejected(home):
    _write_access_file(home, {"accessToken": SYNTHETIC, "refreshToken": "synthetic"})
    with pytest.raises(policy.NoKeychainError, match="refresh grant"):
        ac.resolve_anthropic_token()


def test_file_and_directory_modes_are_required(home):
    target = _write_access_file(home)
    for path, bad_mode in [(target, 0o644), (target.parent, 0o755)]:
        path.chmod(bad_mode)
        with pytest.raises(policy.NoKeychainError):
            ac.resolve_anthropic_token()
        path.chmod(0o600 if path == target else 0o700)


def test_leaf_and_directory_symlinks_are_rejected(home):
    outside = home / "outside"
    outside.write_text(json.dumps({"accessToken": SYNTHETIC}))
    outside.chmod(0o600)
    directory = home / "credentials"
    directory.mkdir(mode=0o700)
    leaf = directory / "anthropic.json"
    leaf.symlink_to(outside)
    with pytest.raises(policy.NoKeychainError):
        ac.resolve_anthropic_token()
    leaf.unlink()
    directory.rmdir()
    directory.symlink_to(home, target_is_directory=True)
    with pytest.raises(policy.NoKeychainError):
        ac.resolve_anthropic_token()


def test_fifo_does_not_block(home):
    directory = home / "credentials"
    directory.mkdir(mode=0o700)
    os.mkfifo(directory / "anthropic.json", mode=0o600)
    with pytest.raises(policy.NoKeychainError):
        ac.resolve_anthropic_token()


def test_expired_and_malformed_file_fail(home):
    target = _write_access_file(home, {"accessToken": SYNTHETIC, "expiresAt": 1})
    with pytest.raises(policy.NoKeychainError, match="expired"):
        ac.resolve_anthropic_token()
    for text in ["broken-json", "[]", '{"accessToken":"synthetic", "expiresAt":NaN}',
                 '{"accessToken":"synthetic", "expiresAt":true}']:
        target.write_text(text)
        with pytest.raises(policy.NoKeychainError):
            ac.resolve_anthropic_token()


@pytest.mark.parametrize("text", ['auth: [', 'auth:\n  no_keychain: "false"\n', '[]', 'auth: true'])
def test_config_failure_never_reenables_legacy(home, text):
    (home / "config.yaml").write_text(text)
    with pytest.raises(policy.NoKeychainError):
        policy.no_keychain_enabled()
    # Never-raising probes fail closed instead: an unreadable policy forbids the OS store too.
    assert policy.keychain_access_forbidden() is True
    assert gc._gh_cli_token() is None


def test_git_and_copilot_do_not_probe_credential_stores(home, monkeypatch):
    assert gc._gh_cli_token() is None
    assert gc._credential_fill("https://example.test") is None
    monkeypatch.setattr(ca, "_gh_cli_token_cache", (time.monotonic(), "synthetic-cache"))
    assert ca._try_gh_cli_token() is None
    assert ca._probe_gh_cli_token() is None
    assert list(gc.iter_git_basic_auth("https://example.test/repo")) == []
    token = set_secret_scope({"GH_TOKEN": "synthetic-gh-token"})
    try:
        # An explicit scoped token is not an OS store: it still authenticates.
        assert list(gc.iter_git_basic_auth("https://github.com/example/repo")) == [
            ("GITHUB_TOKEN/GH_TOKEN", ("x-access-token", "synthetic-gh-token"))]
    finally:
        reset_secret_scope(token)


def test_signing_keychain_operations_are_disabled(home, capsys):
    assert tcc._macos_codesigning_identity_valid("security", "synthetic") is False
    assert tcc._macos_create_signing_identity("openssl", "security", "codesign", "synthetic", "synthetic") is False
    assert tcc._desktop_macos_setup_tcc_identity("synthetic") is False
    assert "auth.no_keychain forbids keychain signing-identity setup" in capsys.readouterr().out


def _git_config_pairs(env):
    return {env[k]: env[k.replace("KEY", "VALUE")] for k in env if k.startswith("GIT_CONFIG_KEY_")}


def test_updater_resets_credential_helper_in_mode(home):
    with patch.object(compat, "_user_safe_directories", return_value=[]):
        env = compat.no_prompt_git_kwargs()["env"]
    assert _git_config_pairs(env).get("credential.helper") == ""


@pytest.mark.parametrize("config_text,helpers_reset", [
    ("auth:\n  no_keychain: true\n", True),
    ("auth:\n  adopt_external_logins: false\n", False),
])
def test_updater_without_policy_dependencies_remains_safe(home, config_text, helpers_reset):
    """Pre-dependency update bootstrap (no YAML): a profile naming the policy fails closed; one
    that never names it keeps upstream helper behaviour (a private-fork origin still authenticates)."""
    import builtins
    original = builtins.__import__
    (home / "config.yaml").write_text(config_text)

    def bootstrap_import(name, *args, **kwargs):
        if name == "agent.keychain_policy":
            raise ModuleNotFoundError("synthetic bootstrap without YAML dependencies")
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=bootstrap_import), \
            patch.object(compat, "_user_safe_directories", return_value=[]):
        env = compat.no_prompt_git_kwargs()["env"]
    assert (_git_config_pairs(env).get("credential.helper") == "") is helpers_reset


def test_policy_off_keeps_owner_helpers_and_pool(home):
    """Default/legacy profiles keep upstream behaviour: helpers still run, the pool still loads."""
    (home / "config.yaml").write_text("auth:\n  no_keychain: false\n")
    assert policy.no_keychain_enabled() is False
    env = compat.no_prompt_git_kwargs()["env"]
    assert "credential.helper" not in _git_config_pairs(env)
    with patch.object(cp, "read_credential_pool", return_value=[]) as read_pool:
        cp.load_pool("anthropic")
    assert read_pool.called
    (home / "config.yaml").unlink()
    assert policy.no_keychain_enabled() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Legacy keychain behavior is macOS-specific")
# Opt out of the suite-wide reader stub only: ``security`` itself stays mocked below, and the
# ``home`` fixture still fails any other subprocess, so no real Keychain is ever touched.
@pytest.mark.allow_macos_keychain
def test_legacy_mode_preserves_existing_keychain_behavior(home):
    (home / "config.yaml").write_text("auth:\n  no_keychain: false\n")
    assert policy.no_keychain_enabled() is False
    with patch.object(ac.subprocess, "run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"claudeAiOauth": {"accessToken": SYNTHETIC}})
        record = ac._read_claude_code_credentials_from_keychain()
        assert record is not None and record["accessToken"] == SYNTHETIC
        assert run.call_count == 1
