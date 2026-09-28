"""Parent-gate exceptions on complete_task, blocked create bookkeeping, unchanged_block promotion guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _newest_event_payload(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return json.loads(row["payload"] or "{}") if row else {}


def test_complete_task_rehome_bypasses_parent_gate(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent still open")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
        conn.commit()
        assert kb.get_task(conn, parent).status == "todo"

        rehomed_child = kb.create_task(conn, title="rehomed child")
        kb.link_tasks(conn, parent, rehomed_child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (rehomed_child,))
        conn.commit()
        kb._append_event(
            conn,
            rehomed_child,
            "status",
            {"note": "REHOMED: superseded by t_abc"},
        )
        assert kb.complete_task(conn, rehomed_child, result="closed copy") is True
        assert kb.get_task(conn, rehomed_child).status == "done"

        blocked_child = kb.create_task(conn, title="blocked child")
        kb.link_tasks(conn, parent, blocked_child)
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked_child,))
        conn.commit()
        kb._append_event(
            conn, blocked_child, "blocked", {"reason": "need a credential"},
        )
        before = kb.get_task(conn, blocked_child).status
        assert kb.complete_task(conn, blocked_child, result="should not close") is False
        assert kb.get_task(conn, blocked_child).status == before


def test_complete_task_merge_parent_todo_with_merged_pr_evidence(kanban_home):
    with kbc.connect() as conn:
        merge_parent = kb.create_task(
            conn, title="Merge the PR", body="bookkeeping",
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (merge_parent,))
        conn.commit()
        merge_child = kb.create_task(conn, title="merge child")
        kb.link_tasks(conn, merge_parent, merge_child)
        kb.add_comment(
            conn,
            merge_parent,
            "reviewer",
            "https://github.com/acme/app/pull/15 merged",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (merge_child,))
        conn.commit()
        assert kb.complete_task(conn, merge_child, result="shipped") is True
        assert kb.get_task(conn, merge_child).status == "done"

        plain_parent = kb.create_task(conn, title="Implement the feature")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (plain_parent,))
        conn.commit()
        plain_child = kb.create_task(conn, title="plain child")
        kb.link_tasks(conn, plain_parent, plain_child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (plain_child,))
        conn.commit()
        assert kb.complete_task(conn, plain_child, result="nope") is False
        assert kb.get_task(conn, plain_child).status == "ready"

        running_merge_parent = kb.create_task(conn, title="Merge the PR", body="bookkeeping")
        kb.add_comment(
            conn,
            running_merge_parent,
            "reviewer",
            "https://github.com/acme/app/pull/99 merged",
        )
        running_merge_child = kb.create_task(conn, title="running merge child")
        kb.link_tasks(conn, running_merge_parent, running_merge_child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (running_merge_child,))
        conn.commit()
        kb.claim_task(conn, running_merge_parent)
        assert kb.get_task(conn, running_merge_parent).status == "running"
        assert kb.complete_task(conn, running_merge_child, result="blocked") is False


def test_create_task_blocked_initial_status_bookkeeping_flag(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="parked", initial_status="blocked")
        payload = _newest_event_payload(conn, tid)
        assert payload.get("reason") == "initial_status"
        assert payload.get("bookkeeping") is True


def _park_breaker_blocked(conn, task_id: str) -> None:
    """Breaker-parked blocked row: no blocked/unblocked events, so _has_sticky_block is false."""
    conn.execute(
        "UPDATE tasks SET status = 'blocked', consecutive_failures = 0 WHERE id = ?",
        (task_id,),
    )
    conn.commit()


def test_recompute_ready_unchanged_block_reason_and_sticky_guard(kanban_home):
    # unchanged_block runs only when _has_sticky_block is false: newest blocked/unblocked
    # must not be ``blocked`` (breaker re-park after unblock + duplicate audit rows).
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="done parent")
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="done")

        child = kb.create_task(conn, title="breaker blocked child", parents=[parent])
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        kb.claim_task(conn, child)
        kb.block_task(
            conn,
            child,
            reason="same transient fault",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        kb.unblock_task(conn, child)
        _park_breaker_blocked(conn, child)
        kb._append_event(conn, child, "blocked", {"reason": "same transient fault"})
        kb._append_event(conn, child, "blocked", {"reason": "same transient fault"})
        kb._append_event(conn, child, "unblocked", None)

        assert kb.get_task(conn, child).status == "blocked"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "blocked"
        unchanged = [e for e in kb.list_events(conn, child) if e.kind == "unchanged_block"]
        assert len(unchanged) == 1
        assert kb.recompute_ready(conn) == 0
        assert len([e for e in kb.list_events(conn, child) if e.kind == "unchanged_block"]) == 1

        sticky_tid = kb.create_task(conn, title="sticky explicit block")
        kb.claim_task(conn, sticky_tid)
        kb.block_task(
            conn,
            sticky_tid,
            reason="review-required: human",
            expected_run_id=kb.get_task(conn, sticky_tid).current_run_id,
        )
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, sticky_tid).status == "blocked"


def test_recompute_ready_different_blocked_reasons_still_promote(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent done")
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="done")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.recompute_ready(conn)
        kb.claim_task(conn, child)
        kb.block_task(
            conn,
            child,
            reason="first reason",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        kb.unblock_task(conn, child)
        _park_breaker_blocked(conn, child)
        kb._append_event(conn, child, "blocked", {"reason": "alpha"})
        kb._append_event(conn, child, "blocked", {"reason": "beta"})
        kb._append_event(conn, child, "unblocked", None)
        assert kb.get_task(conn, child).status == "blocked"
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, child).status == "ready"
