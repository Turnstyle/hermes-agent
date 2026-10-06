"""Tests for placeholder profile dispatch suppression and separate non-paging telemetry.

Verifies that placeholder profiles (default, alpha, beta, orch) with no gateway
workers are excluded from spawnable telemetry, counted in count_placeholder_ready,
routed to res.skipped_placeholder without spurious owner_unavailable events, and
reported as separate non-paging counts.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home_with_profiles(tmp_path, monkeypatch):
    """Hermetic HERMES_HOME with a clean kanban DB and a real 'sage' profile."""
    home = tmp_path / ".hermes"
    home.mkdir()
    sage_dir = home / "profiles" / "sage"
    sage_dir.mkdir(parents=True)
    (sage_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    return 12345


def test_placeholder_predicate_helpers():
    """is_placeholder_profile and is_dispatch_enabled_profile helpers."""
    for name in ("default", "alpha", "beta", "orch", "DEFAULT", "Alpha", " Orch "):
        assert kbd.is_placeholder_profile(name) is True
    for name in ("sage", "tb-king", "tb-cndr", None, "", "   "):
        assert kbd.is_placeholder_profile(name) is False


def test_placeholder_profiles_not_spawnable_when_allowlist_unset(kanban_home_with_profiles):
    """When dispatch_profiles is unset, placeholder profiles must NOT be spawnable.

    They must report count_spawnable_ready == 0, has_spawnable_ready is False,
    and populate res.skipped_placeholder without owner_unavailable events.
    """
    with kbc.connect() as conn:
        t_def = kb.create_task(conn, title="default card", assignee="default")
        t_alp = kb.create_task(conn, title="alpha card", assignee="alpha")
        t_bet = kb.create_task(conn, title="beta card", assignee="beta")
        t_orc = kb.create_task(conn, title="orch card", assignee="orch")

        assert kbd.has_spawnable_ready(conn) is False
        assert kbd.count_spawnable_ready(conn) == 0
        assert kbd.count_placeholder_ready(conn) == 4

        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert res.spawned == []
    assert res.owner_unavailable == []
    skipped_map = dict(res.skipped_placeholder)
    assert skipped_map[t_def] == "default"
    assert skipped_map[t_alp] == "alpha"
    assert skipped_map[t_bet] == "beta"
    assert skipped_map[t_orc] == "orch"

    # Verify no owner_unavailable event was recorded in the database
    with kbc.connect() as conn:
        for tid in (t_def, t_alp, t_bet, t_orc):
            evs = [e.kind for e in kb.list_events(conn, tid)]
            assert "owner_unavailable" not in evs


def test_real_profiles_spawnable_when_allowlist_unset(kanban_home_with_profiles):
    """Real named profiles with local profile directories remain spawnable."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="sage card", assignee="sage")

        assert kbd.has_spawnable_ready(conn) is True
        assert kbd.count_spawnable_ready(conn) == 1
        assert kbd.count_placeholder_ready(conn) == 0

        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert [s[0] for s in res.spawned] == [tid]
    assert res.skipped_placeholder == []
    assert res.owner_unavailable == []


def test_mixed_queue_separates_spawnable_and_placeholder(kanban_home_with_profiles):
    """A queue with mixed assignees cleanly separates spawnable, placeholder, and unassigned."""
    with kbc.connect() as conn:
        t_real = kb.create_task(conn, title="sage card", assignee="sage")
        t_hold = kb.create_task(conn, title="default card", assignee="default")
        t_un = kb.create_task(conn, title="unassigned card", assignee=None)

        assert kbd.has_spawnable_ready(conn) is True
        assert kbd.count_spawnable_ready(conn) == 1
        assert kbd.count_placeholder_ready(conn) == 1

        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert [s[0] for s in res.spawned] == [t_real]
    assert res.skipped_placeholder == [(t_hold, "default")]
    assert res.skipped_unassigned == [t_un]
    assert res.owner_unavailable == []


def test_review_lane_placeholder_and_spawnable_counts(kanban_home_with_profiles):
    """Review lane honors placeholder suppression for count_spawnable_review and count_placeholder_review."""
    with kbc.connect() as conn:
        t_rev_ph = kb.create_task(conn, title="review default", assignee="default")
        t_rev_real = kb.create_task(conn, title="review sage", assignee="sage")
        # Move both to review
        conn.execute("UPDATE tasks SET status = 'review' WHERE id IN (?, ?)", (t_rev_ph, t_rev_real))
        conn.commit()

        assert kbd.count_placeholder_review(conn) == 1
        assert kbd.count_spawnable_review(conn) == 1
        assert kbd.has_spawnable_review(conn) is True


def test_explicit_opt_in_allows_placeholder_profile(kanban_home_with_profiles):
    """Explicitly adding 'default' to kanban.dispatch_profiles enables it."""
    (kanban_home_with_profiles / "config.yaml").write_text(
        "kanban:\n  dispatch_profiles:\n    - default\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="explicit default", assignee="default")

        assert kbd.has_spawnable_ready(conn) is True
        assert kbd.count_spawnable_ready(conn) == 1
        assert kbd.count_placeholder_ready(conn) == 0

        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert [s[0] for s in res.spawned] == [tid]
    assert res.skipped_placeholder == []


def test_dispatcher_ready_counts(kanban_home_with_profiles):
    """_KanbanDispatcher.ready_counts returns aggregated counts across boards."""
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher

    settings = _DispatcherSettings(
        interval=5.0, max_spawn=None, max_in_progress=None, failure_limit=3,
        stale_timeout_seconds=3600, reconcile_orphans=True, default_assignee=None,
        max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kb, settings)
    with kbc.connect() as conn:
        kb.create_task(conn, title="default card", assignee="default")
        kb.create_task(conn, title="alpha card", assignee="alpha")
        kb.create_task(conn, title="sage card", assignee="sage")

    counts = dispatcher.ready_counts()
    assert counts == {"spawnable": 1, "placeholder": 2}
    assert dispatcher.ready_nonempty() is True


def test_policy_held_card_not_spawnable_ready(kanban_home_with_profiles):
    """An isolated card held by active_pr guard must NOT be counted as spawnable ready work."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="guarded task", assignee="sage")
        kb.add_comment(conn, tid, author="sage", body="Opened https://github.com/example/repo/pull/123 for review.")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        assert kbd.count_spawnable_ready(conn) == 0
        assert kbd.has_spawnable_ready(conn) is False

        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
        assert res.spawned == []
        assert res.respawn_guarded == [(tid, "active_pr")]
        assert kbd.describe_suppression([res]) == "active_pr=1"


def test_watcher_paging_predicate_ignores_policy_and_capacity_holds(kanban_home_with_profiles):
    """Watcher paging predicate must not increment bad_ticks when work is held by guards or capacity."""
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher

    settings = _DispatcherSettings(
        interval=5.0, max_spawn=None, max_in_progress=None, failure_limit=3,
        stale_timeout_seconds=3600, reconcile_orphans=True, default_assignee=None,
        max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kb, settings)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="guarded task", assignee="sage")
        kb.add_comment(conn, tid, author="sage", body="Opened https://github.com/example/repo/pull/123 for review.")

    # Isolated active_pr card: ready_counts returns spawnable=0, ready_nonempty is False
    counts = dispatcher.ready_counts()
    assert counts["spawnable"] == 0
    assert dispatcher.ready_nonempty() is False

    # Simulate tick execution
    results = dispatcher.tick_once()
    assert len(results) == 1
    _slug, res = results[0]
    assert res.spawned == []
    assert res.respawn_guarded == [(tid, "active_pr")]

    held = kbd.describe_suppression([res])
    assert held == "active_pr=1"

    # Paging predicate check:
    can_dispatch = counts.get("spawnable", 0) > 0 and not held
    bad_ticks = 0
    bad_ticks = bad_ticks + 1 if can_dispatch and not res.spawned else 0
    assert bad_ticks == 0, "Policy-held card must not increment bad_ticks"


def test_log_spawn_results_emits_placeholder_diagnostics(caplog):
    """_log_spawn_results logs placeholder counts and emits non-paging diagnostics."""
    import logging
    from gateway.kanban_watchers_dispatcher import _LAST_PLACEHOLDER_LOG, _log_spawn_results
    from hermes_cli.kanban_db_dispatch import DispatchResult

    _LAST_PLACEHOLDER_LOG.clear()
    caplog.set_level(logging.INFO)

    # Case 1: zero spawned, placeholder skipped
    res1 = DispatchResult()
    res1.skipped_placeholder = [("t1", "default"), ("t2", "alpha")]
    any_spawned = _log_spawn_results([("test-board", res1)])
    assert any_spawned is False
    assert "placeholder-assigned task(s) skipped (non-paging, waiting for assignment)" in caplog.text
    assert "WARNING" not in caplog.text

    # Case 2: worker spawned with placeholder present
    caplog.clear()
    res2 = DispatchResult()
    res2.spawned = [("t3", "sage", "/tmp/ws")]
    res2.skipped_placeholder = [("t1", "default")]
    any_spawned = _log_spawn_results([("test-board", res2)])
    assert any_spawned is True
    assert "placeholder=1" in caplog.text
    assert "WARNING" not in caplog.text


@pytest.mark.asyncio
async def test_gateway_watcher_with_policy_held_and_placeholder_cards(kanban_home_with_profiles, monkeypatch, caplog):
    """Exercise GatewayKanbanWatchersMixin._kanban_dispatcher_watcher loop with guarded and placeholder cards."""
    import asyncio
    import logging
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    caplog.set_level(logging.INFO)

    with kbc.connect() as conn:
        # Create a placeholder card
        kb.create_task(conn, title="placeholder task", assignee="default")
        # Create an active_pr policy-held card
        tid = kb.create_task(conn, title="guarded task", assignee="sage")
        kb.add_comment(conn, tid, author="sage", body="Opened https://github.com/example/repo/pull/123 for review.")

    class TestRunner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self._kanban_dispatcher_lock_handle = None

        async def _sleep_between_ticks(self, interval: float) -> None:
            self._running = False

    runner = TestRunner()

    real_sleep = asyncio.sleep
    async def fake_sleep(delay):
        if delay == 5:  # initial delay in watcher
            return None
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await runner._kanban_dispatcher_watcher()

    # 1. Non-paging placeholder diagnostics emitted
    assert "placeholder" in caplog.text.lower()
    # 2. Non-paging dispatch held back emitted
    assert "dispatch held back: active_pr=1 (non-paging)" in caplog.text
    # 3. No stuck warning emitted
    assert "kanban dispatcher stuck" not in caplog.text


@pytest.mark.asyncio
async def test_gateway_watcher_capacity_held_does_not_page(kanban_home_with_profiles, monkeypatch, caplog):
    """Capacity-held tasks must emit non-paging diagnostics and not increment bad_ticks in the watcher."""
    import asyncio
    import logging
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    caplog.set_level(logging.INFO)

    # Configure kanban with max_in_progress = 1
    config_file = kanban_home_with_profiles / "config.yaml"
    config_file.write_text("kanban:\n  max_in_progress: 1\n", encoding="utf-8")

    with kbc.connect() as conn:
        # Create a running task to saturate the host cap (using current live PID so it is not reaped as dead)
        r_tid = kb.create_task(conn, title="running task", assignee="sage")
        conn.execute("UPDATE tasks SET status='running', claim_lock='test:1', worker_pid=? WHERE id=?", (os.getpid(), r_tid))
        # Create a ready task that cannot start due to capacity
        kb.create_task(conn, title="waiting task", assignee="sage")

    class TestRunner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self._kanban_dispatcher_lock_handle = None

        async def _sleep_between_ticks(self, interval: float) -> None:
            self._running = False

    runner = TestRunner()

    real_sleep = asyncio.sleep
    async def fake_sleep(delay):
        if delay == 5:
            return None
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await runner._kanban_dispatcher_watcher()

    # Verify capacity hold is logged non-paging and does not page
    assert "dispatch held back: host cap: 1 running of 1 (non-paging)" in caplog.text
    assert "kanban dispatcher stuck" not in caplog.text


def test_eligible_work_stalled_matrix():
    """Verify eligible_work_stalled correctly distinguishes holds from mixed blockages."""
    from hermes_cli.kanban_db_dispatch import DispatchResult, eligible_work_stalled

    # 1. Zero spawnable ready work -> False
    res = DispatchResult()
    res.respawn_guarded.append(("t1", "active_pr"))
    assert eligible_work_stalled([res], 0) is False

    # 2. Spawnable work, but worker spawned -> False
    res2 = DispatchResult()
    res2.spawned.append(("t2", "sage", "/tmp/ws"))
    assert eligible_work_stalled([res2], 1) is False

    # 3. Host capacity held -> False
    res3 = DispatchResult()
    res3.capacity_held = "host cap: 12 running of 12"
    assert eligible_work_stalled([res3], 3) is False

    # 4. Critical memory pressure -> False
    res4 = DispatchResult()
    res4.memory_pressure = "critical"
    assert eligible_work_stalled([res4], 2) is False

    # 5. Profile capped -> False
    res5 = DispatchResult()
    res5.skipped_per_profile_capped.append(("t5", "sage", 1))
    assert eligible_work_stalled([res5], 1) is False

    # 6. Profile busy -> False
    res6 = DispatchResult()
    res6.profile_busy.append("t6")
    assert eligible_work_stalled([res6], 1) is False

    # 7. Rate limited -> False
    res7 = DispatchResult()
    res7.rate_limited.append("t7")
    assert eligible_work_stalled([res7], 1) is False

    # 8. Mixed failure: held card + claim/lease error card -> True
    held_res = DispatchResult()
    held_res.respawn_guarded.append(("t_held", "active_pr"))
    fail_res = DispatchResult()
    fail_res.claim_errors.append(("t_fail", "claim: verified execution lease required before running"))
    assert eligible_work_stalled([held_res, fail_res], 1) is True
    # Also when on the same board
    mixed_res = DispatchResult()
    mixed_res.respawn_guarded.append(("t_held", "active_pr"))
    mixed_res.claim_errors.append(("t_fail", "claim: verified execution lease required before running"))
    assert eligible_work_stalled([mixed_res], 1) is True

    # 9. Mixed failure: held card + auto_blocked card -> True
    ab_res = DispatchResult()
    ab_res.respawn_guarded.append(("t_held", "active_pr"))
    ab_res.auto_blocked.append("t_fail")
    assert eligible_work_stalled([ab_res], 1) is True

    # 10. More spawnable cards than accounted for by holds -> True
    part_res = DispatchResult()
    part_res.skipped_per_profile_capped.append(("t_capped", "sage", 1))
    assert eligible_work_stalled([part_res], 2) is True

    # 11. Unrelated respawn_guarded hold does not mask unheld spawnable card -> True
    guarded_res = DispatchResult()
    guarded_res.respawn_guarded.append(("t_held", "active_pr"))
    assert eligible_work_stalled([guarded_res], 1) is True

    # 12. Successful spawn does not mask failing card (claim error) -> True
    spawn_fail_res = DispatchResult()
    spawn_fail_res.spawned.append(("t_ok", "sage", "/tmp"))
    spawn_fail_res.claim_errors.append(("t_fail", "claim: verified execution lease required"))
    assert eligible_work_stalled([spawn_fail_res], 1) is True

    # 13. Successful spawn does not mask auto-blocked failing card -> True
    spawn_ab_res = DispatchResult()
    spawn_ab_res.spawned.append(("t_ok", "sage", "/tmp"))
    spawn_ab_res.auto_blocked.append("t_fail")
    assert eligible_work_stalled([spawn_ab_res], 1) is True

    # 14. Host capacity hold on one board does not mask claim error on another -> True
    cap_b1 = ("board1", DispatchResult(capacity_held="host cap: 2 running of 2"))
    err_b2 = ("board2", DispatchResult(claim_errors=[("t_fail", "claim: lease refused")]))
    assert eligible_work_stalled([cap_b1, err_b2], 1) is True

    # 15. Auto-blocked card after ready row removed (count=0) -> True
    ab_empty_res = DispatchResult(auto_blocked=["t_fail"])
    assert eligible_work_stalled([ab_empty_res], 0) is True


@pytest.mark.asyncio
async def test_gateway_watcher_mixed_held_and_failing_card_pages(kanban_home_with_profiles, monkeypatch, caplog):
    """Mixed failure: one active_pr held card and one card failing lease must increment bad_ticks and page."""
    import asyncio
    import logging
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    caplog.set_level(logging.INFO)

    with kbc.connect() as conn:
        # 1. Held card (active_pr)
        tid_held = kb.create_task(conn, title="guarded task", assignee="sage")
        kb.add_comment(conn, tid_held, author="sage", body="Opened https://github.com/example/repo/pull/123 for review.")

        # 2. Eligible card that fails lease
        tid_fail = kb.create_task(conn, title="failing lease task", assignee="sage")

        # Install SQLite trigger simulating execution lease refusal
        conn.execute(f"""
            CREATE TRIGGER test_lease_fence BEFORE UPDATE OF status ON tasks
            WHEN NEW.id = '{tid_fail}' AND NEW.status = 'running'
            BEGIN
                SELECT RAISE(ABORT, 'verified execution lease required before running');
            END;
        """)

    class TestRunner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self._kanban_dispatcher_lock_handle = None
            self._ticks = 0

        async def _sleep_between_ticks(self, interval: float) -> None:
            self._ticks += 1
            if self._ticks >= 6:
                self._running = False

    runner = TestRunner()

    real_sleep = asyncio.sleep
    async def fake_sleep(delay):
        if delay == 5:
            return None
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await runner._kanban_dispatcher_watcher()

    # Verify that:
    # 1. Dispatch held back is logged for the guarded card
    assert "dispatch held back: active_pr=1" in caplog.text
    # 2. Dispatcher stuck warning IS emitted after 6 ticks (not masked by the active_pr hold!)
    assert "kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks but 0 workers spawned." in caplog.text
    assert "Last tick held back: active_pr=1." in caplog.text


@pytest.mark.asyncio
async def test_gateway_watcher_mixed_spawned_and_failing_card_pages(kanban_home_with_profiles, monkeypatch, caplog):
    """Mixed progress & failure: active worker spawns must not mask a persistently failing card."""
    import asyncio
    import logging
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli.kanban_db_dispatch import DispatchResult

    caplog.set_level(logging.INFO)

    res = DispatchResult()
    res.spawned.append(("t_spawned", "sage", "/tmp/ws"))
    res.claim_errors.append(("t_fail", "claim: verified execution lease required before running"))

    class FakeDispatcher:
        def auto_decompose_tick(self, per_tick):
            pass

        def tick_once(self):
            return [("default", res)]

        def ready_counts(self):
            return {"spawnable": 1, "placeholder": 0}

    class TestRunner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self._kanban_dispatcher_lock_handle = None
            self._ticks = 0

        async def _sleep_between_ticks(self, interval: float) -> None:
            self._ticks += 1
            if self._ticks >= 6:
                self._running = False

    monkeypatch.setattr("gateway.kanban_watchers._KanbanDispatcher", lambda _kb, _settings: FakeDispatcher())

    real_sleep = asyncio.sleep
    async def fake_sleep(delay):
        if delay == 5:
            return None
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    runner = TestRunner()
    await runner._kanban_dispatcher_watcher()

    # Verify that:
    # 1. Spawn is logged
    assert "spawned=1" in caplog.text
    # 2. Stuck warning IS emitted after 6 ticks with accurate text reflecting that workers spawned!
    assert "kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks but eligible tasks failing despite active spawns." in caplog.text


def test_cli_daemon_on_tick_mixed_held_and_failing_card(kanban_home_with_profiles, monkeypatch, capsys):
    """CLI daemon _cmd_daemon must increment bad_ticks and warn when eligible work fails alongside a hold."""
    import argparse
    from hermes_cli import kanban_ops
    from hermes_cli.kanban_db_dispatch import DispatchResult

    with kbc.connect() as conn:
        # Held card (active_pr)
        tid_held = kb.create_task(conn, title="guarded task", assignee="sage")
        kb.add_comment(conn, tid_held, author="sage", body="Opened https://github.com/example/repo/pull/123 for review.")
        # Eligible card that fails lease
        tid_fail = kb.create_task(conn, title="failing lease task", assignee="sage")

    res = DispatchResult()
    res.respawn_guarded.append((tid_held, "active_pr"))
    res.claim_errors.append((tid_fail, "claim: verified execution lease required before running"))

    def fake_run_daemon(interval, max_spawn, failure_limit, on_tick):
        for _ in range(6):
            on_tick(res)

    monkeypatch.setattr(kanban_ops.kbd, "run_daemon", fake_run_daemon)

    args = argparse.Namespace(interval=5, max=None, failure_limit=3, verbose=False, pidfile=None, force=True)
    ret = kanban_ops._cmd_daemon(args)
    assert ret == 0

    captured = capsys.readouterr()
    assert "WARN dispatcher stuck: ready queue non-empty for 6 consecutive ticks but 0 workers spawned successfully. Last tick held back: active_pr=1." in captured.err


def test_cli_daemon_on_tick_mixed_spawned_and_failing_card(kanban_home_with_profiles, monkeypatch, capsys):
    """CLI daemon _cmd_daemon must increment bad_ticks and warn when eligible work fails alongside active spawns."""
    import argparse
    from hermes_cli import kanban_ops
    from hermes_cli.kanban_db_dispatch import DispatchResult

    with kbc.connect() as conn:
        tid_spawn = kb.create_task(conn, title="spawned task", assignee="sage")
        tid_fail = kb.create_task(conn, title="failing lease task", assignee="sage")

    res = DispatchResult()
    res.spawned.append((tid_spawn, "sage", "/tmp/ws"))
    res.claim_errors.append((tid_fail, "claim: verified execution lease required before running"))

    def fake_run_daemon(interval, max_spawn, failure_limit, on_tick):
        for _ in range(6):
            on_tick(res)

    monkeypatch.setattr(kanban_ops.kbd, "run_daemon", fake_run_daemon)

    args = argparse.Namespace(interval=5, max=None, failure_limit=3, verbose=False, pidfile=None, force=True)
    ret = kanban_ops._cmd_daemon(args)
    assert ret == 0

    captured = capsys.readouterr()
    assert "WARN dispatcher stuck: ready queue non-empty for 6 consecutive ticks but eligible tasks failing despite active spawns." in captured.err



