"""Missing-owner evidence survives capacity limits and reaches existing surfaces."""
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_dispatch_owners as owners
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "normal")
    kb.init_db()
    return home


def owner_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "owner_unavailable"]


@pytest.mark.parametrize("capacity", ["node_full", "board_full", "exhausted", "review_reserved"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_capacity_does_not_hide_missing_owner(home, monkeypatch, capacity, dry_run):
    spawned = []
    with kbc.connect() as conn:
        worker = kb.create_task(conn, title="first runnable", assignee="worker", priority=10)
        tid = kb.create_task(conn, title="missing owner", assignee="missing", priority=0)
        kwargs = {"max_spawn": 1}
        if capacity == "node_full":
            monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda _b: 12)
            kwargs = {"max_in_progress": 12}
        elif capacity == "board_full":
            kwargs = {"max_spawn": 0}
        elif capacity == "review_reserved":
            assert kb.request_review(conn, worker, summary="review", reviewer="worker")
        result = kbd.dispatch_once(
            conn, dry_run=dry_run, spawn_fn=lambda task, _w: spawned.append(task.id) or 4242,
            **kwargs,
        )
        assert result.owner_unavailable == [(tid, "missing")]
        assert result.skipped_nonspawnable == [tid]
        assert len(owner_events(conn, tid)) == (0 if dry_run else 1)
        assert kb.get_task(conn, tid).assignee == "missing"
        assert kb.get_task(conn, tid).status == "ready"
        expected = [] if dry_run or capacity.endswith("full") else [worker]
        assert spawned == expected


def test_owner_policy_reads_one_snapshot_per_tick(home, monkeypatch):
    from hermes_cli import config_effective
    (home / "config.yaml").write_text(
        "kanban:\n  dispatch_profiles: [worker, missing, lane]\n  control_plane_lanes: [lane]\n"
    )
    calls = []
    real_load = config_effective.load_user_config_effective

    def load(*args, **kwargs):
        calls.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(config_effective, "load_user_config_effective", load)
    with kbc.connect() as conn:
        tids = {name: kb.create_task(conn, title=name, assignee=name)
                for name in ("missing", "lane", "foreign")}
        for _ in range(2):
            calls.clear()
            result = kbd.dispatch_once(conn, dry_run=True)
            assert result.owner_unavailable == [(tids["missing"], "missing")]
            assert len(calls) == 1
        # No process-global cache: a new tick observes the owning home's changes.
        (home / "config.yaml").write_text("kanban:\n  dispatch_profiles: [worker]\n")
        assert kbd.dispatch_once(conn, dry_run=True).owner_unavailable == []


@pytest.mark.parametrize("race", ["status", "claim"])
def test_transaction_recheck_rejects_new_status_or_claim(home, monkeypatch, race):
    real_txn = kb.write_txn
    raced = False
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="missing owner", assignee="missing")

        @contextmanager
        def write_txn(db, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                # Land through the application API after the preflight read,
                # immediately before the recorder acquires its write lock.
                if race == "status":
                    assert kb.block_task(db, tid, reason="operator hold", kind="needs_input")
                else:
                    assert kb.claim_task(db, tid, claimer="new-owner") is not None
            with real_txn(db, **kwargs):
                yield db

        monkeypatch.setattr(kb, "write_txn", write_txn)
        assert owners._record_owner_unavailable(conn, tid, "missing") is False
        assert raced
        assert owner_events(conn, tid) == []
        assert kb.list_comments(conn, tid) == []


def test_first_event_has_atomic_deduped_comment(home, monkeypatch):
    import sqlite3

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="missing", assignee="missing")
        with monkeypatch.context() as scoped:
            def refuse_comment(*_args, **_kwargs):
                raise sqlite3.IntegrityError("test comment write refused")
            scoped.setattr(kb, "add_comment", refuse_comment)
            kbd.dispatch_once(conn)
        assert owner_events(conn, tid) == []  # event must roll back with comment
        assert kb.list_comments(conn, tid) == []
        for _ in range(3):
            kbd.dispatch_once(conn)
        events = owner_events(conn, tid)
        comments = kb.list_comments(conn, tid)
        assert len(events) == len(comments) == 1
        assert "owner_unavailable" in comments[0].body
        assert "missing" in comments[0].body
        assert events[0].payload["node"] in comments[0].body
        assert kb.reassign_task(conn, tid, "missing-again", reason="explicit recovery")
        for _ in range(2):
            kbd.dispatch_once(conn)
        assert len(owner_events(conn, tid)) == len(kb.list_comments(conn, tid)) == 2


def test_stranded_diagnostic_uses_only_current_owner_episode(home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="missing", assignee="missing")
        kbd.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        now = task.created_at + 2700
        diags = kd.compute_task_diagnostics(task, kb.list_events(conn, tid), [], now=now)
        stranded = next(d for d in diags if d.kind == "stranded_in_ready")
        assert stranded.data["owner_unavailable"]["assignee"] == "missing"
        assert "profile_not_found" in stranded.detail
        # Reassignment clears the episode even within the same timestamp.
        assert kb.reassign_task(conn, tid, "worker", reason="recover")
        task = kb.get_task(conn, tid)
        diags = kd.compute_task_diagnostics(task, kb.list_events(conn, tid), [], now=now)
        stranded = next(d for d in diags if d.kind == "stranded_in_ready")
        assert "owner_unavailable" not in stranded.data


def test_owner_policy_follows_served_home_a_b_a(tmp_path, monkeypatch):
    """Real loaders/profile lookup under multiplexing cannot retain another home's policy."""
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "alpha", root / "profiles" / "beta"
    for home in (a, b):
        home.mkdir(parents=True)
    (a / "config.yaml").write_text("kanban:\n  dispatch_profiles: [missing]\n")
    (b / "config.yaml").write_text("kanban:\n  dispatch_profiles: [beta]\n")
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    was_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        for home, expected in ((a, True), (b, False), (a, True)):
            home_token = set_hermes_home_override(home)
            scope_token = secret_scope.set_secret_scope({}, profile_home=str(home))
            try:
                kb.init_db()
                with kbc.connect() as conn:
                    tid = kb.create_task(conn, title="missing", assignee="missing")
                    result = kbd.dispatch_once(conn, max_spawn=0)
                    assert ((tid, "missing") in result.owner_unavailable) is expected
                    assert bool(owner_events(conn, tid)) is expected
                    assert bool(kb.list_comments(conn, tid)) is expected
                policy = owners.load_owner_policy()
                assert policy.can_spawn("beta") is (home == b)
            finally:
                secret_scope.reset_secret_scope(scope_token)
                reset_hermes_home_override(home_token)
    finally:
        secret_scope.set_multiplex_active(was_multiplex)
