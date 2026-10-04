"""The turn-lock holder sidecar and the watchdog that reports long-held locks (never kills)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import bot_relay, bot_relay_lock_watchdog as watchdog
from tools.bot_relay import acquire_turn_lock, lock_holder_path, turn_lock_path

pytestmark = pytest.mark.platforms("posix")

REPO = Path(__file__).resolve().parents[2]

_HOLDER = """\
import sys, time
sys.path.insert(0, {repo!r})
from tools.bot_relay import acquire_turn_lock
with acquire_turn_lock({root!r}, 'ops', 5):
    print('held', flush=True)
    time.sleep(60)
"""


@pytest.fixture
def held_lock(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER.format(repo=str(REPO), root=str(tmp_path))],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    yield tmp_path, proc
    proc.kill()
    proc.wait()


def test_sidecar_is_written_on_acquire_and_removed_on_release(tmp_path):
    sidecar = lock_holder_path(turn_lock_path(tmp_path, "ops"))
    with acquire_turn_lock(tmp_path, "ops", 1):
        data = json.loads(sidecar.read_text())
        assert data["pid"] == os.getpid() and abs(data["since"] - time.time()) < 60
    assert not sidecar.exists()


def test_lock_still_works_when_the_sidecar_cannot_be_written(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_relay, "atomic_json_write", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    with acquire_turn_lock(tmp_path, "ops", 1):
        pass


def test_old_held_lock_is_reported_with_holder_pid_and_command(held_lock):
    root, proc = held_lock
    rows = watchdog.find_stale_locks(root, 15 * 60, now=lambda: time.time() + 3600,
                                     cmdline=lambda pid: f"cmd-of-{pid}")
    assert [(r["profile"], r["pid"], r["command"]) for r in rows] == [("ops", proc.pid, f"cmd-of-{proc.pid}")]
    assert rows[0]["age_seconds"] >= 3600 - 60


def test_recently_taken_lock_is_not_reported(held_lock):
    root, _ = held_lock
    assert watchdog.find_stale_locks(root, 15 * 60) == []


def test_released_lock_with_a_leftover_sidecar_is_not_reported(tmp_path):
    lock = turn_lock_path(tmp_path, "ops")
    lock.parent.mkdir(parents=True)
    lock.touch()
    lock_holder_path(lock).write_text(json.dumps({"pid": os.getpid(), "since": 1.0}))
    assert watchdog.find_stale_locks(tmp_path, 60, now=lambda: time.time() + 3600) == []


def test_held_lock_without_a_sidecar_ages_from_the_lock_mtime(held_lock):
    root, proc = held_lock
    lock_holder_path(turn_lock_path(root, "ops")).unlink()
    rows = watchdog.find_stale_locks(root, 15 * 60, now=lambda: time.time() + 3600)
    assert len(rows) == 1 and rows[0]["pid"] is None


def test_cli_exits_1_and_never_signals_the_holder(held_lock):
    root, proc = held_lock
    sidecar = lock_holder_path(turn_lock_path(root, "ops"))
    data = json.loads(sidecar.read_text())
    data["since"] -= 3600
    sidecar.write_text(json.dumps(data))
    out = subprocess.run([sys.executable, "-m", "tools.bot_relay_lock_watchdog", "--root", str(root), "--json"],
                         cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1, out.stderr
    assert json.loads(out.stdout)[0]["pid"] == proc.pid
    assert proc.poll() is None


def test_cli_exits_0_when_nothing_is_stale(tmp_path):
    out = subprocess.run([sys.executable, "-m", "tools.bot_relay_lock_watchdog", "--root", str(tmp_path)],
                         cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout == ""
