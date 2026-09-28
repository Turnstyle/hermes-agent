"""A reported Bot Chat turn frees its profile lock before the child exits."""

import os
import threading
import time
from pathlib import Path

import pytest

from tools import bot_mode_dm
from tools.bot_relay import BOT_CHAT_TURN_ARGS, TurnBusyError, acquire_turn_lock

pytestmark = pytest.mark.platforms("posix")


def test_local_delivery_releases_lock_at_report_while_child_lingers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    launcher = tmp_path / "hermes"
    launcher.write_text("""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
report = Path(os.environ.pop('HERMES_QUIET_TURN_REPORT_FILE'))
assert os.environ['HERMES_BOT_CHAT_DELIVERY_PROFILE'] == 'ops'
Path(str(report) + '.ready').write_text('ready')
time.sleep(0.7)
report.write_text(json.dumps({'pid': os.getpid(), 'exit_code': 0, 'error': '', 'reply': 'first'}))
time.sleep(1.2)
print('final reply')
""")
    launcher.chmod(0o755)
    dm = tmp_path / "dm.txt"
    dm.write_text("hello")
    report = Path(str(dm) + ".turn.json")
    result = []
    worker = threading.Thread(target=lambda: result.append(bot_mode_dm._run_delivery_locked(
        [str(launcher), "-p", "ops", *BOT_CHAT_TURN_ARGS], str(dm), stdin_file=False)))
    worker.start()
    try:
        until = time.monotonic() + 5
        while not Path(str(report) + ".ready").exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert Path(str(report) + ".ready").exists()
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", 0.1):
                pass
        while not report.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert report.exists() and worker.is_alive()
        with acquire_turn_lock(home, "ops", 0.5):
            assert worker.is_alive()
        worker.join(timeout=5)
        assert result == [0]
    finally:
        worker.join(timeout=5)


def test_failed_report_keeps_lock_through_policy_retry(tmp_path, monkeypatch):
    import sys

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    launcher = tmp_path / "hermes"
    launcher.write_text(f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
report = Path(os.environ.pop('HERMES_QUIET_TURN_REPORT_FILE'))
retry = bool(os.environ.get('HERMES_RESUME_UNANSWERED_TURN'))
if not retry:
    report.write_text(json.dumps({{'pid': os.getpid(), 'exit_code': 1, 'error': 'rate limit', 'reply': ''}}))
    time.sleep(1.0)
    sys.stderr.write('Error code: 429 - rate limit exceeded\\n')
    sys.exit(1)
print('ok')
""")
    launcher.chmod(0o755)
    dm = tmp_path / "dm.txt"
    dm.write_text("hello")
    report = Path(str(dm) + ".turn.json")
    result = []
    worker = threading.Thread(target=lambda: result.append(bot_mode_dm._run_delivery_locked(
        [str(launcher), "-p", "ops", *BOT_CHAT_TURN_ARGS], str(dm), stdin_file=False)))
    worker.start()
    try:
        until = time.monotonic() + 5
        while not report.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert report.exists() and worker.is_alive()
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", 0.3):
                pass
        worker.join(timeout=10)
        assert result == [0]
    finally:
        worker.join(timeout=10)
