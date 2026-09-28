"""Sender-visible wording for single-owner / turn-lock busy refusals (redtape)."""

from __future__ import annotations

import json
import sys
import threading

import pytest

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from tools import bot_mode_dm, bot_relay
from tools.bot_relay import TurnBusyError, acquire_turn_lock, turn_lock_path


def _continue_card_warning(text: str) -> bool:
    lower = text.lower()
    return "continue" in lower and "card" in lower and "park" in lower


def test_turn_busy_error_wording():
    err = TurnBusyError("prof", 3.2)
    text = str(err)
    assert "NOT delivered" not in text
    assert _continue_card_warning(text)
    assert err.reason == "target_busy"


def test_run_local_turn_session_not_owned_refusal_wording(tmp_path, capsys):
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("hi", encoding="utf-8")
    child = tmp_path / "owned.py"
    child.write_text(
        "import sys\n"
        "print('hermes-refusal-reason: SESSION_NOT_OWNED', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    rc = bot_mode_dm._run_local_turn(
        [sys.executable, str(child), "-p", "ops"], str(dm_file), busy_raises=False
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "target_busy"
    assert "NOT delivered" not in payload["error"]
    assert _continue_card_warning(payload["error"])


@pytest.mark.platforms("linux", "macos")
def test_second_delivery_still_busy_refusal(root, tmp_path, monkeypatch, capsys):
    if fcntl is None:
        pytest.skip("fcntl unavailable")
    """Lock timeout still refuses (rc 1, target_busy) — no concurrent turn."""
    import os

    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 0.2)
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")

    held = threading.Event()
    release = threading.Event()

    def _hold_flock(path, hold_event, release_event):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        hold_event.set()
        release_event.wait(timeout=10)
        os.close(fd)

    t = threading.Thread(
        target=_hold_flock, args=(turn_lock_path(home, "ops"), held, release)
    )
    t.start()
    assert held.wait(timeout=5)
    turn_started = threading.Event()

    def _fake_run(argv, **kwargs):
        turn_started.set()
        class _P:
            returncode = 0
            stdout = "should not run"
            stderr = ""
        return _P()

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
    try:
        rc = bot_mode_dm._delivery_main(
            ["--run-delivery", "query-file", str(dm), "hermes", "-p", "ops", "chat"]
        )
        assert rc == 1
        assert not turn_started.is_set(), "must not start a turn while lock is held"
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["reason"] == "target_busy"
        assert "NOT delivered" not in payload["error"]
        assert _continue_card_warning(payload["error"])
    finally:
        release.set()
        t.join(timeout=5)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    return r
