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
