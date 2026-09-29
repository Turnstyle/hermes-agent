"""One-shot cleanup leaves durable notify jobs running after its linger bound."""

import subprocess
import sys
import types

import pytest

from agent.client_lifecycle import ClientLifecycleMixin
from hermes_cli.oneshot import _close_agent
from tools.process_registry import ProcessRegistry


@pytest.mark.platforms("posix")
def test_oneshot_close_spares_file_notify_and_kills_pipe(monkeypatch):
    import tools.process_registry as module

    registry = ProcessRegistry()
    monkeypatch.setattr(module, "process_registry", registry)
    real_linger = registry.wait_for_pending_completions
    linger_results = []
    monkeypatch.setattr(registry, "wait_for_pending_completions",
                        lambda task_id: linger_results.append(
                            real_linger(task_id, timeout=0.1, poll_interval=0.05)))
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent._quietly = lambda fn, *args: fn(*args)
    fake_run_agent.cleanup_vm = lambda *a: None
    fake_run_agent.cleanup_browser = lambda *a: None
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr("tools.file_tools.clear_file_ops_cache", lambda *a: None)
    monkeypatch.setattr("tools.computer_use.tool.release_computer_use_session", lambda *a: None)

    durable = registry.spawn_local("sleep 20", owner_task_id="turn-1")
    durable.notify_on_complete = True
    pipe_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pipe = registry.adopt_local(pipe_proc, command="sleep 20", cwd=None,
                                owner_task_id="turn-1", notify_on_complete=True)

    class Agent:
        _process_owner_task_ids = ("turn-1",)
        _session_messages = []

        def shutdown_memory_provider(self, *args):
            pass

        def close(self):
            ClientLifecycleMixin._close_task_resources(self, "session")

    agent = Agent()
    try:
        _close_agent(agent, None)
        assert durable.id in linger_results[0]["timed_out"]
        assert pipe.id in linger_results[0]["timed_out"]
        assert agent._preserve_notify_file_processes_on_close is True
        assert registry.poll(durable.id)["status"] == "running"
        assert registry.poll(pipe.id)["status"] == "exited"
    finally:
        registry.kill_all()
