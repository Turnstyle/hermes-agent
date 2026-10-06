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


def test_pid_write_failure_keeps_the_running_claim_visible(board, monkeypatch):
    _, _, conn = board
    tid = kb.create_task(conn, title="uncertain handoff", assignee="worker")
    def failed_write(*args):
        raise OSError("injected persistence failure")
    monkeypatch.setattr(dispatch, "_set_worker_pid", failed_write)
    result = dispatch.dispatch_once(conn, spawn_fn=lambda *args: 99999999)
    assert result.spawned == []
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.claim_lock
    assert task.worker_pid is None
    assert task.consecutive_failures == 0
    assert conn.execute("SELECT status FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0] == "running"


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
