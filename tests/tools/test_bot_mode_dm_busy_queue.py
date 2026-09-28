"""A held Bot Chat is fast-acked into fleet_messages_v1. The old 1800s retry
loop is the bug: the sender must see a queued reply in under 5s.

The write itself is a subprocess of the source-checkout launcher
(``.hermes/bin/hermes --run-module tools.fleet_message_drain enqueue``), keyed
by the delivery envelope id. Queueing happens only when the recipient drain is
on; otherwise the reply is an immediate ``target_busy``.
"""

import io
import json
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from tools import bot_live_delivery as live
from tools import bot_mode_dm, bot_relay, fleet_message_drain as fmd

_REFUSAL_STDERR = "hermes-refusal-reason: SESSION_NOT_OWNED\nCe chat est occupé.\n"


def _enqueue_completed(argv):
    output = io.StringIO()
    with redirect_stdout(output):
        rc = fmd.main(argv[argv.index("enqueue"):])
    return subprocess.CompletedProcess(argv, rc, stdout=output.getvalue(), stderr="")


def test_busy_opted_in_target_is_enqueued_on_first_attempt(tmp_path, monkeypatch, capsys):
    """Adapted from tb-king: the launcher subprocess stays, the writer is create-only.

    The fresh-uuid ``enqueue_message`` field set, the 32-hex doc id, the 1.5s store
    timeout, and the "No need to resend" prose asserted the weaker writer. This asserts
    the envelope id, ``reply_relayed`` false, and ``enqueue_busy_dm`` instead.
    """
    from tools.fleet_message_enqueue import queued_ack

    home, dm_file = _setup(tmp_path, monkeypatch, queue_wait=1800)
    target = home / "profiles" / "ops"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("fleet_messages:\n  drain_on_turn_end: true\n  target: live\n")
    assert fmd.drain_config_for_home(home) is None
    assert fmd.drain_config_for_home(target) is not None
    assert fmd.drain_config_for_home(home) is None
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)
    seen = []

    def fake_enqueue(*, sender, recipient, body, message_id=None, writer=None, reader=None):
        seen.append({"sender": sender, "recipient": recipient, "body": body, "message_id": message_id})
        return message_id

    monkeypatch.setattr("tools.fleet_message_enqueue.enqueue_busy_dm", fake_enqueue)
    calls = []
    enqueue_calls = []

    def held(argv, **kwargs):
        if "tools.fleet_message_drain" in argv:
            enqueue_calls.append((argv, kwargs))
            return _enqueue_completed(argv)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", held)
    started = time.monotonic()
    rc = bot_mode_dm._run_delivery(["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
                                   profile_home=target, author={"id": "bot:tb-cndr", "name": "tb-cndr"})
    assert rc == 0 and time.monotonic() - started < 5
    assert len(calls) == 1 and len(enqueue_calls) == 1 and len(seen) == 1
    source_launcher = Path(bot_mode_dm.__file__).resolve().parents[1] / ".hermes" / "bin" / "hermes"
    expected_launcher = str(source_launcher) if source_launcher.is_file() else "hermes"
    envelope_id = bot_mode_dm._generation_delivery_id(str(dm_file), 0)
    assert enqueue_calls[0][0][:3] == [expected_launcher, "--run-module", "tools.fleet_message_drain"]
    assert enqueue_calls[0][0][3:] == ["enqueue", "--from", "tb-cndr", "--to-home", str(target),
                                       "--body-file", str(dm_file), "--message-id", envelope_id]
    assert enqueue_calls[0][1]["timeout"] == 5
    assert isinstance(enqueue_calls[0][1].get("env"), dict)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued" and payload["reply_relayed"] is False
    assert payload["message_id"] == envelope_id
    assert payload["reply"] == queued_ack(envelope_id)
    assert seen[0] == {"sender": "tb-cndr", "recipient": "ops", "body": "hi", "message_id": envelope_id}
    assert not dm_file.exists()

    (target / "config.yaml").write_text("fleet_messages:\n  drain_on_turn_end: false\n")
    before = len(seen)
    assert fmd.main(["enqueue", "--from", "tb-cndr", "--to-home", str(target),
                     "--body-file", str(dm_file), "--message-id", envelope_id]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["status"] == "disabled" and disabled["reply_relayed"] is False
    assert len(seen) == before


def test_busy_queue_uses_delivery_venv_when_store_python_cannot_read_config(tmp_path, monkeypatch, capsys):
    """Adapted: the parent does not read config via ``drain_config_for_home``.

    Admission still requires a readable drain-on config in this interpreter; the
    launcher argv gains ``--message-id`` so the write is create-only. The delivery
    venv python is still not the enqueue interpreter.
    """
    home, dm_file = _setup(tmp_path, monkeypatch, queue_wait=1800)
    target = home / "profiles" / "ops"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(
        "fleet_messages:\n  drain_on_turn_end: true\n  emulator_host: 127.0.0.1:1\n",
        encoding="utf-8",
    )
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = test\n")
    for name in ("hermes", "python"):
        (venv / "bin" / name).write_text("stub\n")
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)
    monkeypatch.setattr(fmd, "drain_config_for_home", lambda h: (_ for _ in ()).throw(ImportError("yaml")))
    monkeypatch.setattr(bot_mode_dm.time, "sleep", lambda seconds: pytest.fail("entered slice wait"))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "tools.fleet_message_drain" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout='{"status":"queued","message_id":"m1"}', stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", fake_run)
    started = time.monotonic()
    rc = bot_mode_dm._run_delivery([str(venv / "bin" / "hermes"), "-p", "ops"], str(dm_file),
                                   stdin_file=False, profile_home=target, author={"id": "bot:tb-cndr"})
    assert rc == 0 and time.monotonic() - started < 5
    assert len(calls) == 2
    source_launcher = Path(bot_mode_dm.__file__).resolve().parents[1] / ".hermes" / "bin" / "hermes"
    expected_launcher = str(source_launcher) if source_launcher.is_file() else str(venv / "bin" / "hermes")
    envelope_id = bot_mode_dm._generation_delivery_id(str(dm_file), 0)
    assert calls[1][0] == [expected_launcher, "--run-module", "tools.fleet_message_drain",
                           "enqueue", "--from", "tb-cndr", "--to-home", str(target),
                           "--body-file", str(dm_file), "--message-id", envelope_id]
    assert str(venv / "bin" / "python") not in calls[1][0], "venv python imports the stale editable workspace"
    assert calls[1][1]["timeout"] == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["message_id"] == "m1" and payload["status"] == "queued" and payload["reply_relayed"] is False
    assert not dm_file.exists()


def test_nonbusy_delivery_does_not_start_enqueue_subprocess(tmp_path, monkeypatch, capsys):
    home, dm_file = _setup(tmp_path, monkeypatch)
    target = home / "profiles" / "ops"
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)
    monkeypatch.setattr(fmd, "drain_config_for_home", lambda h: pytest.fail("eager opt-in check"))
    calls = []

    def delivered(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="delivered", stderr="")

    monkeypatch.setattr(subprocess, "run", delivered)
    assert bot_mode_dm._run_delivery(["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
                                     profile_home=target, author={"id": "bot:tb-cndr"}) == 0
    assert len(calls) == 1 and "tools.fleet_message_drain" not in calls[0]
    assert capsys.readouterr().out == "delivered"


def _setup(tmp_path, monkeypatch, *, slice_seconds=0.05, queue_wait=5.0):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_mode_dm, "_BUSY_SLICE_SECONDS", slice_seconds)
    monkeypatch.setattr(bot_relay, "dm_queue_wait_seconds", lambda: queue_wait)
    dm_file = tmp_path / "message.txt"
    dm_file.write_text("hi", encoding="utf-8")
    return home, dm_file


def test_message_agent_fast_acks_a_held_session(tmp_path, monkeypatch, capsys):
    """A held Bot Chat is queued to fleet_messages_v1 on the first refusal, under 5s.

    Adapted for the launcher subprocess: the parent no longer calls ``enqueue_busy_dm``
    itself. The checkout CLI does, and the sender-facing payload is status, message_id,
    and ``reply_relayed`` false, with ``queued (<id>)`` as the reply text.
    """
    from tools.fleet_message_enqueue import queued_ack

    home, dm_file = _setup(tmp_path, monkeypatch, queue_wait=30.0)
    ops = home / "profiles" / "ops"
    ops.mkdir(parents=True)
    (ops / "config.yaml").write_text(
        "fleet_messages:\n  drain_on_turn_end: true\n  emulator_host: 127.0.0.1:1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)
    seen = {}

    def fake_enqueue(*, sender, recipient, body, message_id=None, writer=None, reader=None):
        seen.update(sender=sender, recipient=recipient, body=body, message_id=message_id)
        return "fm-held"

    monkeypatch.setattr("tools.fleet_message_enqueue.enqueue_busy_dm", fake_enqueue)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "tools.fleet_message_drain" in argv:
            return _enqueue_completed(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", fake_run)

    started = time.monotonic()
    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
        profile_home=home / "profiles" / "ops",
        author={"name": "tb-cndr", "id": "bot:tb-cndr", "is_bot": True},
    )
    elapsed = time.monotonic() - started

    assert rc == 0
    assert len(calls) == 2 and "tools.fleet_message_drain" not in calls[0]
    assert elapsed < 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued" and payload["reply_relayed"] is False
    assert payload["message_id"] == "fm-held"
    assert payload["reply"] == queued_ack("fm-held")
    assert seen["recipient"] == "ops"
    assert seen["sender"] == "tb-cndr"
    assert seen["body"] == "hi"


def test_message_agent_does_not_claim_queued_when_the_registry_write_fails(tmp_path, monkeypatch, capsys):
    """A failed fleet write is an immediate target_busy, not a 30-minute wait and not a queued ack."""
    from tools.fleet_message_enqueue import FleetEnqueueError

    home, dm_file = _setup(tmp_path, monkeypatch, queue_wait=30.0)
    ops = home / "profiles" / "ops"
    ops.mkdir(parents=True)
    (ops / "config.yaml").write_text(
        "fleet_messages:\n  drain_on_turn_end: true\n  emulator_host: 127.0.0.1:1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)

    def fake_enqueue(**kwargs):
        raise FleetEnqueueError("firestore down")

    monkeypatch.setattr("tools.fleet_message_enqueue.enqueue_busy_dm", fake_enqueue)

    def fake_run(argv, **kwargs):
        if "tools.fleet_message_drain" in argv:
            return _enqueue_completed(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", fake_run)
    started = time.monotonic()
    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
        profile_home=home / "profiles" / "ops",
    )
    elapsed = time.monotonic() - started

    assert rc == 1
    assert elapsed < 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "target_busy"
    assert "queued (" not in payload["error"]


def test_queued_dm_matches_fleet_messages_v1_expiry():
    """expires_at is created_at plus 24 hours, and the drain identity is the profile name."""
    import datetime

    from tools.fleet_message_enqueue import build_queued_dm, fleet_handle

    doc = build_queued_dm(sender="tb-cndr", recipient="tb-king", body="hello")
    created = datetime.datetime.strptime(doc["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    expires = datetime.datetime.strptime(doc["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert expires - created == datetime.timedelta(hours=24)
    assert doc["status"] == "queued"
    assert doc["to"] == "tb-king"
    assert doc["kind"] == "dm"
    assert doc["schema_version"] == 2
    assert doc["last_error"] == "target_busy"
    assert fleet_handle("bot:sheldon/tb-king", fallback="sender") == "tb-king"


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
