"""Startup must not ask a person to approve an unattended secret fetch.

Protects the loader boundary, including scoped hydration and interactive use.
Regression: cron/launchd/children resolve op references and open a Touch ID prompt.
The existing opt-out test covers only one caller, not automatic context detection.
Only the actual source fetch is mocked; no test-only product API is needed.
"""
from contextlib import nullcontext
import sys
from unittest.mock import Mock

import pytest

from agent.secret_sources.base import FetchResult
from agent.secret_sources.onepassword import OnePasswordSource
from hermes_cli import env_loader


@pytest.mark.parametrize("route", ["dotenv", "hydrate"])
@pytest.mark.parametrize("context,tty,argv,marker,load", [
    ("explicit opt-out", True, ["hermes", "chat"], ("HERMES_SKIP_EXTERNAL_SECRET_SOURCES", "1"), False),
    ("no TTY", False, ["hermes", "--help"], None, False),
    ("launchd", True, ["hermes", "--help"], ("XPC_SERVICE_NAME", "job"), False),
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
    ("Desktop unchanged", False, ["hermes", "serve"], ("HERMES_DESKTOP", "1"), True),
    ("Desktop child", False, ["hermes", "chat"], ("HERMES_DESKTOP", "1"), False),
    ("gateway cron", True, ["hermes", "gateway", "run"], ("HERMES_CRON_SESSION", "1"), False),
    ("in-process child", True, ["hermes", "gateway", "run"], None, False),
    ("context cron", True, ["hermes", "gateway", "run"], None, False),
])
def test_startup_onepassword_context(monkeypatch, tmp_path, capsys, route, context, tty, argv, marker, load):
    for name in ("HERMES_SKIP_EXTERNAL_SECRET_SOURCES", "HERMES_DELEGATED_CHILD_CONTEXT",
                 "HERMES_KANBAN_TASK", "_HERMES_CRON_EXTERNAL_WORKER", "HERMES_CRON_SESSION",
                 "XPC_SERVICE_NAME", "HERMES_LAUNCHD_LABEL", "HERMES_DESKTOP"):
        monkeypatch.delenv(name, raising=False)
    if marker:
        monkeypatch.setenv(*marker)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(env_loader, "_ONEPASSWORD_SKIP_LOGGED", False, raising=False)
    monkeypatch.setenv("STARTUP_TEST_SECRET", "existing")
    (tmp_path / "config.yaml").write_text("secrets:\n  onepassword:\n    enabled: true\n    env:\n      STARTUP_TEST_SECRET: op://Vault/Item/field\n", encoding="utf-8")
    fetch = Mock(return_value=FetchResult(secrets={"STARTUP_TEST_SECRET": "mock-resolved"}))
    monkeypatch.setattr(OnePasswordSource, "fetch", fetch)
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import _VAR_MAP
    scope = delegated_child_context() if context == "in-process child" else nullcontext()
    token = _VAR_MAP["HERMES_CRON_SESSION"].set("1" if context == "context cron" else "")
    # Env cron fallback is masked by the context var; explicitly bind that case too.
    if context == "cron session" or context == "gateway cron":
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
            assert output.count("1Password skipped:") == (0 if context == "explicit opt-out" else 1)
            assert "STARTUP_TEST_SECRET" not in output
            assert "op://" not in output
            assert "mock-resolved" not in output
    finally:
        _VAR_MAP["HERMES_CRON_SESSION"].reset(token)
        env_loader.reset_secret_source_cache()
