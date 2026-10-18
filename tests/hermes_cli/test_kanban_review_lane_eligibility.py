"""Per-profile review opt-out must not claim work or tax the ready lane."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles


@pytest.fixture
def review_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    kb.init_db()
    return home


def _configure(home, name="reviewer", text="kanban:\n  review_lane: false\n"):
    target = home if name == "default" else home / "profiles" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.yaml").write_text(text, encoding="utf-8")


def _review(conn, assignee="reviewer"):
    tid = kb.create_task(conn, title="review work", assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    return tid


def _assert_unclaimed(conn, tid):
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "review"
    assert task.claim_lock is None
    assert task.worker_pid is None
    assert task.current_run_id is None
    assert task.consecutive_failures == 0
    assert kb.list_runs(conn, tid) == []


def _skips(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "review_lane_skipped"]


def test_opted_out_review_is_never_claimed_or_spawned(review_home, monkeypatch):
    _configure(review_home)

    def forbidden(*args, **kwargs):
        pytest.fail("opted-out review reached a claim or spawn")

    monkeypatch.setattr(kb, "claim_review_task", forbidden)
    with kbc.connect() as conn:
        tid = _review(conn)
        result = kbd.dispatch_once(conn, spawn_fn=forbidden)
        assert result.spawned == []
        assert (tid, "review_lane_disabled") in result.respawn_guarded
        _assert_unclaimed(conn, tid)
        events = _skips(conn, tid)
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["assignee"] == "reviewer"
        assert events[0].payload["reason"] == "review_lane_disabled"
        assert "kanban.review_lane" in events[0].payload["message"]


def test_skip_event_is_durable_and_rate_limited(review_home, monkeypatch):
    import time

    _configure(review_home)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = _review(conn)
        kbd.dispatch_once(conn)
        kb.add_comment(conn, tid, author="operator", body="still waiting")
        now += 299
        kbd.dispatch_once(conn)
        assert len(_skips(conn, tid)) == 1
    # Reopening the board must not reset the throttle.
    with kbc.connect() as conn:
        now += 1
        kbd.dispatch_once(conn)
        assert len(_skips(conn, tid)) == 2
        _assert_unclaimed(conn, tid)


def test_dry_run_reports_skip_without_writes(review_home):
    _configure(review_home)
    with kbc.connect() as conn:
        tid = _review(conn)
        before = kb.list_events(conn, tid)
        result = kbd.dispatch_once(conn, dry_run=True)
        assert result.spawned == []
        assert (tid, "review_lane_disabled") in result.respawn_guarded
        assert kb.list_events(conn, tid) == before
        _assert_unclaimed(conn, tid)


@pytest.mark.parametrize("assignee", ["default", "reviewer"])
@pytest.mark.parametrize("config", [
    None, "kanban:\n  review_lane: true\n", 'kanban:\n  review_lane: "false"\n',
    "kanban:\n  review_lane: null\n", "kanban:\n  review_lane: 0\n",
])
def test_default_eligibility_still_claims_review(review_home, assignee, config):
    if config is not None:
        _configure(review_home, assignee, config)
    seen = []

    def spawn(task, workspace, board=None):
        seen.append((task.id, task.status, task.claim_lock))
        return None

    with kbc.connect() as conn:
        tid = _review(conn, assignee)
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert [row[0] for row in result.spawned] == [tid]
        assert seen[0][:2] == (tid, "running")
        assert seen[0][2] is not None
        assert len(kb.list_runs(conn, tid)) == 1
        claimed = [e for e in kb.list_events(conn, tid) if e.kind == "claimed"]
        assert claimed[-1].payload is not None
        assert claimed[-1].payload["source_status"] == "review"
        assert _skips(conn, tid) == []


def test_ready_lane_ignores_review_opt_out_and_keeps_only_slot(review_home):
    _configure(review_home)
    with kbc.connect() as conn:
        review_id = _review(conn)
        ready_id = kb.create_task(conn, title="ready work", assignee="reviewer")
        assert kbd.has_spawnable_ready(conn) is True
        assert kbd.has_spawnable_review(conn) is False
        result = kbd.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None, max_in_progress=1)
        assert [row[0] for row in result.spawned] == [ready_id]
        _assert_unclaimed(conn, review_id)


def test_assignee_config_not_dispatcher_config_controls_review(review_home):
    _configure(review_home, "default")
    _configure(review_home, "reviewer", "kanban:\n  review_lane: true\n")
    with kbc.connect() as conn:
        disabled = _review(conn, "default")
        eligible = _review(conn, "reviewer")
        result = kbd.dispatch_once(conn, dry_run=True)
        assert [row[0] for row in result.spawned] == [eligible]
        assert (disabled, "review_lane_disabled") in result.respawn_guarded


def test_unavailable_profile_metadata_does_not_bypass_review_opt_out(review_home, monkeypatch):
    _configure(review_home)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    with kbc.connect() as conn:
        _review(conn)
        assert kbd.has_spawnable_review(conn) is False
        assert kbd.dispatch_once(conn, dry_run=True).spawned == []


def test_unreadable_config_blocks_review_with_sanitized_diagnostic(review_home):
    _configure(review_home, text="kanban: [not valid: yaml")
    with kbc.connect() as conn:
        tid = _review(conn)
        result = kbd.dispatch_once(conn)
        assert (tid, "review_lane_config_error") in result.respawn_guarded
        _assert_unclaimed(conn, tid)
        event = _skips(conn, tid)[0]
        assert event.payload is not None
        assert event.payload["reason"] == "review_lane_config_error"
        assert "not valid" not in str(event.payload)


def test_changing_config_releases_review_on_next_tick(review_home):
    _configure(review_home)
    with kbc.connect() as conn:
        tid = _review(conn)
        assert kbd.dispatch_once(conn).spawned == []
        _configure(review_home, text="kanban:\n  review_lane: true\n")
        result = kbd.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert [row[0] for row in result.spawned] == [tid]


def test_skip_throttle_is_scoped_to_assignee_and_reason(review_home):
    _configure(review_home)
    _configure(review_home, "other-reviewer")
    with kbc.connect() as conn:
        tid = _review(conn)
        kbd.dispatch_once(conn)
        assert kb.assign_task(conn, tid, "other-reviewer")
        kbd.dispatch_once(conn)
        assert [e.payload and e.payload.get("assignee") for e in _skips(conn, tid)] == [
            "reviewer", "other-reviewer",
        ]
        _configure(review_home, "other-reviewer", "kanban: [broken")
        kbd.dispatch_once(conn)
        assert len(_skips(conn, tid)) == 3
        kbd.dispatch_once(conn)
        assert len(_skips(conn, tid)) == 3
        _assert_unclaimed(conn, tid)
