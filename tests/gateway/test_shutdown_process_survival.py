"""Gateway shutdown preserves only restart-safe, non-cron host processes."""

import json
from pathlib import Path

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tools.process_registry import ProcessRegistry


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
    assert registry.poll(cron.id)["status"] == "exited"
    assert registry.poll(pty.id)["status"] == "exited"
    checkpoint = json.loads((Path(survivor.receipt_home) / "processes.json").read_text())
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
    assert registry.poll(session.id)["status"] == "exited"
    assert all(row["session_id"] != session.id for row in json.loads(
        (Path(session.receipt_home) / "processes.json").read_text()))

