"""The unmanaged daemon `gateway ensure` starts (the local Desktop's backend door) never inherits a
supervisor identity from the shell that asked for it, and keeps the delegated-child fence."""
import subprocess
import sys
import time

import pytest


@pytest.mark.platforms("linux", "macos")
def test_unmanaged_daemon_drops_inherited_supervisor_markers(tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime_start as start
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    seen = {}

    def fake_popen(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(start.subprocess, "Popen", fake_popen)
    inherited = {"HERMES_SUPERVISED_CHILD": "1", "HERMES_LAUNCHD_LABEL": "ai.hermes.gateway",
                 "INVOCATION_ID": "terminal-scope"}
    for key, value in {**inherited, "HERMES_DELEGATED_CHILD_CONTEXT": "1"}.items():
        monkeypatch.setenv(key, value)
    start.spawn_unmanaged_gateway(home, deadline=time.monotonic() + 5)
    env = seen["env"]
    assert [key for key in inherited if key in env] == []
    assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == "1"
    assert env["HERMES_GATEWAY_DETACHED"] == "1"
    assert seen["command"][:3] == [sys.executable, "-m", "hermes_cli.main"]
    # The asking process keeps its own supervisor identity.
    assert {key: start.os.environ.get(key) for key in inherited} == inherited
