"""A Bot Chat DM child that hangs must never pin the recipient's turn lock or the sender's reply.

Regression for the 2026-10-03 incident: ``hermes -p <bot> chat -Q --query-file`` hung 10h14m at 0 CPU
in interpreter finalization (import-lock deadlock) after its turn ended; the delivery runner waited
with no timeout, held ``bot_relay/locks/<bot>.lock`` throughout, and queued DMs expired undelivered.

The synthetic child stands in for hermes: it records its start, writes the real turn report, then
hangs. Deadlines are shrunk to seconds through the module constants the production code reads.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import quiet_single_query as qsq
from tools import bot_mode_dm, bot_relay
from tools.bot_relay import BOT_CHAT_TURN_ARGS, TurnBusyError, acquire_turn_lock

pytestmark = pytest.mark.platforms("posix")

REPO = Path(__file__).resolve().parents[2]

_CHILD = """\
import os, signal, sys, time
sys.path.insert(0, {repo!r})
from pathlib import Path
base = {base!r}
report = os.environ.pop('HERMES_QUIET_TURN_REPORT_FILE', None)
with open(base + '.starts', 'a') as fh:
    fh.write('%d resume=%d\\n' % (os.getpid(), bool(os.environ.get('HERMES_RESUME_UNANSWERED_TURN'))))
Path(base + '.pid').write_text(str(os.getpid()))

def _term(*_):
    Path(base + '.got_term').write_text('1')
    if not {ignore_term!r}:
        os._exit(143)

signal.signal(signal.SIGTERM, _term)
if {report!r}:
    from hermes_cli.quiet_single_query import write_turn_report
    write_turn_report(report, exit_code=0, reply='the reply')
    Path(base + '.reported').write_text('1')
while True:
    time.sleep(0.05)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "profiles" / "ops").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def tiny_deadlines(monkeypatch):
    monkeypatch.setattr(qsq, "POST_REPORT_EXIT_SLACK_SECONDS", 0.0)
    monkeypatch.setattr(qsq, "POST_REPORT_EXIT_CAP_SECONDS", 1.5)
    monkeypatch.setattr(qsq, "CHILD_TERM_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(bot_relay, "TURN_ATTEMPT_TIMEOUT_SECONDS", 1.5)


def _launcher(tmp_path, *, report, ignore_term):
    marker = str(tmp_path / "child")
    launcher = tmp_path / "hermes"
    launcher.write_text(f"#!{sys.executable}\n" + _CHILD.format(
        repo=str(REPO), base=marker, report=report, ignore_term=ignore_term))
    launcher.chmod(0o755)
    return launcher, marker


def _starts(marker):
    path = Path(marker + ".starts")
    return path.read_text().splitlines() if path.exists() else []


def _wait_for(path, timeout=15):
    deadline = time.monotonic() + timeout
    while not Path(path).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Path(path).exists(), path


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid, timeout=15):
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def _deliver(launcher, tmp_path):
    dm = tmp_path / "dm.txt"
    dm.write_text("hello")
    result = []
    worker = threading.Thread(target=lambda: result.append(bot_mode_dm._run_delivery_locked(
        [str(launcher), "-p", "ops", *BOT_CHAT_TURN_ARGS], str(dm), stdin_file=False)))
    worker.start()
    return worker, result


@pytest.mark.parametrize("ignore_term", [False, True], ids=["sigterm", "sigkill-needed"])
def test_reported_child_hung_in_finalization_frees_lock_replies_once_and_is_killed(
        home, tmp_path, capsys, ignore_term):
    launcher, marker = _launcher(tmp_path, report=True, ignore_term=ignore_term)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    worker, result = _deliver(launcher, tmp_path)
    try:
        _wait_for(marker + ".reported")
        with acquire_turn_lock(home, "ops", 1.0):  # (i) the lock is free long before the child exits
            assert worker.is_alive()
        pid = int(Path(marker + ".pid").read_text())
        worker.join(timeout=15)
        assert not worker.is_alive()
        assert result == [0]
        assert capsys.readouterr().out.count("the reply") == 1  # (ii) relayed once...
        assert len(_starts(marker)) == 1  # ...and the DM was never re-run
        assert _wait_gone(pid)  # (iii) the exact child is dead after the kill deadline
        assert Path(marker + ".got_term").exists()  # SIGTERM first, before any SIGKILL
        assert bystander.poll() is None  # (iv) nothing else was signalled
    finally:
        bystander.kill()
        bystander.wait()
        worker.join(timeout=15)


@pytest.mark.parametrize("ignore_term", [False, True], ids=["sigterm", "sigkill-needed"])
def test_child_that_never_reports_hits_the_turn_deadline_loudly(home, tmp_path, capsys, ignore_term):
    launcher, marker = _launcher(tmp_path, report=False, ignore_term=ignore_term)
    worker, result = _deliver(launcher, tmp_path)
    try:
        _wait_for(marker + ".starts")
        with pytest.raises(TurnBusyError):  # the lock is held while the turn is still running
            with acquire_turn_lock(home, "ops", 0.1):
                pass
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert result == [1]
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["reason"] == "turn_timeout" and out["reply_relayed"] is False
        assert "NOT delivered" in out["error"] and "NOT completed" in out["error"]
        # The retry policy re-ran it once, resuming the already-persisted DM row.
        starts = _starts(marker)
        assert [line.split()[1] for line in starts] == ["resume=0", "resume=1"]
        assert all(_wait_gone(int(line.split()[0])) for line in starts)
        with acquire_turn_lock(home, "ops", 0.5):
            pass
    finally:
        worker.join(timeout=30)


def test_reaper_outlives_the_spawner_that_returned_and_exited(tmp_path):
    """The delivery runner is detached and exits right after replying; the kill must not die with it."""
    launcher, marker = _launcher(tmp_path, report=True, ignore_term=True)
    parent = textwrap.dedent(f"""
        import os, sys, tempfile
        sys.path.insert(0, {str(REPO)!r})
        from hermes_cli import quiet_single_query as qsq
        proc = qsq.run_reported_turn([{str(launcher)!r}], env=os.environ, report_path=os.path.join(
            {str(tmp_path)!r}, 'turn.json'), timeout=30, exit_grace=None, exit_wait=1.0, term_grace=1.0)
        print(proc.returncode, proc.stdout, flush=True)
    """)
    done = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=60)
    assert done.stdout.split() == ["0", "the", "reply"], done.stderr
    pid = int(Path(marker + ".pid").read_text())
    assert _alive(pid)  # the spawner is gone, the child is still hung
    assert _wait_gone(pid)
    assert Path(marker + ".got_term").exists()


class _FakeProcesses:
    """Injectable clock + ps + kill for ``reap_after``."""

    def __init__(self, stamps, exits_on=None):
        self.t = 100.0
        self.stamps = dict(stamps)
        self.exits_on = exits_on
        self.sent = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def probe(self, pid):
        return self.stamps.get(pid)

    def send(self, pid, sig):
        self.sent.append((pid, sig))
        if sig == self.exits_on:
            self.stamps.pop(pid, None)


def _reap(fake, pid=4242, stamp="Sat Oct  3 12:00:00 2026"):
    return qsq.reap_after(pid, stamp, 130.0, 10.0, now=fake.now, sleep=fake.sleep, probe=fake.probe, send=fake.send)


def test_reaper_never_signals_a_recycled_pid():
    fake = _FakeProcesses({4242: "Sun Oct  4 01:00:00 2026"})  # same PID, different process
    assert _reap(fake) == "gone"
    assert fake.sent == []


def test_reaper_signals_only_the_exact_pid_term_then_kill():
    fake = _FakeProcesses({4242: "Sat Oct  3 12:00:00 2026", 99: "bystander"})
    assert _reap(fake) == "killed"
    assert fake.sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert fake.t >= 140.0  # SIGKILL only after the full grace window


def test_reaper_skips_sigkill_when_sigterm_was_enough():
    fake = _FakeProcesses({4242: "Sat Oct  3 12:00:00 2026"}, exits_on=signal.SIGTERM)
    assert _reap(fake) == "terminated"
    assert fake.sent == [(4242, signal.SIGTERM)]


def test_reaper_waits_for_the_absolute_deadline():
    fake = _FakeProcesses({4242: "Sat Oct  3 12:00:00 2026"}, exits_on=signal.SIGTERM)
    _reap(fake)
    assert fake.t >= 130.0 and fake.sent[0][1] == signal.SIGTERM


def test_idle_drain_turn_kills_its_exact_child_at_the_turn_deadline(home, tmp_path, monkeypatch):
    """``fleet_message_drain._idle_cli_turn`` bounds its wait with ``subprocess.run(timeout=)``, which kills
    and reaps that one child before re-raising; the drain's caller records the error and releases its lock."""
    from tools import fleet_message_drain

    launcher, marker = _launcher(tmp_path, report=False, ignore_term=False)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: str(launcher))
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            fleet_message_drain._idle_cli_turn(home / "profiles" / "ops", "hello", None)
        assert _wait_gone(int(Path(marker + ".pid").read_text()), timeout=5)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()
