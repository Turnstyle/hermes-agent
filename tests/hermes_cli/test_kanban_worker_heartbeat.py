"""Real SQLite + fake-clock tests for activity-independent worker liveness."""
from __future__ import annotations

import contextvars
import os
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli import kanban_worker_heartbeat as timer


@pytest.fixture
def worker(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    with connect_closing(path) as conn:
        task = kb.create_task(conn, title="silent worker", assignee="test-worker")
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, task, claimer="test:worker", ttl_seconds=777)
        assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", claimed.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)
    return timer.WorkerHeartbeat(path, claimed.id, claimed.current_run_id, claimed.claim_lock)


def events(worker):
    with sqlite3.connect(worker.path) as conn:
        return conn.execute(
            "SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id",
            (worker.task_id,),
        ).fetchall()


def fake_time(monkeypatch, now):
    clock = SimpleNamespace(now=float(now))
    fake = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(timer, "time", fake)
    monkeypatch.setattr(kbd, "time", fake)
    monkeypatch.setattr(kb, "time", fake)
    return clock


class ClockEvent:
    def __init__(self, clock, end, on_wait=None):
        self.clock, self.end, self.on_wait = clock, end, on_wait
        self.done = False
        self.waits = []

    def is_set(self):
        return self.done

    def set(self):
        self.done = True

    def wait(self, delay):
        self.waits.append(delay)
        self.clock.now += delay
        if self.on_wait:
            self.on_wait()
        if self.clock.now >= self.end:
            self.done = True
        return self.done


@pytest.mark.parametrize("silent_operation", ["model", "tool"])
def test_31_minutes_silent_cli_turn(worker, monkeypatch, silent_operation):
    """Run the actual CLI one-shot function around a simulated blocking call.

    Only the timer's Event.wait advances the fake clock; the call emits zero
    stream chunks, tool callbacks, or activity heartbeats for 31 minutes.
    """
    import cli as cli_module

    start = int(time.time()) + 100
    clock = fake_time(monkeypatch, start)
    worker.stopped = ClockEvent(clock, start + 31 * 60)
    monkeypatch.setattr(timer, "start_worker_heartbeat", lambda: worker)
    finalized = []
    helpers: dict[str, Any] = dict(_should_seed_interactive=lambda *a: False,
                   _collect_query_images=lambda *a: ("query", []), _collect_kanban_task_images=lambda *a: [],
                   _route_single_query_images=lambda *a: "query", _configure_quiet_agent=lambda *a: None,
                   _finalize_single_query=lambda cli: finalized.append(True))

    def silent_call(*args, **kwargs):
        worker.run()
        if silent_operation == "tool":
            raise RuntimeError("simulated tool failure after silent call")
        return "model result"

    helpers["_run_quiet_single_query"] = silent_call
    for name, helper in helpers.items():
        monkeypatch.setattr(cli_module, name, helper)
    cli = SimpleNamespace(_claim_active_session=lambda *a, **kw: True,
                          console=SimpleNamespace(print=lambda *a: None),
                          _ensure_runtime_credentials=lambda: True,
                          _resolve_turn_agent_config=lambda q: dict(signature="same", model="test", runtime="test"),
                          _active_agent_route_signature="same", _init_agent=lambda **kw: True, agent=object(),
                          _show_security_advisories=lambda: None, chat=silent_call,
                          _print_exit_summary=lambda **kw: None)
    run = lambda: cli_module._run_single_query_mode(cli, "query", None, silent_operation == "tool", True)
    if silent_operation == "tool":
        with pytest.raises(RuntimeError, match="simulated tool failure"):
            run()
    else:
        run()
    beats = [r[0] for r in events(worker)]
    assert len(beats) == 31
    assert beats[0] == start
    assert max(b - a for a, b in zip(beats, beats[1:])) <= 60
    assert worker.stopped.is_set() and finalized == [True]
    with sqlite3.connect(worker.path) as conn:
        task = conn.execute("SELECT last_heartbeat_at,claim_expires FROM tasks WHERE id=?", (worker.task_id,)).fetchone()
        attempt = conn.execute("SELECT last_heartbeat_at,claim_expires FROM task_runs WHERE id=?", (worker.run_id,)).fetchone()
    assert task == attempt  # timer updates both views; does NOT renew the claim
    assert task[0] == beats[-1]
    assert task[1] < clock.now
    assert worker.beat() is None
    assert len(events(worker)) == 31


@pytest.mark.parametrize("transition", ["end", "crash", "release", "successor", "foreign_lock", "foreign_pid"])
def test_timer_stops_on_lifecycle_conflict(worker, monkeypatch, transition):
    clock = fake_time(monkeypatch, int(time.time()) + 100)
    assert worker.beat() == 60
    with connect_closing(worker.path) as conn, kb.write_txn(conn):
        if transition in ("end", "crash"):
            kb._end_run(conn, worker.task_id, outcome=transition)
        elif transition == "release":
            conn.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (worker.task_id,))
        elif transition == "successor":
            conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (worker.task_id,))
        elif transition == "foreign_lock":
            conn.execute("UPDATE tasks SET claim_lock='other-owner' WHERE id=?", (worker.task_id,))
        else:
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid()+1, worker.task_id))
    clock.now += 60
    worker.run()
    assert worker.stopped.is_set()
    assert len(events(worker)) == 1


def test_activity_beats_coalesce_and_reschedule_timer(worker, monkeypatch):
    clock = fake_time(monkeypatch, int(time.time()) + 100)
    assert worker.beat() == 60
    clock.now += 59
    with connect_closing(worker.path) as conn:
        assert kbd.heartbeat_worker(conn, worker.task_id, expected_run_id=worker.run_id,
                                    min_interval_seconds=60)
    assert len(events(worker)) == 1
    clock.now += 1
    assert worker.beat() == 60
    # An explicit note-bearing heartbeat remains supported and resets timer due time.
    clock.now += 10
    with connect_closing(worker.path) as conn:
        assert kbd.heartbeat_worker(conn, worker.task_id, note="progress", expected_run_id=worker.run_id)
    clock.now += 10
    assert worker.beat() == 50
    assert len(events(worker)) == 3


def test_real_thread_copied_context_and_stop(worker, monkeypatch):
    context = contextvars.ContextVar("worker_test", default="missing")
    context.set("owner")
    seen = threading.Event()
    original = worker.beat

    def beat():
        assert context.get() == "owner"
        result = original()
        seen.set()
        return result

    monkeypatch.setattr(worker, "beat", beat)
    worker.start()
    assert seen.wait(5)
    worker.stop()
    assert not worker.thread.is_alive()
    assert worker.thread.daemon
    assert len(events(worker)) == 1
    assert worker.beat() is None


def test_activity_bridge_and_timer_share_minute_budget(worker, monkeypatch):
    from tools import kanban_tools

    clock = fake_time(monkeypatch, int(time.time()) + 100)
    monkeypatch.setattr(kanban_tools, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0)
    assert kanban_tools.heartbeat_current_worker_from_env()
    assert worker.beat() == 60
    assert len(events(worker)) == 1
    clock.now += 60
    assert worker.beat() == 60
    assert kanban_tools.heartbeat_current_worker_from_env()
    assert len(events(worker)) == 2


def test_busy_board_does_not_hold_worker_shutdown(worker):
    with sqlite3.connect(worker.path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            worker.beat()
        assert time.monotonic() - started < 3
    assert not events(worker)


@pytest.mark.parametrize("bad_env", ["chat", "missing_lock", "bad_run", "delegate", "cron"])
def test_nonworkers_get_no_timer(worker, monkeypatch, bad_env):
    if bad_env == "chat":
        monkeypatch.delenv("HERMES_KANBAN_TASK")
    elif bad_env == "missing_lock":
        monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK")
    elif bad_env == "bad_run":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-a-run")
    elif bad_env == "delegate":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    else:
        from agent.delegation_context import non_dispatcher_owned_context
        with non_dispatcher_owned_context():
            assert timer.start_worker_heartbeat() is None
        return
    assert timer.start_worker_heartbeat() is None


def test_deleted_board_not_recreated(worker):
    worker.path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        worker.beat()
    assert not worker.path.exists()


def test_fork_identity_never_beats(worker, monkeypatch):
    monkeypatch.setattr(timer.os, "getpid", lambda: worker.pid + 1)
    assert worker.beat() is None
    assert not events(worker)


@pytest.mark.parametrize("exit_kind", ["normal", "crash", "hard_exit"])
def test_no_timer_process_survives_worker_exit(worker, exit_kind):
    code = '''
import os, sys, threading
from pathlib import Path
from hermes_cli.kanban_worker_heartbeat import WorkerHeartbeat
w=WorkerHeartbeat(Path(sys.argv[1]),sys.argv[2],int(sys.argv[3]),sys.argv[4])
ready=threading.Event()
original=w.beat
def beat():
    result=original()
    ready.set()
    return result
w.beat=beat
w.start()
assert ready.wait(5)
if sys.argv[5]=='hard_exit': os._exit(0)
if sys.argv[5]=='crash': raise RuntimeError('simulated worker crash')
'''
    result = subprocess.run([sys.executable, "-c", code, str(worker.path), worker.task_id,
                             str(worker.run_id), worker.claim_lock, exit_kind],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == (1 if exit_kind == "crash" else 0)
    assert len(events(worker)) == 1
