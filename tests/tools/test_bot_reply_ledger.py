"""message_agent reply waiters survive a restart visibly: durable ledger, startup sweep,
restart notice, and the next-turn note (tools/bot_reply_ledger.py).

The waiter tests drive the real ``bot_mode_dm.py --wait-reply`` entrypoint as a subprocess,
so the ledger must be written and removed by the process itself on every exit path.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
WAITER = REPO / "tools" / "bot_mode_dm.py"
MARKER = "bot_mode_dm.py --wait-reply"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process semantics")


@pytest.fixture
def root():
    home = Path(os.environ["HERMES_HOME"])
    (home / "bot_relay" / "replies").mkdir(parents=True, exist_ok=True)
    return home


def _pending(root: Path) -> Path:
    return root / "bot_relay" / "pending"


def _reply_path(root: Path, delivery_id: str) -> Path:
    return root / "bot_relay" / "replies" / f"{delivery_id}.json"


def _waiter_env(session_id: str = "sess-a") -> dict:
    return {**os.environ, "HERMES_SESSION_ID": session_id, "HERMES_SESSION_KEY": "key-a",
            "PYTHONPATH": str(REPO)}


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _spawn_waiter(root: Path, delivery_id: str, budget: str = "30", session_id: str = "sess-a"):
    """Launched like the process registry launches it: its own process session, this test as spawner."""
    return subprocess.Popen(
        [sys.executable, str(WAITER), "--wait-reply", str(_reply_path(root, delivery_id)), "@researcher", budget],
        env=_waiter_env(session_id), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)


def _spawn_orphan_waiter(root: Path, delivery_id: str, budget: str = "60", session_id: str = "sess-a") -> int:
    """A waiter whose launcher has already exited (reparented, PPID 1) — the restart case."""
    launcher = (
        "import subprocess, sys\n"
        "p = subprocess.Popen(sys.argv[1:], start_new_session=True,\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n")
    out = subprocess.run(
        [sys.executable, "-c", launcher, sys.executable, str(WAITER), "--wait-reply",
         str(_reply_path(root, delivery_id)), "@researcher", budget],
        env=_waiter_env(session_id), capture_output=True, text=True, check=True)
    pid = int(out.stdout.strip())
    assert _wait_for(lambda: (_pending(root) / f"{delivery_id}.json").exists()), "waiter never wrote its ledger entry"
    return pid


def _alive(pid: int) -> bool:
    try:
        import psutil

        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _entry(root: Path, delivery_id: str) -> dict:
    return json.loads((_pending(root) / f"{delivery_id}.json").read_text(encoding="utf-8"))


def _write_entry(root: Path, delivery_id: str, **fields) -> Path:
    _pending(root).mkdir(parents=True, exist_ok=True)
    path = _pending(root) / f"{delivery_id}.json"
    entry = {"delivery_id": delivery_id, "reply_file": str(_reply_path(root, delivery_id)),
             "target": "@researcher", "profile": "default", "parent_session_id": "sess-a",
             "session_key": "", "pid": 999999, "pid_start_time": 1, "spawned_at": time.time(),
             "timeout_s": 60.0}
    entry.update(fields)
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# 1. Ledger lifecycle — created at spawn, removed on reply / timeout / SIGTERM
# ---------------------------------------------------------------------------


class TestLedgerLifecycle:
    def test_entry_created_at_spawn_with_routing_fields(self, root):
        proc = _spawn_waiter(root, "a" * 32)
        try:
            assert _wait_for(lambda: (_pending(root) / f"{'a' * 32}.json").exists())
            entry = _entry(root, "a" * 32)
            assert entry["delivery_id"] == "a" * 32
            assert entry["reply_file"] == str(_reply_path(root, "a" * 32))
            assert entry["target"] == "@researcher"
            assert entry["parent_session_id"] == "sess-a" and entry["session_key"] == "key-a"
            assert entry["pid"] == proc.pid and entry["pid_start_time"] is not None
            assert entry["timeout_s"] == 30.0 and entry["spawned_at"] <= time.time()
            assert entry["owner_pid"] == os.getpid() and entry["owner_start_time"] is not None
            assert "profile" in entry
            # Atomic write: no temp files left behind.
            assert [p.name for p in _pending(root).iterdir() if p.name.endswith(".tmp")] == []
        finally:
            proc.kill()
            proc.wait()

    def test_entry_removed_when_reply_lands(self, root):
        proc = _spawn_waiter(root, "b" * 32)
        assert _wait_for(lambda: (_pending(root) / f"{'b' * 32}.json").exists())
        _reply_path(root, "b" * 32).write_text(json.dumps({"reply": "done"}), encoding="utf-8")
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0 and "done" in out
        assert not (_pending(root) / f"{'b' * 32}.json").exists()

    def test_entry_removed_on_timeout(self, root):
        proc = _spawn_waiter(root, "c" * 32, budget="1")
        assert _wait_for(lambda: (_pending(root) / f"{'c' * 32}.json").exists())
        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 1 and "No reply" in out
        assert not (_pending(root) / f"{'c' * 32}.json").exists()

    def test_entry_removed_on_sigterm(self, root):
        proc = _spawn_waiter(root, "d" * 32)
        assert _wait_for(lambda: (_pending(root) / f"{'d' * 32}.json").exists())
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        assert not (_pending(root) / f"{'d' * 32}.json").exists()


# ---------------------------------------------------------------------------
# 2. Startup sweep — adopt, kill over-TTL, PID-reuse guard, stale entries
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    from tools.process_registry import ProcessRegistry

    reg = ProcessRegistry()
    yield reg
    with reg._lock:
        reg._running.clear()


class TestStartupSweep:
    @pytest.mark.live_system_guard_bypass  # the orphan is reparented to init: signalling it is the point
    def test_adopts_live_orphan_within_budget(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        pid = _spawn_orphan_waiter(root, "e" * 32)
        try:
            summary = sweep_pending_waiters(root, registry=registry, grace=0)
            assert summary["adopted"] == 1 and summary["killed"] == 0
            assert _alive(pid)
            tracked = [s for s in registry._running.values() if s.pid == pid]
            assert len(tracked) == 1 and tracked[0].detached and tracked[0].parent_session_id == "sess-a"
            assert (_pending(root) / f"{'e' * 32}.json").exists()
            # A second sweep (another gateway/serve, or a re-run) never double-adopts or re-notifies.
            again = sweep_pending_waiters(root, registry=registry, grace=0)
            assert again["adopted"] == 0 and again["notices"] == 0
            assert len([s for s in registry._running.values() if s.pid == pid]) == 1
        finally:
            _kill(pid)

    @pytest.mark.live_system_guard_bypass  # the orphan is reparented to init: signalling it is the point
    def test_kills_orphan_past_budget_plus_grace(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        pid = _spawn_orphan_waiter(root, "f" * 32)
        try:
            entry = _entry(root, "f" * 32)
            entry["spawned_at"] = time.time() - 1000  # budget 60 + grace 300 long gone
            (_pending(root) / f"{'f' * 32}.json").write_text(json.dumps(entry), encoding="utf-8")
            summary = sweep_pending_waiters(root, registry=registry, grace=300)
            assert summary["killed"] == 1
            assert _wait_for(lambda: not _alive(pid))
            assert not (_pending(root) / f"{'f' * 32}.json").exists()
        finally:
            _kill(pid)

    @pytest.mark.live_system_guard_bypass  # the orphan is reparented to init: signalling it is the point
    def test_refuses_to_kill_pid_with_mismatched_start_time(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        pid = _spawn_orphan_waiter(root, "1" * 32)
        try:
            entry = _entry(root, "1" * 32)
            entry.update(spawned_at=time.time() - 1000, pid_start_time=int(entry["pid_start_time"]) - 100000)
            (_pending(root) / f"{'1' * 32}.json").write_text(json.dumps(entry), encoding="utf-8")
            summary = sweep_pending_waiters(root, registry=registry, grace=0)
            assert summary["killed"] == 0 and summary["adopted"] == 0
            assert _alive(pid), "a PID whose start time does not match must never be killed"
            assert not (_pending(root) / f"{'1' * 32}.json").exists()
        finally:
            _kill(pid)

    def test_refuses_to_kill_pid_whose_cmdline_is_not_a_waiter(self, root, registry):
        from gateway.status import get_process_start_time
        from tools.bot_reply_ledger import sweep_pending_waiters

        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            assert _wait_for(lambda: get_process_start_time(stranger.pid) is not None)
            _write_entry(root, "2" * 32, pid=stranger.pid, pid_start_time=get_process_start_time(stranger.pid),
                         spawned_at=time.time() - 1000)
            summary = sweep_pending_waiters(root, registry=registry, grace=0)
            assert summary["killed"] == 0
            assert stranger.poll() is None, "an unrelated process reusing the PID must survive"
            assert not (_pending(root) / f"{'2' * 32}.json").exists()
        finally:
            stranger.kill()
            stranger.wait()

    def test_deletes_entries_for_dead_pids_and_corrupt_files(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        _write_entry(root, "3" * 32, pid=_dead_pid())
        (_pending(root) / ("4" * 32 + ".json")).write_text("{not json", encoding="utf-8")
        summary = sweep_pending_waiters(root, registry=registry, grace=0)
        assert summary["removed"] == 2 and summary["killed"] == 0
        assert list(_pending(root).glob("*.json")) == []

    def test_sigkilled_waiter_leaves_an_entry_the_next_sweep_removes(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        proc = _spawn_waiter(root, "5a" * 16)
        assert _wait_for(lambda: (_pending(root) / f"{'5a' * 16}.json").exists())
        proc.kill()  # SIGKILL: no finally runs, the entry survives
        proc.wait()
        assert (_pending(root) / f"{'5a' * 16}.json").exists()
        summary = sweep_pending_waiters(root, registry=registry, grace=0)
        assert summary["removed"] == 1 and summary["killed"] == 0
        assert not (_pending(root) / f"{'5a' * 16}.json").exists()

    def test_leaves_waiter_with_live_spawner_alone(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        proc = _spawn_waiter(root, "5" * 32)  # this test process is its live launcher
        try:
            assert _wait_for(lambda: (_pending(root) / f"{'5' * 32}.json").exists())
            entry = _entry(root, "5" * 32)
            entry["spawned_at"] = time.time() - 1000
            (_pending(root) / f"{'5' * 32}.json").write_text(json.dumps(entry), encoding="utf-8")
            summary = sweep_pending_waiters(root, registry=registry, grace=0)
            assert summary["killed"] == 0 and summary["adopted"] == 0 and proc.poll() is None
        finally:
            proc.kill()
            proc.wait()

    def test_gateway_startup_runs_the_sweep_after_checkpoint_recovery(self, root, monkeypatch):
        import asyncio

        import gateway.run as gateway_run
        import tools.bot_reply_ledger as ledger
        from gateway.run_startup import GatewayStartupMixin
        from tools.process_registry import process_registry

        calls = []
        monkeypatch.setattr(process_registry, "recover_from_checkpoint", lambda: calls.append("recover") or 0)
        monkeypatch.setattr(ledger, "sweep_pending_waiters",
                            lambda **kw: calls.append(("sweep", kw.get("registry") is process_registry)))
        monkeypatch.setattr(gateway_run, "_hermes_home", root)
        (root / ".clean_shutdown").write_text("", encoding="utf-8")

        class _Runner(GatewayStartupMixin):
            hooks = SimpleNamespace(discover_and_load=lambda: None)
            _start_register_plugins_relay_hooks = staticmethod(lambda: None)
            _subscribe_plugin_rewire = lambda self, *a, **k: None
            _suspend_stuck_loop_sessions = lambda self: 0

            def _recover_secondary_process_checkpoints(self, process_registry) -> int:
                calls.append("secondary")
                return 0

            async def _consume_clean_shutdown_marker(self, marker_path) -> int:
                calls.append("clean-marker")
                return 0

        asyncio.run(_Runner()._start_recover_previous_run())
        assert calls == ["recover", "secondary", ("sweep", True), "clean-marker"]

    def test_gateway_startup_survives_a_failing_sweep(self, root, monkeypatch):
        import asyncio

        import gateway.run as gateway_run
        import tools.bot_reply_ledger as ledger
        from gateway.run_startup import GatewayStartupMixin
        from tools.process_registry import process_registry

        def _boom(**_kw):
            raise RuntimeError("sweep exploded")

        reached = []
        monkeypatch.setattr(process_registry, "recover_from_checkpoint", lambda: 0)
        monkeypatch.setattr(ledger, "sweep_pending_waiters", _boom)
        monkeypatch.setattr(gateway_run, "_hermes_home", root)
        (root / ".clean_shutdown").write_text("", encoding="utf-8")

        class _Runner(GatewayStartupMixin):
            hooks = SimpleNamespace(discover_and_load=lambda: None)
            _start_register_plugins_relay_hooks = staticmethod(lambda: None)
            _subscribe_plugin_rewire = lambda self, *a, **k: None
            _suspend_stuck_loop_sessions = lambda self: 0

            def _recover_secondary_process_checkpoints(self, process_registry) -> int:
                return 0

            async def _consume_clean_shutdown_marker(self, marker_path) -> int:
                reached.append("clean-marker")
                return 0

        asyncio.run(_Runner()._start_recover_previous_run())
        assert reached == ["clean-marker"], "a sweep failure must not stop the rest of startup recovery"

    def test_serve_startup_runs_the_sweep(self, monkeypatch):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        import hermes_cli.web_server as ws
        import tools.bot_reply_ledger as ledger
        from tools.process_registry import process_registry

        swept = threading.Event()
        seen = {}

        def _fake_sweep(**kw):
            seen.update(kw, thread=threading.current_thread().name)
            swept.set()

        monkeypatch.setattr(ws, "_warm_gateway_module", lambda: None)
        monkeypatch.setattr(ledger, "sweep_pending_waiters", _fake_sweep)
        with TestClient(ws.app):
            assert swept.wait(10), "serve lifespan never ran the reply-waiter sweep"
        assert seen["registry"] is process_registry
        assert seen["thread"] == "reply-waiter-sweep", "the sweep must run off the ready-probe path"

    def test_grace_config_key_defaults_to_300(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["bot_mode"]["waiter_orphan_grace_seconds"] == 300


class TestSweepHardening:
    def test_kill_revalidates_against_the_recorded_start_time(self, root, registry, monkeypatch):
        """The PID may change owners between the identity check and the signal: the kill must be
        re-validated against the ledger's start time, never a fresh reading of the (new) PID."""
        from unittest.mock import Mock

        import tools.bot_reply_ledger as ledger

        _write_entry(root, "race", pid=123456, pid_start_time=1000, spawned_at=time.time() - 1000)
        alive = iter([True, False])  # matches before the signal, gone after it
        monkeypatch.setattr(ledger, "_identity_matches", lambda entry: next(alive))
        monkeypatch.setattr(ledger, "_is_orphaned", lambda entry: True)
        monkeypatch.setattr(registry, "_safe_host_start_time", lambda pid: 900000)
        terminate = Mock()
        monkeypatch.setattr(registry, "_terminate_host_pid", terminate)
        summary = ledger.sweep_pending_waiters(root, registry=registry, grace=0)
        terminate.assert_called_once_with(123456, 1000)
        assert summary["killed"] == 1 and not (_pending(root) / "race.json").exists()

    def test_row_is_kept_when_the_waiter_survives_termination(self, root, registry, monkeypatch):
        import tools.bot_reply_ledger as ledger

        _write_entry(root, "stuck", pid=123456, pid_start_time=1000, spawned_at=time.time() - 1000)
        monkeypatch.setattr(ledger, "_identity_matches", lambda entry: True)
        monkeypatch.setattr(ledger, "_is_orphaned", lambda entry: True)
        monkeypatch.setattr(registry, "_terminate_host_pid", lambda pid, start: None)  # refused
        summary = ledger.sweep_pending_waiters(root, registry=registry, grace=0)
        assert summary["killed"] == 0 and summary["skipped"] == 1
        assert (_pending(root) / "stuck.json").exists()

    def test_adoption_tracks_the_recorded_start_time(self, root, registry, monkeypatch):
        import tools.bot_reply_ledger as ledger

        _write_entry(root, "adopt", pid=123456, pid_start_time=1000)
        monkeypatch.setattr(ledger, "_identity_matches", lambda entry: True)
        monkeypatch.setattr(ledger, "_is_orphaned", lambda entry: True)
        monkeypatch.setattr(registry, "_safe_host_start_time", lambda pid: 900000)
        monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
        assert ledger.sweep_pending_waiters(root, registry=registry, grace=0)["adopted"] == 1
        tracked = [s for s in registry._running.values() if s.pid == 123456]
        assert len(tracked) == 1 and tracked[0].host_start_time == 1000
        assert _entry(root, "adopt")["pid_start_time"] == 1000

    @pytest.mark.parametrize("fields", [
        {"adopted_by": "broken"},
        {"adopted_by": {"pid": "x", "start_time": None}},
        {"spawned_at": "not-a-number"},
        {"timeout_s": [60]},
        {"pid": "123"},
        {"pid_start_time": "soon"},
        {"target": 7, "parent_session_id": ["sess-a"], "profile": {"p": 1}},
    ], ids=["adopter-str", "adopter-junk", "spawned-str", "timeout-list", "pid-str", "start-str", "strings"])
    def test_malformed_row_does_not_abort_later_rows_or_notices(self, root, registry, session_db, fields):
        from tools.bot_reply_ledger import sweep_pending_waiters

        session_db.create_session("sess-a", source="cli")
        _write_entry(root, "a", **{"pid": _dead_pid(), **fields})
        good = _write_entry(root, "z", pid=_dead_pid(), target="@scribe")
        summary = sweep_pending_waiters(root, registry=registry, grace=0)
        assert not good.exists(), summary
        assert any("@scribe" in m["content"] for m in _notices(session_db, "sess-a"))

    def test_unexpected_row_error_is_isolated_to_that_row(self, root, registry, monkeypatch):
        import tools.bot_reply_ledger as ledger

        real = ledger._sweep_entry

        def _flaky(path, *args):
            if path.name == "a.json":
                raise RuntimeError("boom")
            return real(path, *args)

        monkeypatch.setattr(ledger, "_sweep_entry", _flaky)
        _write_entry(root, "a", pid=_dead_pid())
        good = _write_entry(root, "z", pid=_dead_pid())
        summary = ledger.sweep_pending_waiters(root, registry=registry, grace=0)
        assert summary["skipped"] == 1 and summary["removed"] == 1 and not good.exists()

    @pytest.mark.live_system_guard_bypass  # the orphan is reparented to init: signalling it is the point
    def test_malformed_spawn_time_on_a_live_orphan_adopts_it_never_kills(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        pid = _spawn_orphan_waiter(root, "st" * 16)
        try:
            entry = _entry(root, "st" * 16)
            entry["spawned_at"] = "not-a-number"
            (_pending(root) / f"{'st' * 16}.json").write_text(json.dumps(entry), encoding="utf-8")
            good = _write_entry(root, "z", pid=_dead_pid())
            summary = sweep_pending_waiters(root, registry=registry, grace=0)
            assert summary["killed"] == 0 and summary["adopted"] == 1 and _alive(pid)
            assert not good.exists()
        finally:
            _kill(pid)

    @pytest.mark.live_system_guard_bypass  # the orphan is reparented to init: signalling it is the point
    def test_adopted_waiter_killed_later_is_removed_by_the_next_sweep(self, root, registry):
        from tools.bot_reply_ledger import sweep_pending_waiters

        pid = _spawn_orphan_waiter(root, "ad" * 16)
        try:
            assert sweep_pending_waiters(root, registry=registry, grace=0)["adopted"] == 1
            _kill(pid)
            assert _wait_for(lambda: not _alive(pid))
            summary = sweep_pending_waiters(root, registry=registry, grace=0)
            assert summary["removed"] == 1 and summary["skipped"] == 0
            assert not (_pending(root) / f"{'ad' * 16}.json").exists()
        finally:
            _kill(pid)


# ---------------------------------------------------------------------------
# 3. Restart notice — one per owning session, through compaction lineage
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db(root):
    from hermes_state import SessionDB

    db = SessionDB(db_path=root / "state.db")
    yield db
    db.close()


def _notices(db, session_id: str) -> list:
    return [m for m in db.get_messages(session_id) if "pending when the app restarted" in str(m.get("content"))]


class TestRestartNotice:
    def test_one_notice_per_owning_session(self, root, registry, session_db):
        from tools.bot_reply_ledger import sweep_pending_waiters

        session_db.create_session("sess-a", source="cli")
        session_db.create_session("sess-b", source="cli")
        _write_entry(root, "6" * 32, pid=_dead_pid(), parent_session_id="sess-a", target="@researcher")
        _write_entry(root, "7" * 32, pid=_dead_pid(), parent_session_id="sess-a", target="@scribe")
        _write_entry(root, "8" * 32, pid=_dead_pid(), parent_session_id="sess-b", target="@scribe")
        summary = sweep_pending_waiters(root, registry=registry, grace=0)
        assert summary["notices"] == 2
        a, b = _notices(session_db, "sess-a"), _notices(session_db, "sess-b")
        assert len(a) == 1 and len(b) == 1
        assert "2 replies were pending when the app restarted: @researcher, @scribe; 0 still waiting, 2 stopped." in a[0]["content"]
        assert "1 replies were pending when the app restarted: @scribe; 0 still waiting, 1 stopped." in b[0]["content"]

    def test_notice_follows_compaction_to_the_newest_session(self, root, registry, session_db):
        from tools.bot_reply_ledger import sweep_pending_waiters

        session_db.create_session("sess-a", source="cli")
        session_db.end_session("sess-a", "compression")
        session_db.create_session("sess-a2", source="cli", parent_session_id="sess-a")
        session_db.end_session("sess-a2", "compression")
        session_db.create_session("sess-a3", source="cli", parent_session_id="sess-a2")
        _write_entry(root, "9" * 32, pid=_dead_pid(), parent_session_id="sess-a")
        sweep_pending_waiters(root, registry=registry, grace=0)
        assert _notices(session_db, "sess-a") == [] and _notices(session_db, "sess-a2") == []
        assert len(_notices(session_db, "sess-a3")) == 1

    def test_waiters_across_one_compaction_lineage_share_one_notice(self, root, registry, session_db):
        from tools.bot_reply_ledger import sweep_pending_waiters

        session_db.create_session("old", source="cli")
        session_db.end_session("old", "compression")
        session_db.create_session("middle", source="cli", parent_session_id="old")
        session_db.end_session("middle", "compression")
        session_db.create_session("new", source="cli", parent_session_id="middle")
        for i, sid in enumerate(["old", "middle", "new"]):
            _write_entry(root, f"l{i}", pid=_dead_pid(), parent_session_id=sid, target=f"@t{i}")
        summary = sweep_pending_waiters(root, registry=registry, grace=0)
        notices = _notices(session_db, "new")
        assert summary["notices"] == 1 and len(notices) == 1
        assert ("3 replies were pending when the app restarted: @t0, @t1, @t2; 0 still waiting, 3 stopped."
                in notices[0]["content"])

    def test_undeliverable_notice_goes_to_the_fallback_log(self, root, registry, session_db):
        from tools.bot_reply_ledger import sweep_pending_waiters

        _write_entry(root, "0" * 32, pid=_dead_pid(), parent_session_id="gone-session")
        summary = sweep_pending_waiters(root, registry=registry, grace=0)
        assert summary["notices"] == 0
        log = (_pending(root) / "_undeliverable.log").read_text(encoding="utf-8")
        assert "session=gone-session" in log and "1 replies were pending when the app restarted" in log


# ---------------------------------------------------------------------------
# 4. Next-turn note — after an interrupted wait and after compaction; N = 0 injects nothing
# ---------------------------------------------------------------------------


def _merged(agent) -> str:
    from agent.turn_context import _merge_gateway_notes

    return _merge_gateway_notes(agent, [{"role": "user", "content": "hi"}], 0, "")


def _agent(session_id: str, db=None):
    return SimpleNamespace(session_id=session_id, _session_db=db,
                           _gateway_turn_context_notes="", _surface_switch_note="")


def _interrupted_wait(session_id: str) -> dict:
    from tools.interrupt import set_interrupt
    from tools.process_registry import ProcessRegistry, ProcessSession

    reg = ProcessRegistry()
    sess = ProcessSession(id="proc_waitnote01", command=f"python {MARKER} x", pid=os.getpid(),
                          parent_session_id=session_id)
    reg._running[sess.id] = sess
    out = {}

    def _run():
        set_interrupt(True)
        try:
            out.update(reg.wait(sess.id, timeout=5))
        finally:
            set_interrupt(False)

    t = threading.Thread(target=_run)
    t.start()
    t.join(10)
    return out


class TestNextTurnNote:
    def test_note_injected_after_interrupted_wait(self, root):
        proc = _spawn_waiter(root, "ab" * 16, session_id="sess-int")
        try:
            assert _wait_for(lambda: (_pending(root) / f"{'ab' * 16}.json").exists())
            assert _interrupted_wait("sess-int").get("status") == "interrupted"
            assert _merged(_agent("sess-int")) == "1 background reply waiters still pending: @researcher"
            assert _merged(_agent("sess-int")) == "", "the note is one-shot"
        finally:
            proc.kill()
            proc.wait()

    def test_note_injected_after_compaction_on_the_rotated_session(self, root, session_db):
        from agent.conversation_compression import _finish_compaction_boundary

        session_db.create_session("sess-c", source="cli")
        session_db.end_session("sess-c", "compression")
        session_db.create_session("sess-c2", source="cli", parent_session_id="sess-c")
        proc = _spawn_waiter(root, "cd" * 16, session_id="sess-c")
        try:
            assert _wait_for(lambda: (_pending(root) / f"{'cd' * 16}.json").exists())
            agent = _agent("sess-c2", session_db)
            agent.context_compressor = SimpleNamespace(compression_count=1)
            agent.tools, agent.event_callback, agent._memory_manager = None, None, None
            agent.platform, agent.log_prefix = "", ""
            _finish_compaction_boundary(
                agent, [{"role": "user", "content": "summary"}], new_system_prompt="", old_session_id="sess-c",
                in_place=False, compacted_in_place=False, session_commit_succeeded=True,
                defer_context_engine_notification=True, compression_made_progress=False,
                compression_used_fallback=False, compression_feasibility_skip=False, task_id="t")
            assert _merged(agent) == "1 background reply waiters still pending: @researcher"
        finally:
            proc.kill()
            proc.wait()

    def test_nothing_injected_when_no_waiters_are_pending(self, root):
        # Armed by an interrupt, but N = 0: no text at all.
        assert _interrupted_wait("sess-empty").get("status") == "interrupted"
        assert _merged(_agent("sess-empty")) == ""
        # Another session's live waiter does not leak into this one.
        proc = _spawn_waiter(root, "ef" * 16, session_id="sess-other")
        try:
            assert _wait_for(lambda: (_pending(root) / f"{'ef' * 16}.json").exists())
            assert _interrupted_wait("sess-mine").get("status") == "interrupted"
            assert _merged(_agent("sess-mine")) == ""
        finally:
            proc.kill()
            proc.wait()

    def test_nothing_injected_without_an_interrupt_or_compaction(self, root):
        proc = _spawn_waiter(root, "fe" * 16, session_id="sess-quiet")
        try:
            assert _wait_for(lambda: (_pending(root) / f"{'fe' * 16}.json").exists())
            assert _merged(_agent("sess-quiet")) == ""
        finally:
            proc.kill()
            proc.wait()
