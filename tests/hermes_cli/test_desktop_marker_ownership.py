"""Desktop-owned launch boundaries drop inherited session markers; everything else keeps its identity.

A Desktop app (or the backend it owns) launched from a ``hermes chat -Q``, cron, launchd or
supervised shell would otherwise run every interactive session as one-shot/cron/supervised.
The Kanban delegated-child fence is not a session mode and always survives.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

_POLLUTED = {
    "HERMES_SINGLE_QUERY_SESSION": "1",
    "HERMES_SESSION_ID": "20261015_120000_oneshot",
    "HERMES_SESSION_SOURCE": "cli_single_query",
    "HERMES_SESSION_KEY": "cli:oneshot",
    "HERMES_SUPERVISED_CHILD": "1",
    "HERMES_LAUNCHD_LABEL": "ai.hermes.gateway",
    "HERMES_CRON_SESSION": "1",
}
_FENCE = "HERMES_DELEGATED_CHILD_CONTEXT"


def _pollute(monkeypatch):
    for key, value in {**_POLLUTED, _FENCE: "1"}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("headless", [False, True])
def test_standalone_dashboard_keeps_supervisor_and_session_identity(monkeypatch, headless):
    from hermes_cli.main import _dashboard_sanitize_desktop_env
    monkeypatch.setattr(sys, "argv", ["hermes", "serve" if headless else "dashboard"])
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    # An inherited Desktop flag alone is not backend ownership.
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    _pollute(monkeypatch)
    _dashboard_sanitize_desktop_env(headless)
    assert {key: os.environ.get(key) for key in _POLLUTED} == _POLLUTED
    assert os.environ[_FENCE] == "1"


@pytest.mark.parametrize("headless", [False, True])
def test_desktop_owned_backend_drops_inherited_markers_keeps_fence(monkeypatch, headless):
    from hermes_cli.main import _dashboard_sanitize_desktop_env
    monkeypatch.setattr(sys, "argv", ["hermes", "serve" if headless else "dashboard"])
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "fixture-not-a-credential")
    _pollute(monkeypatch)
    _dashboard_sanitize_desktop_env(headless)
    assert [key for key in _POLLUTED if key in os.environ] == []
    assert os.environ[_FENCE] == "1"


def test_gateway_import_and_prompt_activation_preserve_identity(monkeypatch):
    """A gateway/TUI backend that is not Desktop-owned keeps its supervisor and session identity."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    identity = {"HERMES_SUPERVISED_CHILD": "1", "HERMES_LAUNCHD_LABEL": "ai.hermes.gateway",
                "HERMES_SESSION_SOURCE": "gateway", "HERMES_SESSION_ID": "fixture-session"}
    for key, value in identity.items():
        monkeypatch.setenv(key, value)
    code = ("import os; expected = {k: os.environ[k] for k in "
            f"{tuple(identity)!r}}}; "
            "import tui_gateway.server as server; "
            "assert {k: os.environ.get(k) for k in expected} == expected; "
            "server._enable_gateway_prompts(); "
            "assert {k: os.environ.get(k) for k in expected} == expected")
    subprocess.run([sys.executable, "-B", "-c", code], check=True, timeout=60)


def test_desktop_ownership_is_required_for_prefix_scrub():
    from hermes_cli.process_identity import strip_inherited_session_markers
    env = {"HERMES_SESSION_FUTURE": "fixture", "HERMES_SUPERVISED_CHILD": "1",
           "HERMES_LAUNCHD_LABEL": "fixture", _FENCE: "1"}
    original = dict(env)
    assert strip_inherited_session_markers(env) == []
    assert env == original
    assert set(strip_inherited_session_markers(env, desktop_owned=True)) == set(original) - {_FENCE}
    assert env == {_FENCE: "1"}


def test_desktop_launch_env_strips_markers_without_touching_the_launching_cli(monkeypatch, tmp_path):
    from hermes_cli import main_desktop
    monkeypatch.setattr(main_desktop, "_detect_linux_password_store", lambda: None)
    _pollute(monkeypatch)
    env, _flags = main_desktop._desktop_launch_env(argparse.Namespace(cwd=str(tmp_path)))
    assert [key for key in _POLLUTED if key in env] == []
    assert env[_FENCE] == "1"
    # The CLI that ran `hermes desktop` keeps its own session identity.
    assert {key: os.environ.get(key) for key in _POLLUTED} == _POLLUTED


@pytest.mark.platforms("macos")
def test_installed_macos_app_launch_strips_markers(monkeypatch):
    from hermes_cli import bundled_app, main_desktop
    launched = {}

    def fake_launch(argv, *, env=None, cwd=None):
        launched.update(argv=argv, env=env, cwd=cwd)
        return 4242

    monkeypatch.setattr(Path, "is_file", lambda self: str(self).endswith("/Contents/MacOS/Hermes"))
    monkeypatch.setattr(bundled_app, "launch_detached", fake_launch)
    _pollute(monkeypatch)
    assert main_desktop._launch_installed_macos_desktop_app() is True
    assert [key for key in _POLLUTED if key in launched["env"]] == []
    assert launched["env"][_FENCE] == "1"
    assert {key: os.environ.get(key) for key in _POLLUTED} == _POLLUTED
