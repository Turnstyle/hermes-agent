"""Parent delivery locks cover the turn, while successful quiet children may linger."""

import sys
import threading
import time
from pathlib import Path

import pytest

from tools import bot_mode_dm, bot_relay
from tools.bot_relay import BOT_CHAT_TURN_ARGS, TurnBusyError, acquire_turn_lock

pytestmark = pytest.mark.platforms("posix")


def _wait_for(path: Path, result=None):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), (path, result)


def _child_source(marker: Path, fail_first: bool, flip_first: bool) -> str:
    return f"""
import json, os, sys, time
from pathlib import Path
marker = Path({str(marker)!r})
report = Path(os.environ.pop('HERMES_QUIET_TURN_REPORT_FILE'))
retry = bool(os.environ.get('HERMES_RESUME_UNANSWERED_TURN'))
if {fail_first!r} and not retry:
    if {flip_first!r}:
        marker.with_suffix('.ready').write_text('ready')
        time.sleep(0.6)
        report.write_text(json.dumps({{'pid': os.getpid(), 'exit_code': 0, 'error': '', 'reply': 'first'}}))
        marker.with_suffix('.success_report').write_text('reported')
    else:
        marker.with_suffix('.failed_ready').write_text('ready')
    time.sleep(0.6)
    report.write_text(json.dumps({{'pid': os.getpid(), 'exit_code': 1, 'error': 'overloaded', 'reply': ''}}))
    marker.with_suffix('.failed_report').write_text('reported')
    time.sleep(0.8)
    sys.stderr.write('server error - overloaded\\n')
    sys.exit(1)
marker.with_suffix('.retry_ready' if retry else '.ready').write_text('ready')
time.sleep(0.6)
report.write_text(json.dumps({{'pid': os.getpid(), 'exit_code': 0, 'error': '', 'reply': 'first'}}))
marker.with_suffix('.retry_success_report' if retry else '.success_report').write_text('reported')
time.sleep(1.2)
print('final reply')
"""


def _start_delivery(surface, tmp_path, monkeypatch, fail_first, flip_first=False):
    marker = tmp_path / "child"
    source = _child_source(marker, fail_first, flip_first)
    result = []
    if surface == "local":
        launcher = tmp_path / "hermes"
        launcher.write_text(f"#!{sys.executable}\n{source}")
        launcher.chmod(0o755)
        dm = tmp_path / "dm.txt"
        dm.write_text("hello")
        target = lambda: bot_mode_dm._run_delivery_locked(
            [str(launcher), "-p", "ops", *BOT_CHAT_TURN_ARGS], str(dm), stdin_file=False)
    else:
        import tui_gateway.server as srv

        monkeypatch.setattr(bot_relay, "local_delivery_command",
                            lambda _profile, _tmp: [sys.executable, "-c", source])
        target = lambda: srv._methods["bot_relay.deliver"](1, {"profile": "ops", "message": "hello"})
    worker = threading.Thread(target=lambda: result.append(target()))
    worker.start()
    return marker, worker, result


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    profile = home / "profiles" / "ops"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("fleet_messages:\n  drain_on_turn_end: true\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.parametrize("surface", ["local", "relay"])
def test_successful_report_releases_parent_lock_during_linger(surface, home, tmp_path, monkeypatch, capsys):
    marker, worker, result = _start_delivery(surface, tmp_path, monkeypatch, False)
    try:
        _wait_for(marker.with_suffix(".ready"), result)
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", 0.1):
                pass
        _wait_for(marker.with_suffix(".success_report"))
        assert worker.is_alive()
        with acquire_turn_lock(home, "ops", 0.5):
            assert worker.is_alive()
        worker.join(timeout=8)
        assert not worker.is_alive()
        if surface == "local":
            assert result == [0]
            assert "final reply" in capsys.readouterr().out
        else:
            assert result[0]["result"]["reply"] == "final reply"
    finally:
        worker.join(timeout=8)


@pytest.mark.parametrize("surface", ["local", "relay"])
def test_failed_report_keeps_parent_lock_through_retry(surface, home, tmp_path, monkeypatch):
    marker, worker, result = _start_delivery(surface, tmp_path, monkeypatch, True)
    try:
        _wait_for(marker.with_suffix(".failed_report"), result)
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", 0.2):
                pass
        _wait_for(marker.with_suffix(".retry_ready"))  # retry has started under the same lock
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", 0.1):
                pass
        _wait_for(marker.with_suffix(".retry_success_report"))
        assert worker.is_alive()
        with acquire_turn_lock(home, "ops", 0.5):
            assert worker.is_alive()
        worker.join(timeout=8)
        assert not worker.is_alive()
        if surface == "local":
            assert result == [0]
        else:
            assert result[0]["result"]["reply"] == "final reply"
    finally:
        worker.join(timeout=8)


@pytest.mark.parametrize("surface", ["local", "relay"])
def test_later_failed_report_reacquires_lock_before_retry(surface, home, tmp_path, monkeypatch):
    marker, worker, result = _start_delivery(surface, tmp_path, monkeypatch, True, flip_first=True)
    try:
        _wait_for(marker.with_suffix(".success_report"))
        with acquire_turn_lock(home, "ops", 0.5):
            assert worker.is_alive()
        _wait_for(marker.with_suffix(".failed_report"))
        _wait_for(marker.with_suffix(".retry_ready"))
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", 0.1):
                pass
        _wait_for(marker.with_suffix(".retry_success_report"))
        with acquire_turn_lock(home, "ops", 0.5):
            assert worker.is_alive()
        worker.join(timeout=8)
        assert not worker.is_alive()
        if surface == "local":
            assert result == [0]
        else:
            assert result[0]["result"]["reply"] == "final reply"
    finally:
        worker.join(timeout=8)
