"""A Bot Chat delivery turn still writing at the cap is working, not stuck (t_d0edbb6e).

shld-cndr job f120c06f81e5 (2026-10-06): the delivered cron turn ran a long tool loop, its last
message landed 6 s before the 600 s cap killed it, and the run was booked as a timeout with a
degraded marker. The cap is now stall-aware: fresh activity on the target Bot Chat moves it to
last activity + stall window, never past the ceiling; a quiet turn is still killed at the cap.
"""
import subprocess
import threading
import time

import pytest

from cron import scheduler_delivery as delivery
from hermes_cli import quiet_single_query as qsq


class _FakeProc:
    """Blocks in communicate() until it exits on its own (``finish_after``) or is killed."""

    finish_after = None  # seconds; None = never ends on its own

    def __init__(self, *args, **kwargs):
        self.pid = 5151
        self.returncode = None
        self.done = threading.Event()
        self.killed = False
        if self.finish_after is not None:
            threading.Timer(self.finish_after, self._finish).start()

    def _finish(self):
        if not self.done.is_set():
            self.returncode = 0
            self.done.set()

    def communicate(self):
        self.done.wait()
        return "ok", ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.done.set()

    def terminate(self):
        self.kill()


@pytest.fixture()
def fake_popen(monkeypatch):
    procs = []

    def factory(finish_after):
        cls = type("P", (_FakeProc,), {"finish_after": finish_after})

        def popen(*a, **k):
            p = cls(*a, **k)
            procs.append(p)
            return p
        monkeypatch.setattr(qsq.subprocess, "Popen", popen)
        monkeypatch.setattr(qsq, "read_turn_report", lambda path, pid: None)
        return procs
    return factory


def test_busy_turn_past_cap_is_not_killed(fake_popen):
    procs = fake_popen(finish_after=0.9)
    result = qsq.run_reported_turn(
        ["hermes"], env={}, report_path="/nonexistent", timeout=0.3,
        progress=lambda: time.time(), stall_window=0.5, max_timeout=3.0)
    assert result.returncode == 0
    assert not procs[0].killed


def test_quiet_turn_is_killed_at_cap(fake_popen):
    procs = fake_popen(finish_after=None)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        qsq.run_reported_turn(
            ["hermes"], env={}, report_path="/nonexistent", timeout=0.3, term_grace=0.1,
            progress=lambda: time.time() - 60, stall_window=0.5, max_timeout=3.0)
    assert procs[0].killed
    assert time.monotonic() - started < 1.0


def test_ceiling_bounds_a_turn_that_never_goes_quiet(fake_popen):
    procs = fake_popen(finish_after=None)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        qsq.run_reported_turn(
            ["hermes"], env={}, report_path="/nonexistent", timeout=0.2, term_grace=0.1,
            progress=lambda: time.time(), stall_window=0.3, max_timeout=0.8)
    elapsed = time.monotonic() - started
    assert procs[0].killed
    assert 0.7 <= elapsed < 1.6


def test_probe_error_holds_the_cap(fake_popen):
    fake_popen(finish_after=None)

    def boom():
        raise RuntimeError("db locked")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        qsq.run_reported_turn(
            ["hermes"], env={}, report_path="/nonexistent", timeout=0.3, term_grace=0.1,
            progress=boom, stall_window=5.0, max_timeout=10.0)
    assert time.monotonic() - started < 1.0


def test_no_probe_keeps_the_fixed_cap(fake_popen):
    fake_popen(finish_after=None)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        qsq.run_reported_turn(["hermes"], env={}, report_path="/nonexistent", timeout=0.3, term_grace=0.1)
    assert time.monotonic() - started < 1.0


def test_last_activity_reads_the_canonical_bot_chat_tip(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="bc1", source="cli")
        db.set_session_title("bc1", "Bot Chat")
        db.append_message("bc1", role="user", content="hello")
    finally:
        db.close()
    last = delivery._bot_chat_last_activity(str(tmp_path))
    assert last is not None and abs(time.time() - last) < 30
    assert delivery._bot_chat_last_activity(str(tmp_path / "missing")) is None


def test_limits_default_and_disable(monkeypatch):
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {})
    assert delivery._get_bot_chat_progress_limits(600) == (180, 1800)
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {"cron": {
        "bot_chat_delivery_stall_seconds": 0, "bot_chat_delivery_max_seconds": 100}})
    assert delivery._get_bot_chat_progress_limits(600) == (0, 600)


def test_cron_lane_passes_the_probe(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(qsq, "run_reported_turn", fake_run)
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {})
    delivery._run_bot_chat_turn(["hermes"], {"HERMES_HOME": str(tmp_path)}, "r.json", 600)
    assert callable(seen["progress"]) and seen["stall_window"] == 180.0 and seen["max_timeout"] == 1800.0
    assert seen["timeout"] == 600 and seen["cwd"] == str(tmp_path)
