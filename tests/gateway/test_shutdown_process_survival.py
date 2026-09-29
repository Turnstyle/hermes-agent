"""Gateway shutdown preserves only restart-safe, non-cron host processes."""

import json
import time
from pathlib import Path

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.process_registry import ProcessRegistry


def _until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def shutdown_registry(monkeypatch):
    import tools.process_registry as module

    registry = ProcessRegistry()
    monkeypatch.setattr(module, "process_registry", registry)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *a, **k: 0)
    monkeypatch.setattr("tools.terminal_tool_lifecycle.cleanup_all_environments", lambda: None)
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
    yield registry
    registry.kill_all()


@pytest.mark.platforms("posix")
def test_shutdown_spares_file_host_but_kills_cron_and_pty(shutdown_registry, monkeypatch):
    registry = shutdown_registry
    monkeypatch.setattr("hermes_cli.config.read_raw_config",
                        lambda: {"terminal": {"kill_background_on_gateway_stop": False}})
    survivor = registry.spawn_local("sleep 20", owner_task_id="turn:1")
    cron = registry.spawn_local("sleep 20", owner_task_id="cron:job:1")
    pty = registry.spawn_local("sleep 20", owner_task_id="turn:2", use_pty=True)
    assert pty.output_log_path == ""

    GatewayShutdownMixin._stop_kill_tool_subprocesses("test")

    assert registry.poll(survivor.id)["status"] == "running"
    assert registry.wait(cron.id, timeout=5)["status"] == "exited"
    assert registry.wait(pty.id, timeout=5)["status"] == "exited"
    checkpoint_path = Path(survivor.output_log_path).parents[2] / "processes.json"
    assert _until(lambda: all(row["session_id"] not in {cron.id, pty.id}
                              for row in json.loads(checkpoint_path.read_text())))
    checkpoint = json.loads(checkpoint_path.read_text())
    entry = next(row for row in checkpoint if row["session_id"] == survivor.id)
    assert entry["output_log_path"] == survivor.output_log_path
    assert entry["exit_file_path"] == survivor.exit_file_path
    assert all(row["session_id"] not in {cron.id, pty.id} for row in checkpoint)


@pytest.mark.platforms("posix")
def test_shutdown_config_true_kills_file_host(shutdown_registry, monkeypatch):
    registry = shutdown_registry
    monkeypatch.setattr("hermes_cli.config.read_raw_config",
                        lambda: {"terminal": {"kill_background_on_gateway_stop": True}})
    session = registry.spawn_local("sleep 20", owner_task_id="turn:1")
    GatewayShutdownMixin._stop_kill_tool_subprocesses("test")
    assert registry.wait(session.id, timeout=5)["status"] == "exited"
    checkpoint_path = Path(session.output_log_path).parents[2] / "processes.json"
    assert _until(lambda: all(row["session_id"] != session.id
                              for row in json.loads(checkpoint_path.read_text())))


@pytest.mark.platforms("posix")
def test_shutdown_reads_each_owning_profiles_policy(shutdown_registry, tmp_path):
    registry = shutdown_registry
    sessions = {}
    for name, kill in (("a", True), ("b", False)):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            f"terminal:\n  kill_background_on_gateway_stop: {str(kill).lower()}\n")
        token = set_hermes_home_override(home)
        try:
            sessions[name] = registry.spawn_local("sleep 20", owner_task_id=f"turn:{name}")
        finally:
            reset_hermes_home_override(token)
    token = set_hermes_home_override(tmp_path / "a")
    try:
        GatewayShutdownMixin._stop_kill_tool_subprocesses("test")
    finally:
        reset_hermes_home_override(token)
    assert registry.wait(sessions["a"].id, timeout=5)["status"] == "exited"
    assert registry.poll(sessions["b"].id)["status"] == "running"
