"""Redtape respawn-guard behaviors (unchanged block, explicit markers, run error auth)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _append_blocked(conn, task_id: str, reason: str, *, created_at: int) -> None:
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            "blocked",
            {"reason": reason, "kind": "needs_input", "recurrences": 1, "source_status": "ready"},
        )
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND id = "
            "(SELECT id FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1)",
            (created_at, task_id, task_id),
        )


def _seed_ended_run(
    conn,
    task_id: str,
    *,
    outcome: str,
    error: str | None = None,
    ended_at: int | None = None,
) -> None:
    ended = ended_at if ended_at is not None else int(time.time())
    kb.claim_task(conn, task_id)
    run_id = kb.get_task(conn, task_id).current_run_id
    assert run_id is not None
    conn.execute(
        "UPDATE task_runs SET outcome = ?, status = ?, error = ?, ended_at = ? WHERE id = ?",
        (outcome, outcome if outcome != "crashed" else "failed", error, ended, run_id),
    )
    conn.execute(
        "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
        "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?",
        (task_id,),
    )
    conn.commit()


def test_respawn_guard_unchanged_block_reason_cleared_after_unblocked(kanban_home):
    base = 8_900_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unblocked", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _append_blocked(conn, tid, "same hold", created_at=base)
        _append_blocked(conn, tid, "same hold", created_at=base + 1)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "unblocked", {"reason": "operator cleared"})
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) is None


def test_respawn_guard_unchanged_block_reason_when_two_blocked_match(kanban_home):
    base = 9_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _append_blocked(conn, tid, "same hold", created_at=base)
        _append_blocked(conn, tid, "same hold", created_at=base + 1)
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "unchanged_block_reason"


def test_respawn_guard_not_unchanged_with_one_or_mismatched_blocked(kanban_home):
    base = 9_100_000
    with kbc.connect() as conn:
        one = kb.create_task(conn, title="one", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (one,))
        _append_blocked(conn, one, "same hold", created_at=base)
        conn.commit()
        assert kbd.check_respawn_guard(conn, one) is None

        two = kb.create_task(conn, title="two", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (two,))
        _append_blocked(conn, two, "hold a", created_at=base)
        _append_blocked(conn, two, "hold b", created_at=base + 1)
        conn.commit()
        assert kbd.check_respawn_guard(conn, two) is None


def test_respawn_guard_fresh_distinct_block_not_unchanged(kanban_home):
    base = 9_200_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _append_blocked(conn, tid, "same hold", created_at=base)
        _append_blocked(conn, tid, "same hold", created_at=base + 1)
        _append_blocked(conn, tid, "new operator hold", created_at=base + 2)
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) is None


def test_dispatch_skips_duplicate_respawn_guarded_for_unchanged_block(
    kanban_home, all_assignees_spawnable,
):
    base = 9_300_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dedupe", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _append_blocked(conn, tid, "same hold", created_at=base)
        _append_blocked(conn, tid, "same hold", created_at=base + 1)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "respawn_guarded", {"reason": "unchanged_block_reason"},
            )
        conn.commit()

        kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a, **k: 1)
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (tid,),
            ).fetchall()
        ]
        assert kinds.count("respawn_guarded") == 1


def test_explicit_do_not_dispatch_wins_over_elapsed_rate_limit_spawn_path(
    kanban_home, monkeypatch,
):
    """Past rate-limit cooldown the guard returns None to allow spawn; explicit
    markers must still block."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="rl",
            body="Operator note: wait for quota",
            assignee="a",
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            ("do not dispatch — quota may be back", tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "explicit_do_not_dispatch"


def test_respawn_guard_explicit_do_not_dispatch_marker_in_body(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="t",
            body="King seat is already doing this workstream",
            assignee="a",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "explicit_do_not_dispatch"
        assert kbd.check_respawn_guard(conn, tid, lane="review") == "explicit_do_not_dispatch"


def test_respawn_guard_possible_duplicate_not_explicit_marker(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="t",
            body="possible duplicate of card #12",
            assignee="a",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) != "explicit_do_not_dispatch"


def test_respawn_guard_explicit_marker_in_title_and_comment(kanban_home):
    with kbc.connect() as conn:
        title_tid = kb.create_task(
            conn, title="Please DO NOT DISPATCH this card", assignee="a",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (title_tid,))
        comment_tid = kb.create_task(conn, title="c", assignee="a")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (comment_tid,))
        kb.add_comment(conn, comment_tid, author="ops", body="king seat is already doing this")
        conn.commit()
        assert kbd.check_respawn_guard(conn, title_tid) == "explicit_do_not_dispatch"
        assert kbd.check_respawn_guard(conn, comment_tid) == "explicit_do_not_dispatch"


@pytest.mark.parametrize("outcome", ["crashed", "spawn_failed", "failed"])
def test_blocker_auth_ignores_stale_last_failure_error_on_latest_run(
    kanban_home, outcome,
):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale err", assignee="a")
        _seed_ended_run(
            conn,
            tid,
            outcome=outcome,
            error="worker process gone",
        )
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("429 rate limit", tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) != "blocker_auth"


def test_blocker_auth_from_latest_run_error_not_task_row(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="run err", assignee="a")
        _seed_ended_run(conn, tid, outcome="failed", error="403 unauthorized")
        conn.execute(
            "UPDATE tasks SET last_failure_error = NULL WHERE id = ?",
            (tid,),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"


def test_blocker_auth_still_skips_crashed_run_error(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="crashed auth", assignee="a")
        _seed_ended_run(
            conn,
            tid,
            outcome="crashed",
            error="authentication failed during teardown",
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) != "blocker_auth"
