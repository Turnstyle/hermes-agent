"""Busy DM fast-ack is honest: queued only when the recipient's turn-end drain is enabled."""

from __future__ import annotations

import io
import json
import subprocess
import time
from contextlib import redirect_stdout

from tools import bot_live_delivery as live
from tools import bot_mode_dm, bot_relay, fleet_message_drain as fmd

_REFUSAL_STDERR = "hermes-refusal-reason: SESSION_NOT_OWNED\nCe chat est occupé.\n"


def _setup(tmp_path, monkeypatch, *, queue_wait=5.0):
    home = tmp_path / ".hermes"
    home.mkdir()
    ops = home / "profiles" / "ops"
    ops.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_mode_dm, "_BUSY_SLICE_SECONDS", 0.05)
    monkeypatch.setattr(bot_relay, "dm_queue_wait_seconds", lambda: queue_wait)
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("hi", encoding="utf-8")
    return home, ops, dm_file


def test_busy_dm_is_queued_only_when_recipient_drain_is_enabled(tmp_path, monkeypatch, capsys):
    from tools.fleet_message_enqueue import queued_ack

    home, ops, dm_file = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)
    enqueued = []

    def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return "fm-test-id"

    monkeypatch.setattr("tools.fleet_message_enqueue.enqueue_busy_dm", fake_enqueue)

    def fake_run(argv, **kwargs):
        if "tools.fleet_message_drain" in argv:
            output = io.StringIO()
            with redirect_stdout(output):
                rc = fmd.main(list(argv[argv.index("enqueue"):]))
            return subprocess.CompletedProcess(argv, rc, stdout=output.getvalue(), stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", fake_run)
    argv = ["hermes", "-p", "ops"]

    (ops / "config.yaml").write_text(
        "fleet_messages:\n  drain_on_turn_end: true\n  emulator_host: 127.0.0.1:1\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    rc = bot_mode_dm._run_delivery(argv, str(dm_file), stdin_file=False, profile_home=ops)
    assert rc == 0
    assert time.monotonic() - started < 5
    assert enqueued
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued" and payload["reply_relayed"] is False
    assert payload["message_id"] == "fm-test-id"
    assert payload["reply"] == queued_ack("fm-test-id")

    enqueued.clear()
    dm_file.write_text("hi", encoding="utf-8")
    (ops / "config.yaml").write_text("fleet_messages:\n  drain_on_turn_end: false\n", encoding="utf-8")
    rc = bot_mode_dm._run_delivery(argv, str(dm_file), stdin_file=False, profile_home=ops)
    assert rc == 1
    assert not enqueued
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "target_busy"
