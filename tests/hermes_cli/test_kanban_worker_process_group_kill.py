"""Worker termination ends the worker's whole process group (t_8ef2c1b0).

``_default_spawn`` starts every worker with ``start_new_session=True``, so the worker leads its own
process group. A timeout / archive / stale reclaim used to signal the worker PID only, leaving any
CLI the worker had launched (``claude -p``, ``codex exec``, a plain ``sleep``) running after the
card was released. These tests use real processes: a worker that starts a grandchild, then the
dispatcher's termination path, then a liveness check on the grandchild.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")

# The grandchild writes its own pid (argv[1]) only once its signal setup is done, so a test never
# signals a half-started child. Worker = argv: <pid file> <grandchild source>.
_WORKER = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])\n"
    "p.wait()\n"
)
_SLEEPER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
_TERM_IGNORING_SLEEPER = (
    "import os, signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
)
_LEADER_EXITS_ON_TERM = (
    "import os, signal, subprocess, sys\n"
    "signal.signal(signal.SIGTERM, lambda *a: os._exit(0))\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])\n"
    "p.wait()\n"
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _alive(pid: int) -> bool:
    """Live and not a zombie (a killed grandchild reparented to init may linger as one briefly)."""
    return kb._pid_alive(pid)


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _spawn_worker(tmp_path: Path, worker_src: str, child_src: str, *, new_session: bool = True):
    script = tmp_path / "worker.py"
    script.write_text(worker_src)
    pid_file = tmp_path / "child.pid"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(pid_file), child_src],
        start_new_session=new_session, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip()):
        time.sleep(0.05)
    child = int(pid_file.read_text())
    assert _alive(child)
    return proc, child


def _cleanup(*pids: int, group: int = 0) -> None:
    """Leave nothing behind, even on a RED run: SIGKILL the worker's own group (reaches a grandchild
    that init already adopted), then each pid."""
    if group:
        try:
            os.killpg(group, signal.SIGKILL)
        except (OSError, RuntimeError):
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, RuntimeError):  # gone, or conftest guard after init adopted it
            pass


def _lock() -> str:
    return f"{kb._host_prefix()}test"


def test_timeout_kills_the_workers_grandchild(board, tmp_path, monkeypatch):
    """RED on base: a real dispatcher timeout left the worker's child running."""
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)  # assignee "fx" is not a real profile
    conn = board
    procs: list[subprocess.Popen] = []
    script = tmp_path / "worker.py"
    script.write_text(_WORKER)
    pid_file = tmp_path / "child.pid"

    def spawn_fn(task, workspace, board=None):
        p = subprocess.Popen(
            [sys.executable, str(script), str(pid_file), _SLEEPER], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p)
        return p.pid

    tid = kb.create_task(conn, title="gchild-timeout", assignee="fx", max_runtime_seconds=1, max_retries=1)
    kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_spawn=1)
    assert procs, "worker was not spawned"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip()):
        time.sleep(0.05)
    child = int(pid_file.read_text())
    try:
        time.sleep(1.5)
        result = kbd.dispatch_once(conn, spawn_fn=spawn_fn, max_spawn=0)
        assert tid in result.timed_out
        assert procs[0].wait(timeout=5) is not None
        assert _wait_dead(child), "worker's grandchild survived the dispatcher timeout"
        payload = [e.payload for e in kb.list_events(conn, tid) if e.kind == "timed_out"][-1]
        assert payload["group_signalled"] is True
    finally:
        _cleanup(child, procs[0].pid, group=procs[0].pid)


def test_group_is_sigkilled_after_grace_even_when_leader_already_exited(tmp_path):
    """Leader exits on SIGTERM; its child ignores SIGTERM. The post-grace group SIGKILL still ends it."""
    proc, child = _spawn_worker(tmp_path, _LEADER_EXITS_ON_TERM, _TERM_IGNORING_SLEEPER)
    try:
        fp = kbd._process_fingerprint(proc.pid)
        info = kbd._terminate_reclaimed_worker(proc.pid, _lock(), started_at=fp)
        assert _wait_dead(child), "SIGTERM-ignoring grandchild survived the group SIGKILL"
        assert info["terminated"] is True
        assert info["group_signalled"] is True and info["sigkill"] is True
    finally:
        _cleanup(child, proc.pid, group=proc.pid)
        proc.wait(timeout=5)


@pytest.mark.live_system_guard_bypass  # cleanup must SIGKILL our own grandchild after init adopts it
def test_worker_sharing_callers_group_never_gets_killpg(tmp_path, monkeypatch):
    """A worker NOT leading its own group (getpgid(pid) != pid) must never trigger killpg: the group
    could be the dispatcher's own. Only the worker PID is signalled (previous behaviour)."""
    calls = []

    def spy_killpg(pg, sig):  # record, never deliver: a regression must fail here, not kill pytest
        calls.append((pg, sig))
        if sig != 0:
            raise PermissionError("killpg refused by test spy")

    monkeypatch.setattr(kbd.os, "killpg", spy_killpg)
    proc, child = _spawn_worker(tmp_path, _WORKER, _SLEEPER, new_session=False)
    try:
        assert os.getpgid(proc.pid) != proc.pid
        info = kbd._terminate_reclaimed_worker(proc.pid, _lock(), started_at=kbd._process_fingerprint(proc.pid))
        assert [c for c in calls if c[1] != 0] == []
        assert info["terminated"] is True and info["group_signalled"] is False
    finally:
        _cleanup(child, proc.pid)
        proc.wait(timeout=5)


def test_signal_fn_hook_keeps_single_pid_path(monkeypatch):
    """A test/injected ``signal_fn`` keeps the single-PID contract; killpg is not used."""
    monkeypatch.setattr(kbd.os, "killpg", lambda *a: pytest.fail("killpg used with signal_fn hook"))
    sent = []
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at=None: True)
    info = kbd._terminate_reclaimed_worker(os.getpid(), _lock(), signal_fn=lambda p, s: sent.append((p, s)),
                                           started_at=kbd._process_fingerprint(os.getpid()))
    assert sent == [(os.getpid(), signal.SIGTERM)]
    assert info["group_signalled"] is False


def test_recycled_leader_pid_is_not_group_signalled(monkeypatch):
    """A fingerprint mismatch returns before any signal: no kill, no killpg."""
    real_kill = os.kill

    def no_real_signal(pid, sig):
        if sig != 0:  # sig 0 is the liveness probe
            pytest.fail("kill on a recycled pid")
        return real_kill(pid, sig)

    monkeypatch.setattr(kbd.os, "killpg", lambda *a: pytest.fail("killpg on a recycled pid"))
    monkeypatch.setattr(os, "kill", no_real_signal)
    info = kbd._terminate_reclaimed_worker(os.getpid(), _lock(), started_at="other-boot|1")
    assert info["pid_recycled"] is True and info["termination_attempted"] is False


def test_archive_kills_the_workers_grandchild(board, tmp_path):
    """archive_task routes through the same termination: the grandchild dies with the worker."""
    conn = board
    proc, child = _spawn_worker(tmp_path, _WORKER, _SLEEPER)
    try:
        tid = kb.create_task(conn, title="archive-gchild", assignee="fx")
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, proc.pid)
        assert kb.archive_task(conn, tid) is True
        proc.wait(timeout=5)
        assert _wait_dead(child), "worker's grandchild survived archive"
    finally:
        _cleanup(child, proc.pid, group=proc.pid)


def test_legacy_row_without_fingerprint_keeps_single_pid_path(tmp_path, monkeypatch):
    """A legacy row (no spawn fingerprint) is only ever signalled by PID, never as a whole group."""
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal for a legacy row"))
    proc, child = _spawn_worker(tmp_path, _WORKER, _SLEEPER)
    try:
        info = kbd._terminate_reclaimed_worker(proc.pid, _lock(), started_at=None)
        assert info["terminated"] is True and info["group_signalled"] is False
    finally:
        _cleanup(child, proc.pid, group=proc.pid)
        proc.wait(timeout=5)


def test_zombie_leader_of_another_parent_does_not_hold_the_grace_poll(monkeypatch):
    """A zombie leader whose parent is not the caller (CLI reclaim / archive) must not keep the
    poll waiting the full grace: only live group members count."""
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at=None: False)
    monkeypatch.setattr(kbd, "_reap_exited_leader", lambda pid: None)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: [])
    monkeypatch.setattr(kbd.time, "sleep", lambda s: pytest.fail("poll slept on a zombie-only group"))
    assert kbd._poll_worker_group_exit(424242, "fp") is True


def test_live_group_members_skips_zombies(monkeypatch):
    class R:
        returncode = 0
        stdout = "  10    10 Z\n  11    10 S\n  12    99 R\n"
    monkeypatch.setattr(kbd.subprocess, "run", lambda *a, **k: R())
    assert kbd._live_group_members(10) == [11]
    assert kbd._live_group_members(99) == [12]
    assert kbd._live_group_members(5) == []


def test_unreadable_ps_never_reads_as_empty_group(monkeypatch):
    """A failing ``ps`` must fail toward finishing the kill (``None`` = members may remain)."""
    class R:
        returncode, stdout = 1, ""
    monkeypatch.setattr(kbd.subprocess, "run", lambda *a, **k: R())
    assert kbd._live_group_members(10) is None
    assert kbd._worker_group_has_live_members(10) is True
