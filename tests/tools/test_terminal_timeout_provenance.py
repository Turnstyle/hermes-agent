"""Terminal timeout teardown must kill only task-owned trees (#107029 / fk_e4eaf333).

Red-on-base (2): kill_process_tree refuses caller pid/pgid; _reap_untracked never killpg
shared PGID. Green scenario tests: LocalEnvironment timeout/interrupt, registry wait/fingerprint.

Synthetic fixtures only: no gateway, profile service, or foreign CLI signalling.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.deadline import kill_process_tree
from tools.environments.local import LocalEnvironment
from tools.process_registry import ProcessRegistry


@pytest.fixture()
def registry():
    return ProcessRegistry()


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "logs").mkdir(exist_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_pid_exit(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


@contextmanager
def _fake_process_tree_snapshot(_pid, hard_kill=False):
    class _FrozenProc:
        def is_running(self):
            return False

        def send_signal(self, _sig):
            pass

    yield [_FrozenProc()]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group provenance")
def test_kill_process_tree_refuses_teardown_when_target_is_caller_pgid(monkeypatch):
    """#107029: target pid equals caller PGID — refuse all signals (no killpg, no os.kill)."""
    killpg_calls = []
    kill_calls = []

    caller_pgid = 67890
    monkeypatch.setattr(os, "getpgid", lambda _pid: caller_pgid)
    monkeypatch.setattr(os, "getpgrp", lambda: caller_pgid)
    monkeypatch.setattr(os, "getpid", lambda: 11111)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr("agent.deadline._process_tree_snapshot", _fake_process_tree_snapshot)

    assert kill_process_tree(caller_pgid, sig=signal.SIGKILL) is False
    assert killpg_calls == []
    assert kill_calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group provenance")
def test_reap_untracked_uses_pid_kill_when_child_shares_caller_group(registry, monkeypatch):
    """Post-spawn failure cleanup must not killpg the caller's process group."""
    fake_proc = SimpleNamespace(pid=9999, poll=lambda: None, wait=lambda timeout=None: 0)
    killpg_calls = []

    monkeypatch.setattr(os, "getpgid", lambda _pid: 67890)
    monkeypatch.setattr(os, "getpgrp", lambda: 67890)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    session = registry._new_session("sleep 9", "task-a", "task-a", "", "/tmp")
    registry._reap_untracked(session, fake_proc)

    assert killpg_calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX live process tree")
def test_foreground_timeout_kills_owned_command_not_foreign_sleeper(tmp_path):
    """Owned foreground command times out; unrelated long-lived process keeps running."""
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    env = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = env.execute(
            f"{sys.executable} -c 'import time; time.sleep(120)'",
            timeout=2,
        )
        assert result.get("returncode") == 124 or "timed out" in result.get("output", "").lower()
        assert _pid_alive(foreign.pid), "foreign sleeper must survive foreground timeout kill"
    finally:
        if foreign.poll() is None:
            foreign.kill()
        foreign.wait(timeout=5)
        try:
            env.cleanup()
        except Exception:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX live process tree")
def test_foreground_timeout_kills_nested_owned_child(tmp_path):
    """Nested grandchild under the timed-out command must not outlive the timeout path."""
    pytest.importorskip("psutil")
    started = tmp_path / "grandchild_started"
    grandchild_py = tmp_path / "grandchild.py"
    grandchild_py.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    parent_py = tmp_path / "parent.py"
    parent_py.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild_py)!r}])\n"
        "time.sleep(120)\n"
    )
    env = LocalEnvironment(cwd=str(tmp_path))
    try:
        import threading

        result_box: dict = {}

        def _run_execute():
            result_box["result"] = env.execute(f"{sys.executable} {parent_py}", timeout=3)

        worker = threading.Thread(target=_run_execute)
        worker.start()
        harness_deadline = time.monotonic() + 10.0
        while time.monotonic() < harness_deadline and not (
            started.exists() and started.read_text().strip()
        ):
            time.sleep(0.05)
        worker.join(timeout=12.0)
        assert started.exists() and started.read_text().strip(), "nested child never started"
        result = result_box.get("result")
        assert result is not None
        assert result.get("returncode") == 124 or "timed out" in result.get("output", "").lower()
        nested_pid = int(started.read_text().strip())
        assert _wait_pid_exit(nested_pid), "nested owned child survived timeout teardown"
    finally:
        try:
            env.cleanup()
        except Exception:
            pass


def test_signal_kill_pty_fallback_routes_through_terminate_host_pid(registry):
    """PTY terminate failure must use fingerprint-aware host teardown, not bare os.kill."""
    session = registry._new_session("codex", "t1", "t1", "", "/tmp")
    session._pty = MagicMock()
    session._pty.terminate.side_effect = RuntimeError("pty gone")
    session.pid = 12345
    session.host_start_time = 424242
    calls: list[tuple[int, int]] = []

    def _capture(pid, expected_start):
        calls.append((pid, expected_start))

    registry._terminate_host_pid = _capture
    assert registry._signal_kill(session, session.id, consume_output=True) is None
    assert calls == [(12345, 424242)]


def test_tracked_background_kill_refuses_wrong_host_start_fingerprint(registry):
    """Background tracked kill path refuses a recycled-PID fingerprint (fail-closed)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real_start = ProcessRegistry._safe_host_start_time(proc.pid)
        if real_start is None:
            pytest.skip("no host start-time fingerprint on this platform")
        registry._terminate_host_pid(proc.pid, expected_start=real_start + 1)
        assert proc.poll() is None, "foreign/recycled fingerprint must not be signalled"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_background_wait_timeout_leaves_process_running(registry):
    """Wait timeout on a tracked process does not kill it (status timeout, still running)."""
    session = registry.spawn_local(
        f"{sys.executable} -c 'import time; time.sleep(30)'",
        cwd=os.getcwd(),
    )
    try:
        result = registry.wait(session.id, timeout=1)
        assert result.get("status") == "timeout"
        assert result.get("process_running") is True
        assert registry.get(session.id) is not None
        assert not registry.get(session.id).exited
    finally:
        registry.kill_process(session.id, source="test_cleanup")


def test_kill_process_tree_on_already_exited_child():
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait(timeout=10)
    assert kill_process_tree(proc.pid) is False


def test_interrupt_path_kills_owned_foreground_not_foreign(tmp_path, monkeypatch):
    """Cancellation (/stop) kills the in-flight owned command; foreign sleeper survives."""
    from tools.interrupt import set_interrupt

    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    env = LocalEnvironment(cwd=str(tmp_path))
    fake_proc = None

    def _capture_bash(*_a, **_k):
        nonlocal fake_proc
        fake_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
        )
        return fake_proc

    monkeypatch.setattr(env, "_run_bash", _capture_bash)

    parent_tid = __import__("threading").get_ident()

    def _wait_with_interrupt(proc, timeout=120, **kwargs):
        set_interrupt(True, thread_id=kwargs.get("watch_interrupt_tid") or parent_tid)
        env._kill_process(proc)
        return {"output": "[Command interrupted]", "returncode": 130}

    monkeypatch.setattr(env, "_wait_for_process", _wait_with_interrupt)
    monkeypatch.setattr(env, "_update_cwd", lambda _r: None)

    try:
        result = env.execute("sleep 120", timeout=60)
        assert result.get("returncode") == 130
        assert fake_proc is not None
        assert _wait_pid_exit(fake_proc.pid)
        assert _pid_alive(foreign.pid), "foreign process must survive owned interrupt kill"
    finally:
        if foreign.poll() is None:
            foreign.kill()
        foreign.wait(timeout=5)
