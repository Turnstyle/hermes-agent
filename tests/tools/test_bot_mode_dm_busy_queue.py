"""Tests: message_agent queues behind a recipient whose Bot Chat is held by ANOTHER surface
(plain ``hermes chat`` CLI, a gateway bridge turn — SESSION_NOT_OWNED, not a live-delivery
mailbox owner) instead of dropping the message on the first refusal.

``tools.bot_mode_dm._run_delivery_locked``'s queue loop already retried a ``TurnBusyError`` from
its OWN turn-lock timeout; the bug is that ``_run_local_turn``'s separate SESSION_NOT_OWNED
refusal (a rival transport holds the CLI lease, not this module's lock) short-circuited the loop
on the very first attempt. These pin the fix: retry that refusal too, bounded by
``bot_mode.dm_queue_wait_seconds``, re-probing for a live mailbox owner between attempts.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from tools import bot_live_delivery as live
from tools import bot_mode_dm, bot_relay

_REFUSAL_STDERR = "hermes-refusal-reason: SESSION_NOT_OWNED\nCe chat est occupé.\n"


def _setup(tmp_path, monkeypatch, *, slice_seconds=0.05, queue_wait=5.0):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_mode_dm, "_BUSY_SLICE_SECONDS", slice_seconds)
    monkeypatch.setattr(bot_relay, "dm_queue_wait_seconds", lambda: queue_wait)
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("hi", encoding="utf-8")
    return home, dm_file


def test_message_agent_queues_behind_a_held_session_then_delivers(tmp_path, monkeypatch, capsys):
    """(a) Held for the first 2 attempts, free on the 3rd: the delivery still succeeds and the
    reply reaches stdout, having run the CLI exactly 3 times."""
    home, dm_file = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) <= 2:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)
        return subprocess.CompletedProcess(argv, 0, stdout="got it, thanks", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
        profile_home=home / "profiles" / "ops",
    )

    assert rc == 0
    assert len(calls) == 3
    assert capsys.readouterr().out == "got it, thanks"
    assert not dm_file.exists()


def test_message_agent_fails_loudly_after_the_full_queue_wait(tmp_path, monkeypatch, capsys):
    """(b) Held for the whole budget: a LOUD target_busy refusal, mentioning how long it queued,
    after a bounded number of attempts — not hundreds — and at least the configured wait."""
    slice_seconds, queue_wait = 0.05, 0.3
    home, dm_file = _setup(tmp_path, monkeypatch, slice_seconds=slice_seconds, queue_wait=queue_wait)
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", fake_run)

    started = time.monotonic()
    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
        profile_home=home / "profiles" / "ops",
    )
    elapsed = time.monotonic() - started

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "target_busy"
    assert "queu" in payload["error"].lower()
    # The final slice doesn't sleep before giving up, so elapsed may fall a bit short of the full
    # budget (by at most one slice) — never far short, and never long past it.
    assert queue_wait - slice_seconds <= elapsed < queue_wait + 2.0
    # ~ queue_wait / slice_seconds = 6 attempts; generous bound rules out a hot loop.
    assert 1 <= len(calls) <= 30
    assert not dm_file.exists()


def test_message_agent_hands_off_to_a_live_owner_that_appears_while_queued(tmp_path, monkeypatch):
    """(c) A live mailbox owner shows up between attempts: the NEXT loop iteration's
    ``_via_live_owner()`` re-probe takes over, and the CLI is never invoked again."""
    home, dm_file = _setup(tmp_path, monkeypatch)
    target = home / "profiles" / "ops"
    owner = dict(profile_home=str(target), session_id="bot", lease_id="lease", live_session_id="live")
    owners = iter([None])  # first probe (before the loop, and the first retry probe): no owner yet
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: next(owners, owner))

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops"], str(dm_file), stdin_file=False, profile_home=target,
    )

    assert rc == 0
    assert len(calls) == 1


def test_direct_call_contract_still_fails_target_busy_immediately(tmp_path, capsys):
    """(d) The pre-existing direct-call contract (busy_raises defaults False, no profile_home so
    there is nothing to queue behind) is unchanged: immediate target_busy, single attempt."""
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("hi", encoding="utf-8")
    import sys

    child = tmp_path / "owned.py"
    child.write_text(
        "import sys\n"
        "print('Session abc already has a live owner (desktop, pid 1).', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )

    rc = bot_mode_dm._run_delivery([sys.executable, str(child), "-p", "ops"], str(dm_file), stdin_file=False)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "target_busy"
