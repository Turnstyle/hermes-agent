"""Startup must not ask a person to approve an unattended secret fetch.

Protects the loader boundary, including scoped hydration and interactive use.
Regression: cron/launchd/children resolve op references and open a Touch ID prompt.
The context matrix mocks fetch; service-account cases run a fixture executable
through the real registry and subprocess path, without contacting 1Password.
"""
from contextlib import nullcontext
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent.secret_sources.base import FetchResult
from agent.secret_sources.onepassword import OnePasswordSource
from hermes_cli import env_loader

_CONTEXT_MARKERS = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "_HERMES_CRON_EXTERNAL_WORKER",
                    "HERMES_CRON_SESSION", "XPC_SERVICE_NAME", "HERMES_LAUNCHD_LABEL", "HERMES_DESKTOP",
                    "HERMES_GATEWAY_DETACHED")


def _clear_context(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("OP_") or name in _CONTEXT_MARKERS:
            monkeypatch.delenv(name)


@pytest.mark.parametrize("route", ["dotenv", "hydrate"])
@pytest.mark.parametrize("context,tty,argv,marker,load", [
    ("no TTY", False, ["hermes", "--help"], None, False),
    ("launchd", True, ["hermes", "--help"], ("XPC_SERVICE_NAME", "job"), False),
    ("launchd label", True, ["hermes", "chat"], ("HERMES_LAUNCHD_LABEL", "ai.hermes.fixture"), False),
    ("launchd zero", True, ["hermes", "--help"], ("XPC_SERVICE_NAME", "0"), True),
    ("delegated child", True, ["hermes", "--help"], ("HERMES_DELEGATED_CHILD_CONTEXT", "1"), False),
    ("Kanban worker", True, ["hermes", "chat"], ("HERMES_KANBAN_TASK", "task"), False),
    ("cron worker", True, ["hermes", "chat"], ("_HERMES_CRON_EXTERNAL_WORKER", "run"), False),
    ("cron session", True, ["hermes", "chat"], ("HERMES_CRON_SESSION", "1"), False),
    ("interactive", True, ["hermes", "chat"], None, True),
    ("interactive IDE", True, ["hermes", "chat"], ("XPC_SERVICE_NAME", "application.com.microsoft.VSCode.1"), True),
    ("unattended IDE", False, ["hermes", "chat"], ("XPC_SERVICE_NAME", "application.com.microsoft.VSCode.1"), False),
    ("gateway global flags", False, ["hermes", "-p", "sample", "gateway", "run"], None, True),
    ("gateway unchanged", False, ["hermes", "gateway", "run"], None, True),
    ("detached gateway daemon", False, ["hermes", "--profile", "default", "gateway", "run", "--quiet"],
     ("HERMES_GATEWAY_DETACHED", "1"), False),
    ("detached gateway with TTY", True, ["hermes", "gateway", "run"], ("HERMES_GATEWAY_DETACHED", "1"), True),
    ("Desktop backend skips", False, ["hermes", "serve"], ("HERMES_DESKTOP", "1"), False),
    ("Desktop dashboard skips", False, ["hermes", "dashboard"], ("HERMES_DESKTOP", "1"), False),
    ("Desktop backend with TTY", True, ["hermes", "serve"], ("HERMES_DESKTOP", "1"), False),
    ("serve without Desktop", True, ["hermes", "serve"], None, True),
    ("Desktop child", False, ["hermes", "chat"], ("HERMES_DESKTOP", "1"), False),
    ("gateway cron", True, ["hermes", "gateway", "run"], ("HERMES_CRON_SESSION", "1"), False),
    ("in-process child", True, ["hermes", "gateway", "run"], None, False),
    ("context cron", True, ["hermes", "gateway", "run"], None, False),
])
def test_startup_onepassword_context(monkeypatch, tmp_path, capsys, route, context, tty, argv, marker, load):
    _clear_context(monkeypatch)
    if marker:
        monkeypatch.setenv(*marker)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(env_loader, "_ONEPASSWORD_SKIP_LOGGED", False)
    monkeypatch.setenv("STARTUP_TEST_SECRET", "existing")
    (tmp_path / "config.yaml").write_text("secrets:\n  onepassword:\n    enabled: true\n    env:\n      STARTUP_TEST_SECRET: op://Vault/Item/field\n", encoding="utf-8")
    fetch = Mock(return_value=FetchResult(secrets={"STARTUP_TEST_SECRET": "mock-resolved"}))
    monkeypatch.setattr(OnePasswordSource, "fetch", fetch)
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import _VAR_MAP
    scope = delegated_child_context() if context == "in-process child" else nullcontext()
    token = _VAR_MAP["HERMES_CRON_SESSION"].set("1" if context == "context cron" else "")
    # Env cron fallback is masked by the context var; explicitly bind that case too.
    if context in {"cron session", "gateway cron"}:
        _VAR_MAP["HERMES_CRON_SESSION"].reset(token)
        token = _VAR_MAP["HERMES_CRON_SESSION"].set("1")
    env_loader.reset_secret_source_cache()
    try:
        with scope:
            for _ in range(2):
                if route == "dotenv":
                    env_loader.load_hermes_dotenv(hermes_home=tmp_path)
                else:
                    env_loader.hydrate_profile_secret_sources(tmp_path)
        assert fetch.call_count == int(load)
        output = capsys.readouterr().err
        if not load:
            assert output.count("1Password skipped:") == 1
            assert "STARTUP_TEST_SECRET" not in output
            assert "op://" not in output
            assert "mock-resolved" not in output
    finally:
        _VAR_MAP["HERMES_CRON_SESSION"].reset(token)
        env_loader.reset_secret_source_cache()


@pytest.mark.parametrize("route", ["dotenv", "hydrate"])
@pytest.mark.parametrize("context,argv,marker,tty", [
    ("cron", ["hermes", "chat"], ("HERMES_CRON_SESSION", "1"), True),
    ("cron worker", ["hermes", "chat"], ("_HERMES_CRON_EXTERNAL_WORKER", "run"), True),
    ("delegated child", ["hermes", "chat"], ("HERMES_DELEGATED_CHILD_CONTEXT", "1"), True),
    ("headless non-TTY", ["hermes", "chat"], None, False),
    ("Desktop", ["hermes", "serve"], ("HERMES_DESKTOP", "1"), False),
    # The .5 local Desktop's backend: `gateway ensure` spawns this detached daemon.
    ("Desktop gateway ensure daemon", ["hermes", "--profile", "default", "gateway", "run", "--quiet"],
     ("HERMES_GATEWAY_DETACHED", "1"), False),
    ("interactive control", ["hermes", "chat"], None, True),
])
def test_tokenless_unattended_startup_never_runs_op(monkeypatch, tmp_path, route, context, argv, marker, tty):
    """The real registry/source/fetch path down to ``run_cli``: a tokenless unattended start
    never spawns ``op`` (the only path to a desktop-app prompt); the interactive control does."""
    from agent.secret_sources import onepassword as op

    _clear_context(monkeypatch)
    if marker:
        monkeypatch.setenv(*marker)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(env_loader, "_ONEPASSWORD_SKIP_LOGGED", False)
    monkeypatch.delenv("FIXTURE_RESOLVED", raising=False)
    runner = Mock(return_value=Mock(returncode=0, stdout="fixture-resolved\n", stderr=""))
    monkeypatch.setattr(op, "run_cli", runner)
    monkeypatch.setattr(op, "find_op", lambda binary_path="": Path("/fixture/op"))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n  onepassword:\n    enabled: true\n    cache_ttl_seconds: 0\n"
        "    env:\n      FIXTURE_RESOLVED: op://Fixture/Item/field\n", encoding="utf-8")
    env_loader.reset_secret_source_cache()
    try:
        if route == "dotenv":
            env_loader.load_hermes_dotenv(hermes_home=tmp_path)
        else:
            env_loader.hydrate_profile_secret_sources(tmp_path)
        assert runner.called == (context == "interactive control")
    finally:
        os.environ.pop("FIXTURE_RESOLVED", None)
        env_loader.reset_secret_source_cache()


@pytest.mark.parametrize("token_present", [False, True])
def test_safe_worker_snapshot_still_skips_hydration(monkeypatch, tmp_path, token_present):
    """Upstream's frozen worker snapshot owns credentials: neither startup route resolves
    any source, tokened or not (the carried worker-bypass env knob is not needed)."""
    import agent.safe_worker_policy as policy

    _clear_context(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    if token_present:
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "fixture-only-token")
    fetch = Mock(return_value=FetchResult(secrets={"STARTUP_TEST_SECRET": "mock-resolved"}))
    monkeypatch.setattr(OnePasswordSource, "fetch", fetch)
    (tmp_path / "config.yaml").write_text("secrets:\n  onepassword:\n    enabled: true\n    env:\n      STARTUP_TEST_SECRET: op://Vault/Item/field\n", encoding="utf-8")
    monkeypatch.setattr(policy, "_policy", policy._SafeWorkerPolicy(os.getpid(), False, False, "{}"))
    from agent.secret_sources import registry
    monkeypatch.setattr(registry, "apply_all", Mock(side_effect=AssertionError("safe worker rehydrated")))
    env_loader.reset_secret_source_cache()
    try:
        assert env_loader.load_hermes_dotenv(hermes_home=tmp_path) == []
        assert env_loader.hydrate_profile_secret_sources(tmp_path) == {}
        assert fetch.call_count == 0
    finally:
        env_loader.reset_secret_source_cache()


@pytest.mark.parametrize("route,bootstrap", [
    ("dotenv", "env"), ("dotenv", ".env"), ("dotenv", ".op.env"),
    ("hydrate", ".env"), ("hydrate", ".op.env"),
])
@pytest.mark.parametrize("context", ["launchd", "cron", "Kanban", "child", "no TTY"])
@pytest.mark.parametrize("token_kind", ["default", "custom", "missing", "blank", "desktop"])
def test_unattended_service_account_only(monkeypatch, tmp_path, capsys, route, bootstrap, context, token_kind):
    """Real registry → fetch thread → executable; never invoke the installed op CLI."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    _clear_context(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: context != "no TTY")
    markers = {"launchd": ("XPC_SERVICE_NAME", "job"),
               "Kanban": ("HERMES_KANBAN_TASK", "fixture-task")}
    if context in markers:
        monkeypatch.setenv(*markers[context])
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import _VAR_MAP
    token = _VAR_MAP["HERMES_CRON_SESSION"].set("1" if context == "cron" else "")
    monkeypatch.setattr(env_loader, "_ONEPASSWORD_SKIP_LOGGED", False)
    token_name = "FIXTURE_OP_TOKEN" if token_kind == "custom" else "OP_SERVICE_ACCOUNT_TOKEN"
    present = token_kind in {"default", "custom"}
    # This executable exits nonzero unless the only possible route is the service account.
    receipt = tmp_path / "called"
    executable = tmp_path / "fixture-op"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "e = os.environ\n"
        "safe = (e.get('OP_SERVICE_ACCOUNT_TOKEN') == 'fixture-only-token'\n"
        "        and e.get('OP_LOAD_DESKTOP_APP_SETTINGS') == 'false'\n"
        "        and 'OP_ACCOUNT' not in e and 'OPENAI_API_KEY' not in e\n"
        "        and not any(k.startswith(('OP_CONNECT_', 'OP_SESSION_')) for k in e))\n"
        "if not safe: sys.exit(90)\n"
        "if sys.argv[1:] != ['read', '--account', 'explicit-fixture', '--', 'op://Fixture/Item/field']: sys.exit(91)\n"
        f"Path({str(receipt)!r}).write_text('service-account-only')\n"
        "print('fixture-resolved')\n", encoding="utf-8")
    executable.chmod(0o700)
    bootstrap_text = (
        "OP_CONNECT_HOST=https://fixture.invalid\nOP_CONNECT_TOKEN=fixture-connect\n"
        "OP_SESSION_fixture=fixture-session\nOP_ACCOUNT=fixture-account\n"
        "OP_LOAD_DESKTOP_APP_SETTINGS=true\nOPENAI_API_KEY=fixture-provider-secret\n")
    if present or token_kind == "blank":
        bootstrap_text += f"{token_name}={'fixture-only-token' if present else '   '}\n"
    if bootstrap == "env":
        for line in bootstrap_text.splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    else:
        (tmp_path / bootstrap).write_text(bootstrap_text, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "secrets:\n  onepassword:\n    enabled: true\n"
        f"    binary_path: {executable}\n    service_account_token_env: {token_name}\n"
        "    account: explicit-fixture\n    cache_ttl_seconds: 0\n"
        "    env:\n      FIXTURE_RESOLVED: op://Fixture/Item/field\n", encoding="utf-8")
    env_loader.reset_secret_source_cache()
    try:
        with delegated_child_context() if context == "child" else nullcontext():
            if route == "dotenv":
                env_loader.load_hermes_dotenv(hermes_home=tmp_path)
                values = env_loader.get_secret_source_values(tmp_path)
            else:
                values = env_loader.hydrate_profile_secret_sources(tmp_path)
        assert receipt.exists() == present
        assert values.get("FIXTURE_RESOLVED") == ("fixture-resolved" if present else None)
        output = capsys.readouterr().err
        reason = {"launchd": "launchd job", "cron": "cron session", "Kanban": "Kanban worker",
                  "child": "delegated child", "no TTY": "no terminal on standard input"}[context]
        assert (f"1Password skipped: {reason}." in output) == (not present)
        assert "fixture-only-token" not in output
        assert "op://" not in output
        if route == "hydrate" and present:
            # A → B → A: neither an ambient token nor A's private bootstrap may
            # authorize B. A's successful snapshot remains usable afterward.
            sibling = tmp_path / "sibling"
            sibling.mkdir()
            (sibling / "config.yaml").write_text(
                (tmp_path / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            receipt.unlink()
            monkeypatch.setenv(token_name, "fixture-only-token")
            with delegated_child_context() if context == "child" else nullcontext():
                assert env_loader.hydrate_profile_secret_sources(sibling) == {}
                assert env_loader.hydrate_profile_secret_sources(tmp_path) == values
            assert not receipt.exists()
            assert f"1Password skipped: {reason}." in capsys.readouterr().err
    finally:
        _VAR_MAP["HERMES_CRON_SESSION"].reset(token)
        env_loader.reset_secret_source_cache()


@pytest.mark.parametrize("token_value", ["", "fixture-only-token"])
def test_op_child_environment_contract(monkeypatch, token_value):
    """Tokened reads drop every inherited account/Connect/session selector and force the
    desktop-app integration off; tokenless reads keep the user's session. Neither forwards an
    unrelated provider secret."""
    from agent.secret_sources.base import set_source_environment, reset_source_environment
    from agent.secret_sources.onepassword import _op_child_env, _OP_ENV_ALLOWLIST
    source = {name: f"fixture-{name}" for name in _OP_ENV_ALLOWLIST}
    source.update(OP_SESSION_fixture="fixture-session", OPENAI_API_KEY="fixture-provider-secret",
                  UNRELATED_SECRET="not-forwarded")
    token = set_source_environment(source)
    try:
        expected = {name: source[name] for name in _OP_ENV_ALLOWLIST}
        expected.update(OP_SESSION_fixture="fixture-session", NO_COLOR="1")
        if token_value:
            expected = {k: v for k, v in expected.items()
                        if k != "OP_ACCOUNT" and not k.startswith(("OP_CONNECT_", "OP_SESSION_"))}
            expected.update(OP_SERVICE_ACCOUNT_TOKEN=token_value, OP_LOAD_DESKTOP_APP_SETTINGS="false")
        assert _op_child_env(token_value) == expected
    finally:
        reset_source_environment(token)


def test_desktop_gateway_ensure_daemon_never_runs_tokenless_op(monkeypatch, tmp_path):
    """The .5 Desktop launch door end to end: ``gateway ensure`` → ``spawn_unmanaged_gateway``
    builds the daemon argv/env (Popen captured, never run); that daemon's startup dotenv load
    then reaches the 1Password source without ever spawning ``op``."""
    import subprocess
    import time
    from agent.secret_sources import onepassword as op
    from hermes_cli import gateway_runtime_start

    _clear_context(monkeypatch)
    home = tmp_path / "profiles" / "fixture"
    home.mkdir(parents=True)
    spawned = {}

    def _capture(command, **kwargs):
        spawned.update(command=command, env=kwargs["env"], stdin=kwargs["stdin"])
        return Mock(pid=0)

    monkeypatch.setattr(gateway_runtime_start.subprocess, "Popen", _capture)
    gateway_runtime_start.spawn_unmanaged_gateway(home, deadline=time.monotonic() + 30)
    assert spawned["stdin"] is subprocess.DEVNULL
    daemon_argv = ["hermes", *spawned["command"][spawned["command"].index("hermes_cli.main") + 1:]]
    assert daemon_argv[1:3] == ["gateway", "run"]

    for name, value in spawned["env"].items():
        if name.startswith("HERMES_") and name != "HERMES_HOME":
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", daemon_argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(env_loader, "_ONEPASSWORD_SKIP_LOGGED", False)
    runner = Mock(return_value=Mock(returncode=0, stdout="fixture-resolved\n", stderr=""))
    monkeypatch.setattr(op, "run_cli", runner)
    monkeypatch.setattr(op, "find_op", lambda binary_path="": Path("/fixture/op"))
    (home / "config.yaml").write_text(
        "secrets:\n  onepassword:\n    enabled: true\n    cache_ttl_seconds: 0\n"
        "    env:\n      FIXTURE_RESOLVED: op://Fixture/Item/field\n", encoding="utf-8")
    env_loader.reset_secret_source_cache()
    try:
        env_loader.load_hermes_dotenv(hermes_home=home)
        assert not runner.called
        assert env_loader._startup_onepassword_skip_reason() == "detached gateway daemon"
    finally:
        os.environ.pop("FIXTURE_RESOLVED", None)
        env_loader.reset_secret_source_cache()
