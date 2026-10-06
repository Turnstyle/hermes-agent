"""G3d: pause and board-lock failures must prevent claims and worker handoff."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    monkeypatch.setattr(dispatch, "_live_worker_procs", {})
    with kbc.connect() as conn:
        yield root, profile, conn


@pytest.mark.parametrize("marker", ["ESTOP", ".drain_request.json", "profiles/worker/ESTOP"])
@pytest.mark.parametrize("entry", ["dispatch", "claim", "review"])
def test_paused_entry_does_not_claim_or_spawn(board, marker, entry):
    root, _, conn = board
    tid = kb.create_task(conn, title="paused", assignee="worker")
    if entry == "review":
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    before = conn.total_changes
    (root / marker).write_text("null")
    if entry == "dispatch":
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("worker started"))
        assert result.spawned == []
    else:
        claim = kb.claim_task if entry == "claim" else kb.claim_review_task
        assert claim(conn, tid) is None
    assert conn.total_changes == before
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["path", "open"])
def test_dispatch_errors_do_not_enter_tick(board, monkeypatch, caplog, failure):
    _, _, conn = board
    calls = []
    monkeypatch.setattr(dispatch, "_dispatch_once_locked", lambda *a, **k: calls.append(True) or dispatch.DispatchResult())
    if failure == "path":
        monkeypatch.setattr(kb, "kanban_db_path", lambda **k: (_ for _ in ()).throw(ValueError("bad board")))
    else:
        original = Path.open
        def fail_lock(path, *args, **kwargs):
            if path.name.endswith(".dispatch.lock"):
                raise PermissionError("lock denied")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", fail_lock)
    result = dispatch.dispatch_once(conn)
    assert calls == []
    assert result.skipped_locked
    assert caplog.records


@pytest.mark.parametrize("entry", ["claim", "review"])
def test_claim_uses_connection_board_lock(board, entry):
    root, _, _ = board
    with kbc.connect(db_path=root / "private.db") as conn:
        tid = kb.create_task(conn, title="private", assignee="worker")
        if entry == "review":
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        claim = kb.claim_task if entry == "claim" else kb.claim_review_task
        with kbc._dispatch_tick_lock(root / "private.db") as held:
            assert held
            assert claim(conn, tid) is None
        assert claim(conn, tid).status == "running"


def test_dispatch_holds_lock_through_pid_record(board, monkeypatch):
    root, _, conn = board
    tid = kb.create_task(conn, title="handoff", assignee="worker")
    db = kb.kanban_db_path()
    original = dispatch._set_worker_pid
    def record(conn, task_id, pid):
        with kbc._dispatch_tick_lock(db) as held:
            assert not held
        original(conn, task_id, pid)
    monkeypatch.setattr(dispatch, "_set_worker_pid", record)
    def spawn(task, workspace):
        with kbc._dispatch_tick_lock(db) as held:
            assert not held
        return 99999999
    result = dispatch.dispatch_once(conn, spawn_fn=spawn)
    assert [row[0] for row in result.spawned] == [tid], repr(result)
    assert kb.get_task(conn, tid).worker_pid == 99999999


def test_pause_between_claim_and_spawn_defers_without_spending_retry(board, monkeypatch):
    from hermes_cli import kanban_db_workspace as workspace
    root, _, conn = board
    tid = kb.create_task(conn, title="late pause", assignee="worker")
    original = workspace.resolve_workspace
    def pause_after_workspace(*args, **kwargs):
        path = original(*args, **kwargs)
        (root / ".drain_request.json").write_text("null")
        return path
    monkeypatch.setattr(workspace, "resolve_workspace", pause_after_workspace)
    result = dispatch.dispatch_once(conn, spawn_fn=lambda *a: pytest.fail("late worker"))
    assert result.spawned == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.consecutive_failures == 0


@pytest.mark.parametrize("failure", ["dangling", "stat"])
def test_unreadable_or_dangling_pause_refuses_claim(board, monkeypatch, failure):
    root, _, conn = board
    tid = kb.create_task(conn, title="unreadable", assignee="worker")
    if failure == "dangling":
        (root / "ESTOP").symlink_to(root / "missing")
    else:
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path.name == "ESTOP":
                raise PermissionError("unreadable pause")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", denied)
    assert kb.claim_task(conn, tid) is None


def test_profile_scope_switch_checks_each_root(board, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    root, profile, conn = board
    other = tmp_path / "other" / "profiles" / "worker"
    other.mkdir(parents=True)
    (root / "ESTOP").write_text("null")
    for home, allowed in ((profile, False), (other, True), (profile, False)):
        tid = kb.create_task(conn, title="scope", assignee="worker")
        token = set_hermes_home_override(home)
        try:
            assert (kb.claim_task(conn, tid) is not None) is allowed
        finally:
            reset_hermes_home_override(token)


@pytest.mark.platforms("posix")
def test_profile_alias_still_honors_its_root_pause(board, tmp_path, monkeypatch):
    root, profile, conn = board
    alias = tmp_path / "profile-alias"
    alias.symlink_to(profile, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(alias))
    tid = kb.create_task(conn, title="alias", assignee="worker")
    (root / "ESTOP").write_text("null")
    assert kb.claim_task(conn, tid) is None


def test_pause_just_before_popen_closes_log_without_starting_worker(board, monkeypatch):
    root, _, conn = board
    tid = kb.create_task(conn, title="last handoff check", assignee="worker")
    task = kb.claim_task(conn, tid)
    logs = []
    def open_log(*args):
        handle = (root / "worker.log").open("a")
        logs.append(handle)
        (root / ".drain_request.json").write_text("null")
        return handle
    calls = []
    monkeypatch.setattr(dispatch, "_open_worker_log", open_log)
    monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **k: calls.append(True) or SimpleNamespace(pid=99999999, returncode=None))
    with pytest.raises(RuntimeError):
        dispatch._default_spawn(task, str(root))
    assert calls == []
    assert logs and logs[0].closed


@pytest.mark.parametrize("default_spawn,write_fails", [(False, True), (True, True), (False, False), (True, False), (False, "all_writes"), (False, "no_pid")])
def test_handoff_survives_claim_expiry_and_dispatcher_restart(board, monkeypatch, default_spawn, write_fails):
    root, _, conn = board
    tid = kb.create_task(conn, title="uncertain handoff", assignee="worker")
    calls = []
    proc = SimpleNamespace(pid=99999999, returncode=None)
    def spawn(*args, **kwargs):
        calls.append(tid)
        if write_fails == "all_writes":
            conn.execute("PRAGMA query_only=ON")
        if write_fails == "no_pid":
            return None
        return proc if default_spawn else proc.pid
    if default_spawn:
        monkeypatch.setattr(dispatch.subprocess, "Popen", spawn)
    spawn_fn = None if default_spawn else spawn
    original = dispatch._set_worker_pid
    def failed_write(*args):
        raise OSError("injected persistence failure")
    if write_fails is True:
        monkeypatch.setattr(dispatch, "_set_worker_pid", failed_write)
    try:
        result = dispatch.dispatch_once(conn, spawn_fn=spawn_fn)
    finally:
        conn.execute("PRAGMA query_only=OFF")
    assert bool(result.spawned) == (not write_fails)
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.claim_lock
    assert task.worker_pid == (None if write_fails else proc.pid)
    assert task.consecutive_failures == 0
    assert conn.execute("SELECT status FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0] == "running"
    registered = dispatch._live_worker_procs.get(proc.pid)
    conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
    monkeypatch.setattr(dispatch, "_set_worker_pid", original)
    monkeypatch.setattr(dispatch, "_live_worker_procs", {})
    probes = []
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: probes.append(a) or True)
    monkeypatch.setattr(kb, "_worker_alive", lambda *a: probes.append(a) or True)
    with kbc.connect(db_path=kb.kanban_db_path()) as reopened:
        for _ in range(2):
            later = dispatch.dispatch_once(reopened, spawn_fn=spawn_fn)
            assert later.spawned == []
        after = kb.get_task(reopened, tid)
        assert after.status == "running"
        assert after.current_run_id == task.current_run_id
        assert after.claim_lock == task.claim_lock
        assert after.consecutive_failures == 0
        assert reopened.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 1
    assert calls == [tid]
    if default_spawn:
        assert registered is proc
    if write_fails:
        assert any("Decider" in reason for _, reason in later.reclaim_errors)
        assert probes == []
    else:
        assert probes


@pytest.mark.parametrize("entry", ["stale", "orphan", "claim", "review", "direct", "gc_then_reopen"])
def test_uncertain_handoff_holds_other_recovery_entries(board, monkeypatch, entry):
    root, _, conn = board
    tid = kb.create_task(conn, title="uncertain handoff", assignee="worker")
    calls = []
    monkeypatch.setattr(dispatch, "_set_worker_pid", lambda *a: (_ for _ in ()).throw(OSError("write refused")))
    dispatch.dispatch_once(conn, spawn_fn=lambda *a: calls.append(tid) or 99999999)
    task = kb.get_task(conn, tid)
    errors = []
    if entry == "stale":
        conn.execute("UPDATE tasks SET started_at=1, last_heartbeat_at=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET started_at=1 WHERE id=?", (task.current_run_id,))
        assert dispatch.detect_stale_running(conn, stale_timeout_seconds=1, errors_out=errors) == []
    elif entry == "orphan":
        conn.execute("UPDATE tasks SET claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
        assert dispatch.reconcile_orphaned_running(conn, errors_out=errors) == []
    elif entry == "gc_then_reopen":
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=1 WHERE task_id=?", (tid,))
        kb.gc_events(conn, older_than_seconds=0)
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid) is None
    elif entry in {"claim", "review"}:
        conn.execute("UPDATE tasks SET status=?, claim_lock=NULL WHERE id=?", ("review" if entry == "review" else "ready", tid))
        claim = kb.claim_review_task if entry == "review" else kb.claim_task
        assert claim(conn, tid) is None
    else:
        monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **k: calls.append(tid) or SimpleNamespace(pid=99999998, returncode=None))
        with pytest.raises(RuntimeError, match="handoff"):
            dispatch._default_spawn(task, str(root))
    if entry in {"stale", "orphan"}:
        assert any("Decider" in reason for _, reason in errors)
    assert calls == [tid]
    assert kb.get_task(conn, tid).current_run_id == task.current_run_id


@pytest.mark.parametrize("failure", ["intent_write", "popen", "interrupt"])
def test_default_spawn_retains_only_uncertain_attempts(board, monkeypatch, failure):
    root, _, conn = board
    tid = kb.create_task(conn, title="refused before spawn", assignee="worker")
    calls = []
    original = kb._append_event
    def append(conn, task_id, kind, *args, **kwargs):
        if failure == "intent_write" and kind == "worker_handoff_pending":
            raise OSError("intent write refused")
        return original(conn, task_id, kind, *args, **kwargs)
    def popen(*args, **kwargs):
        calls.append(tid)
        if failure == "interrupt":
            raise KeyboardInterrupt("interrupted during spawn")
        raise OSError("spawn refused")
    monkeypatch.setattr(kb, "_append_event", append)
    monkeypatch.setattr(dispatch.subprocess, "Popen", popen)
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            dispatch.dispatch_once(conn)
        conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
        later = dispatch.dispatch_once(conn, spawn_fn=lambda *a: calls.append(tid) or 99999999)
        assert later.spawned == []
        assert calls == [tid]
        assert kb.get_task(conn, tid).status == "running"
        return
    result = dispatch.dispatch_once(conn)
    assert result.spawned == []
    assert result.spawn_errors
    assert calls == ([] if failure == "intent_write" else [tid])
    assert kb.get_task(conn, tid).status == "ready"
    monkeypatch.setattr(kb, "_append_event", original)
    retry = dispatch.dispatch_once(conn, spawn_fn=lambda *a: 99999999)
    assert [row[0] for row in retry.spawned] == [tid]


@pytest.mark.parametrize("state", ["paused", "unclaimed", "claimed", "fenced", "dispatch"])
def test_direct_spawn_requires_admission_and_tracks_pid(board, monkeypatch, state):
    root, _, conn = board
    tid = kb.create_task(conn, title="direct", assignee="worker")
    task = kb.claim_task(conn, tid) if state not in {"unclaimed", "dispatch"} else kb.get_task(conn, tid)
    if state == "paused":
        (root / "ESTOP").write_text("null")
    if state == "fenced":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))
    calls = []
    def spawn(*args, **kwargs):
        with kbc._dispatch_tick_lock(kb.kanban_db_path()) as held:
            assert not held
        calls.append(True)
        return SimpleNamespace(pid=99999999, returncode=None)
    monkeypatch.setattr(dispatch.subprocess, "Popen", spawn)
    if state in {"claimed", "dispatch"}:
        if state == "claimed":
            assert dispatch._default_spawn(task, str(root)) == 99999999
        else:
            result = dispatch.dispatch_once(conn)
            assert [row[0] for row in result.spawned] == [tid], repr(result)
        assert kb.get_task(conn, tid).worker_pid == 99999999
        assert calls == [True]
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='spawned'", (tid,)).fetchone()[0] == 1
    else:
        with pytest.raises((RuntimeError, PermissionError)):
            dispatch._default_spawn(task, str(root))
        assert calls == []
