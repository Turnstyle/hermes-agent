"""A worker that exits on its own leaves no helper behind in its process group (t_590dc20c).

``_default_spawn`` starts every worker with ``start_new_session=True``, so the worker leads its own
process group. When an external CLI worker exits nonzero BY ITSELF (no timeout / archive), the
crash sweep used to release the card without signalling anything: an MCP helper the CLI started
(``npm exec @playwright/mcp``) stayed alive in the dead worker's group, adopted by init. These tests
use real processes: a leader that starts a grandchild and then exits on its own, then the crash
sweep, then a liveness check on the grandchild.
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

# Leader = argv: <child pid file> <child source> <go file> <exit code>. It starts the grandchild,
# waits until the test has recorded its fingerprint (go file), then exits on its own.
_LEADER_EXITS_ALONE = (
    "import os, subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])\n"
    "while not os.path.exists(sys.argv[3]):\n"
    "    time.sleep(0.02)\n"
    "os._exit(int(sys.argv[4]))\n"
)
_SLEEPER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
_TERM_IGNORING_SLEEPER = (
    "import os, signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
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


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kb._pid_alive(pid):
            return True
        time.sleep(0.05)
    return not kb._pid_alive(pid)


def _cleanup(*pids: int, group: int = 0) -> None:
    if group:
        try:
            os.killpg(group, signal.SIGKILL)
        except (OSError, RuntimeError):
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, RuntimeError):
            pass


def _running_card_whose_leader_exits(conn, tmp_path: Path, child_src: str, exit_code: int,
                                     *, new_session: bool = True):
    """A claimed card whose recorded worker (real fingerprint) starts a grandchild and then exits
    ``exit_code`` on its own. Returns (task id, leader Popen, grandchild pid)."""
    script = tmp_path / "leader.py"
    script.write_text(_LEADER_EXITS_ALONE)
    pid_file, go = tmp_path / "child.pid", tmp_path / "go"
    tid = kb.create_task(conn, title="crash-orphan", assignee="fx", max_retries=3)
    kb.claim_task(conn, tid)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(pid_file), child_src, str(go), str(exit_code)],
        start_new_session=new_session, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    kbd._set_worker_pid(conn, tid, proc.pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip()):
        time.sleep(0.05)
    child = int(pid_file.read_text())
    assert kb._pid_alive(child)
    go.write_text("1")
    proc.wait(timeout=10)  # the leader is gone (reaped), its grandchild is not
    assert kb._pid_alive(child)
    return tid, proc, child


def test_crash_sweep_ends_the_dead_workers_orphaned_group(board, tmp_path):
    """RED on base: the leader exits 1 by itself; the crash sweep released the card and the
    grandchild kept running."""
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 1)
    try:
        crashed = kbd.detect_crashed_workers(conn)
        assert tid in crashed
        assert _wait_dead(child), "dead worker's grandchild survived the crash sweep"
        reaped = [e.payload for e in kb.list_events(conn, tid) if e.kind == "worker_group_reaped"]
        assert len(reaped) == 1
        assert reaped[0]["pid"] == proc.pid and reaped[0]["members"] == [child]
        assert reaped[0]["signalled"] is True
    finally:
        _cleanup(child, group=proc.pid)


def test_crash_sweep_sigkills_a_term_ignoring_orphan(board, tmp_path):
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _TERM_IGNORING_SLEEPER, 1)
    try:
        assert tid in kbd.detect_crashed_workers(conn)
        assert _wait_dead(child), "SIGTERM-ignoring orphan survived the crash sweep"
        reaped = [e.payload for e in kb.list_events(conn, tid) if e.kind == "worker_group_reaped"][-1]
        assert reaped["sigkill"] is True
    finally:
        _cleanup(child, group=proc.pid)


def test_rate_limited_exit_also_ends_the_orphaned_group(board, tmp_path):
    """A quota exit is released without a failure count, but its helpers still go."""
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(
        conn, tmp_path, _SLEEPER, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    try:
        kbd._record_worker_exit(proc.pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        assert tid not in kbd.detect_crashed_workers(conn)
        assert tid in kbd.detect_crashed_workers._last_rate_limited
        assert _wait_dead(child), "rate-limited worker's grandchild survived the sweep"
    finally:
        _cleanup(child, group=proc.pid)


@pytest.mark.live_system_guard_bypass  # cleanup must SIGKILL our own grandchild after init adopts it
def test_worker_that_shared_a_group_is_never_group_signalled(board, tmp_path, monkeypatch):
    """A worker that did NOT lead its own group has no group of its own to end: no killpg."""
    monkeypatch.setattr(kbd.os, "killpg", lambda *a: pytest.fail("killpg for a non-leader worker"))
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 1, new_session=False)
    try:
        assert tid in kbd.detect_crashed_workers(conn)
        assert kb._pid_alive(child)
        assert [e for e in kb.list_events(conn, tid) if e.kind == "worker_group_reaped"] == []
    finally:
        _cleanup(child)


@pytest.mark.parametrize("fingerprint", [None, kbd.UNVERIFIED_WORKER_FINGERPRINT, 12345])
def test_unverified_or_legacy_rows_never_group_signal(monkeypatch, fingerprint):
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: pytest.fail("group probed"))
    info = kbd._reap_orphaned_worker_group(424242, fingerprint)
    assert info["signalled"] is False and info["refused"]


def test_other_boot_fingerprint_never_group_signals(monkeypatch):
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: pytest.fail("group probed"))
    info = kbd._reap_orphaned_worker_group(424242, "other-boot:1|100")
    assert info["refused"] == "other_boot"


def test_live_leader_pid_is_never_group_signalled(monkeypatch):
    """A live process holding the leader PID (recycled) means the old group is gone."""
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    fp = kbd._process_fingerprint(os.getpid())
    info = kbd._reap_orphaned_worker_group(os.getpid(), fp)
    assert info["refused"] == "leader_pid_live"


def test_member_older_than_the_worker_blocks_the_group_signal(monkeypatch):
    """Every member must have started at or after the worker did; an older one is a stranger."""
    from gateway.drain_control import current_instantiation_epoch
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: pid != 424242)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: [5001])
    import gateway.status as gs
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: 99)
    info = kbd._reap_orphaned_worker_group(424242, f"{current_instantiation_epoch()}|100")
    assert info["refused"] == "member_predates_worker" and info["members"] == [5001]


def test_dispatcher_own_group_is_never_signalled(monkeypatch):
    from gateway.drain_control import current_instantiation_epoch
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    info = kbd._reap_orphaned_worker_group(os.getpgrp(), f"{current_instantiation_epoch()}|1")
    assert info["refused"] == "own_group"


def test_empty_group_is_a_quiet_no_op(monkeypatch):
    from gateway.drain_control import current_instantiation_epoch
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: [])
    info = kbd._reap_orphaned_worker_group(424242, f"{current_instantiation_epoch()}|1")
    assert info["members"] == [] and info["signalled"] is False and info["refused"] is None


def test_unreadable_ps_never_signals(monkeypatch):
    """Without a member list the start-time check cannot run: refuse, never kill blind."""
    from gateway.drain_control import current_instantiation_epoch
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: None)
    info = kbd._reap_orphaned_worker_group(424242, f"{current_instantiation_epoch()}|1")
    assert info["refused"] == "ps_unreadable"
