"""A worker that exits on its own leaves no helper behind in its process group (t_590dc20c).

``_default_spawn`` starts every worker with ``start_new_session=True``, so the worker leads its own
process group. When an external CLI worker exits nonzero BY ITSELF (no timeout / archive), the
crash sweep used to release the card without signalling anything: an MCP helper the CLI started
(``npm exec @playwright/mcp``) stayed alive in the dead worker's group, adopted by init. These tests
use real processes: a leader that starts a grandchild and then exits on its own, the dispatcher's
own reap of the leader, then the crash sweep, then a liveness check on the grandchild.
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
# waits (at most 30 s) until the test says go, then exits on its own.
_LEADER_EXITS_ALONE = (
    "import os, subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])\n"
    "deadline = time.monotonic() + 30\n"
    "while not os.path.exists(sys.argv[3]) and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
    "os._exit(int(sys.argv[4]))\n"
)
_SLEEPER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
_TERM_IGNORING_SLEEPER = (
    "import os, signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
)
# A real orphan helper started while the worker ran: older than the reap by more than the margin.
# (getattr: the file must import on the pre-fix base so its RED run shows the real survivor.)
_HELPER_AGE = getattr(kbd, "_ORPHAN_GROUP_REAP_MARGIN_SECONDS", 2.0) + 0.5


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


def _running_card_whose_leader_exits(conn, tmp_path: Path, child_src: str, exit_code: int, *,
                                     new_session: bool = True, dispatcher_reaps: bool = True,
                                     helper_age: float = _HELPER_AGE):
    """A claimed card whose recorded worker (real fingerprint) starts a grandchild and then exits
    ``exit_code`` on its own. The leader is reaped the way the dispatcher does it
    (``_reap_exited_leader``) unless ``dispatcher_reaps`` is False (then the worker is untracked,
    as when another process spawned it).
    Returns (task id, leader Popen, grandchild pid). Kills everything on any setup failure."""
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
    child = 0
    try:
        track = getattr(kbd, "_track_worker_proc", None)  # the _default_spawn bookkeeping
        if dispatcher_reaps and track is not None:
            track(proc)
        kbd._set_worker_pid(conn, tid, proc.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip()):
            time.sleep(0.05)
        child = int(pid_file.read_text())
        assert kb._pid_alive(child)
        time.sleep(helper_age)
        go.write_text("1")
        if dispatcher_reaps:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and proc.pid not in kbd._recent_worker_exits:
                kbd._reap_exited_leader(proc.pid)
                time.sleep(0.02)
            assert proc.pid in kbd._recent_worker_exits
        else:
            proc.wait(timeout=10)
        assert kb._pid_alive(child)  # the leader is gone, its grandchild is not
    except BaseException:
        _cleanup(*([child] if child else []), proc.pid, group=proc.pid if new_session else 0)
        raise
    return tid, proc, child


def _events(conn, tid, kind="worker_group_reaped"):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == kind]


def test_crash_sweep_ends_the_dead_workers_orphaned_group(board, tmp_path):
    """RED on base: the leader exits 1 by itself; the crash sweep released the card and the
    grandchild kept running."""
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 1)
    try:
        crashed = kbd.detect_crashed_workers(conn)
        assert tid in crashed
        assert _wait_dead(child), "dead worker's grandchild survived the crash sweep"
        reaped = _events(conn, tid)
        assert len(reaped) == 1
        assert reaped[0]["pid"] == proc.pid and reaped[0]["members"] == [child]
        assert reaped[0]["signalled"] is True and reaped[0]["refused"] is None
        assert reaped[0]["survivors"] == []
    finally:
        _cleanup(child, group=proc.pid)


def test_default_spawn_shape_survives_an_unrelated_subprocess_before_the_reap(board, tmp_path,
                                                                            monkeypatch):
    """Live gateway shape (review 2): the REAL ``_default_spawn`` returns only the pid. A dropped
    ``Popen`` of a running child lands in ``subprocess._active`` and the next ``Popen()`` anywhere
    in the process (here ``subprocess.run(['true'])``) reaped the worker silently, so the dispatcher
    never saw the exit and the orphans were left alone. RED before ``_default_spawn`` kept the
    worker handle."""
    conn = board
    script = tmp_path / "leader.py"
    script.write_text(_LEADER_EXITS_ALONE)
    pid_file, go = tmp_path / "child.pid", tmp_path / "go"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(kbd, "_worker_argv", lambda task, profile, home: [
        sys.executable, str(script), str(pid_file), _SLEEPER, str(go), "1"])
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda task, cmd: cmd)
    tid = kb.create_task(conn, title="spawn-shape", assignee="fx", max_retries=3)
    kb.claim_task(conn, tid)
    pid = kbd._default_spawn(kb.get_task(conn, tid), str(workspace))
    child = 0
    try:
        kbd._set_worker_pid(conn, tid, pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip()):
            time.sleep(0.05)
        child = int(pid_file.read_text())
        time.sleep(_HELPER_AGE)
        go.write_text("1")
        time.sleep(1.0)  # leader exits; nothing has reaped it yet
        subprocess.run(["true"], check=True)  # any unrelated subprocess in the dispatcher process
        kbd.reap_worker_zombies()
        assert tid in kbd.detect_crashed_workers(conn)
        assert _wait_dead(child), "orphan survived: the worker's exit was reaped behind the dispatcher"
        ev = _events(conn, tid)
        assert len(ev) == 1 and ev[0]["refused"] is None and ev[0]["signalled"] is True
    finally:
        _cleanup(*([child] if child else []), group=pid)


def test_reap_time_is_recorded_only_for_tracked_workers(monkeypatch):
    """A reap of any other child that reuses a number never becomes a group-reap anchor."""
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_worker_reaped_at", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    kbd._note_worker_reaped(424242, 0)
    assert kbd._worker_exit_reaped_at(424242) is None

    class FakeProc:
        pid, returncode = 424243, None

    fake = FakeProc()
    kbd._track_worker_proc(fake)
    kbd._note_worker_reaped(424243, 1 << 8)
    assert kbd._worker_exit_reaped_at(424243) is not None
    assert 424243 not in kbd._live_worker_procs and fake.returncode == 1

    # A leftover handle for a reused pid is marked finished before it is replaced.
    stale, fresh = FakeProc(), FakeProc()
    kbd._track_worker_proc(stale)
    kbd._track_worker_proc(fresh)
    assert stale.returncode == -1 and kbd._live_worker_procs[424243] is fresh


def test_crash_sweep_sigkills_a_term_ignoring_orphan(board, tmp_path):
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _TERM_IGNORING_SLEEPER, 1)
    try:
        assert tid in kbd.detect_crashed_workers(conn)
        assert _wait_dead(child), "SIGTERM-ignoring orphan survived the crash sweep"
        assert _events(conn, tid)[-1]["sigkill"] is True
    finally:
        _cleanup(child, group=proc.pid)


def test_rate_limited_exit_also_ends_the_orphaned_group(board, tmp_path):
    """A quota exit is released without a failure count, but its helpers still go."""
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(
        conn, tmp_path, _SLEEPER, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    try:
        assert tid not in kbd.detect_crashed_workers(conn)
        assert tid in kbd.detect_crashed_workers._last_rate_limited  # type: ignore[attr-defined]
        assert _wait_dead(child), "rate-limited worker's grandchild survived the sweep"
    finally:
        _cleanup(child, group=proc.pid)


def test_clean_exit_protocol_violation_also_ends_the_orphaned_group(board, tmp_path):
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 0)
    try:
        kbd.detect_crashed_workers(conn)
        assert _events(conn, tid, "protocol_violation")
        assert _wait_dead(child), "protocol-violation worker's grandchild survived the sweep"
    finally:
        _cleanup(child, group=proc.pid)


def test_crash_accounting_is_written_before_the_group_grace(board, tmp_path, monkeypatch):
    """The reap runs last: the crash event and failure count already exist when it starts."""
    conn = board
    seen = {}
    real = kbd._reap_crashed_worker_groups

    def spy(c, dead_groups):
        seen["crashed_events"] = len(_events(conn, dead_groups[0][0], "crashed"))
        seen["failures"] = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (dead_groups[0][0],)).fetchone()[0]
        return real(c, dead_groups)

    monkeypatch.setattr(kbd, "_reap_crashed_worker_groups", spy)
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 1)
    try:
        assert tid in kbd.detect_crashed_workers(conn)
        assert seen == {"crashed_events": 1, "failures": 1}
        assert _wait_dead(child)
    finally:
        _cleanup(child, group=proc.pid)


def test_unobserved_leader_exit_is_left_alone_and_recorded(board, tmp_path, monkeypatch):
    """No reap by this process (e.g. a per-tick CLI dispatcher) = no upper time bound = no signal."""
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 1,
                                                        dispatcher_reaps=False)
    try:
        kbd._recent_worker_exits.pop(proc.pid, None)
        assert tid in kbd.detect_crashed_workers(conn)
        assert kb._pid_alive(child)
        ev = _events(conn, tid)
        assert len(ev) == 1 and ev[0]["refused"] == "leader_exit_unobserved"
        assert ev[0]["members"] == [child] and ev[0]["signalled"] is False
    finally:
        _cleanup(child, group=proc.pid)


def test_group_whose_members_all_postdate_the_reap_is_left_alone(board, tmp_path, monkeypatch):
    """The recycled-group shape: every member started after the recorded reap → refuse."""
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    conn = board
    tid, proc, child = _running_card_whose_leader_exits(conn, tmp_path, _SLEEPER, 1, helper_age=0)
    try:
        assert tid in kbd.detect_crashed_workers(conn)
        assert kb._pid_alive(child)
        assert _events(conn, tid)[-1]["refused"] == "members_postdate_leader_exit"
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
        assert _events(conn, tid) == []
    finally:
        _cleanup(child)


def _same_boot(start: int) -> str:
    from gateway.drain_control import current_instantiation_epoch
    return f"{current_instantiation_epoch()}|{start}"


def test_two_dead_groups_share_one_grace(monkeypatch):
    """Groups are signalled together and wait ONE grace, not one each."""
    monkeypatch.setattr(kbd, "_vet_orphaned_worker_group",
                        lambda pid, fp: {"pgid": pid, "members": [pid + 1], "refused": None})
    sent = []
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda pg, sig: sent.append((pg, sig)) or True)
    monkeypatch.setattr(kbd, "_worker_group_has_live_members", lambda pg: True)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pg: [])
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    sleeps = []
    monkeypatch.setattr(kbd.time, "sleep", lambda s: sleeps.append(s))
    res = kbd._reap_orphaned_worker_groups([(424242, _same_boot(1)), (424250, _same_boot(1))])
    assert [s for (_p, s) in sent] == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL, signal.SIGKILL]
    assert sum(sleeps) <= 5.5
    assert res[424242]["sigkill"] and res[424250]["sigkill"]


@pytest.mark.parametrize("fingerprint", [None, kbd.UNVERIFIED_WORKER_FINGERPRINT, 12345])
def test_unverified_or_legacy_rows_never_group_signal(monkeypatch, fingerprint):
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: pytest.fail("group probed"))
    assert kbd._vet_orphaned_worker_group(424242, fingerprint)["refused"] == "no_fingerprint"


def test_other_boot_fingerprint_never_group_signals(monkeypatch):
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: pytest.fail("group probed"))
    assert kbd._vet_orphaned_worker_group(424242, "other-boot:1|100")["refused"] == "other_boot"


def test_live_leader_pid_is_never_group_signalled():
    fp = kbd._process_fingerprint(os.getpid())
    assert kbd._vet_orphaned_worker_group(os.getpid(), fp)["refused"] == "leader_pid_live"


def test_member_older_than_the_worker_blocks_the_group_signal(monkeypatch):
    import gateway.status as gs
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: pid != 424242)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: [5001])
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: 99)
    info = kbd._vet_orphaned_worker_group(424242, _same_boot(100))
    assert info["refused"] == "member_predates_worker" and info["members"] == [5001]


def test_member_with_unreadable_start_time_blocks_the_group_signal(monkeypatch):
    import gateway.status as gs
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: [5001])
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: None)
    assert kbd._vet_orphaned_worker_group(424242, _same_boot(1))["refused"] == "member_start_unreadable"


def test_dispatcher_own_group_is_never_signalled(monkeypatch):
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    assert kbd._vet_orphaned_worker_group(os.getpgrp(), _same_boot(1))["refused"] == "own_group"


def test_empty_group_is_a_quiet_no_op(board, monkeypatch):
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: [])
    info = kbd._vet_orphaned_worker_group(424242, _same_boot(1))
    assert info["members"] == [] and info["refused"] is None
    tid = kb.create_task(board, title="quiet", assignee="fx")
    kbd._reap_crashed_worker_groups(board, [(tid, 424242, _same_boot(1), None)])
    assert _events(board, tid) == []


def test_unreadable_ps_never_signals_and_is_recorded(board, monkeypatch):
    """Without a member list the checks cannot run: refuse, never kill blind, and say so."""
    monkeypatch.setattr(kbd, "_signal_worker_group", lambda *a: pytest.fail("group signal"))
    monkeypatch.setattr(kbd._kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_live_group_members", lambda pgid: None)
    assert kbd._vet_orphaned_worker_group(424242, _same_boot(1))["refused"] == "ps_unreadable"
    tid = kb.create_task(board, title="ps", assignee="fx")
    kbd._reap_crashed_worker_groups(board, [(tid, 424242, _same_boot(1), None)])
    assert [e["refused"] for e in _events(board, tid)] == ["ps_unreadable"]

