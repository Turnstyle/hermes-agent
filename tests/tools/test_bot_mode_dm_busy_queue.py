"""A held Bot Chat is fast-acked into fleet_messages_v1. The old 1800s retry
loop is the bug: the sender must see queued (message_id) in under 5s.
"""

import json
import datetime
import io
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from tools import bot_live_delivery as live
from tools import bot_mode_dm, bot_relay, fleet_message_drain as fmd

_REFUSAL_STDERR = "hermes-refusal-reason: SESSION_NOT_OWNED\nCe chat est occupé.\n"


def test_busy_opted_in_target_is_enqueued_on_first_attempt(tmp_path, monkeypatch, capsys):
    home, dm_file = _setup(tmp_path, monkeypatch, queue_wait=1800)
    target = home / "profiles" / "ops"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("fleet_messages:\n  drain_on_turn_end: true\n  target: live\n")
    assert fmd.drain_config_for_home(home) is None
    assert fmd.drain_config_for_home(target) is not None
    assert fmd.drain_config_for_home(home) is None
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda h: None)
    writes = []
    monkeypatch.setattr(fmd, "store_for", lambda config: type("Store", (), {
        "create": lambda self, doc_id, fields: writes.append((doc_id, fields, config))})())
    calls = []
    enqueue_calls = []

    def held(argv, **kwargs):
        if "tools.fleet_message_drain" in argv:
            enqueue_calls.append((argv, kwargs))
            output = io.StringIO()
            with redirect_stdout(output):
                assert fmd.main(argv[argv.index("enqueue"):]) == 0
            return subprocess.CompletedProcess(argv, 0, stdout=output.getvalue(), stderr="")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=_REFUSAL_STDERR)

    monkeypatch.setattr(subprocess, "run", held)
    started = time.monotonic()
    rc = bot_mode_dm._run_delivery(["hermes", "-p", "ops"], str(dm_file), stdin_file=False,
                                   profile_home=target, author={"id": "bot:tb-cndr", "name": "tb-cndr"})
    assert rc == 0 and time.monotonic() - started < 5
    assert len(calls) == 1 and len(enqueue_calls) == 1 and len(writes) == 1
    source_launcher = Path(bot_mode_dm.__file__).resolve().parents[1] / ".hermes" / "bin" / "hermes"
    expected_launcher = str(source_launcher) if source_launcher.is_file() else "hermes"
    assert enqueue_calls[0][0][:3] == [expected_launcher, "--run-module", "tools.fleet_message_drain"]
    payload = json.loads(capsys.readouterr().out)
    doc_id, fields, config = writes[0]
    assert payload["status"] == "queued" and payload["message_id"] == doc_id
    assert len(doc_id) == 32 and int(doc_id, 16) >= 0
    assert set(fields) == {"message_id", "from", "to", "kind", "body", "status", "attempts",
                           "created_at", "updated_at", "expires_at"}
    assert (fields["from"], fields["to"], fields["body"], fields["status"], fields["attempts"]) == (
        "tb-cndr", "ops", "hi", "queued", 0)
    assert fields["message_id"] == doc_id and fields["created_at"] == fields["updated_at"]
    assert fmd.parse_ts(fields["expires_at"]) - fmd.parse_ts(fields["created_at"]) == datetime.timedelta(hours=24)
    assert config.timeout_seconds <= 1.5
    assert "No need to resend" in payload["reply"]
    assert not dm_file.exists()


def test_busy_queue_uses_delivery_venv_when_store_python_cannot_read_config(tmp_path, monkeypatch, capsys):
    home, dm_file = _setup(tmp_path, monkeypatch, queue_wait=1800)
    target = home / "profiles" / "ops"
    target.mkdir(parents=True)
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
    assert calls[1][0] == [expected_launcher, "--run-module", "tools.fleet_message_drain",
                           "enqueue", "--from", "tb-cndr", "--to-home", str(target),
                           "--body-file", str(dm_file)]
    assert str(venv / "bin" / "python") not in calls[1][0], "venv python imports the stale editable workspace"
    assert calls[1][1]["timeout"] == 5
    assert json.loads(capsys.readouterr().out)["message_id"] == "m1"
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
    """A held Bot Chat is queued to fleet_messages_v1 on the first refusal, under 5s,
    and the sender sees queued (message_id). The CLI is not retried for the old budget."""
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

    def fake_enqueue(*, sender, recipient, body, message_id=None, writer=None):
        seen.update(sender=sender, recipient=recipient, body=body)
        return "fm-held"

    monkeypatch.setattr("tools.fleet_message_enqueue.enqueue_busy_dm", fake_enqueue)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
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
    assert len(calls) == 1
    assert elapsed < 5
    assert capsys.readouterr().out.strip() == queued_ack("fm-held")
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
