"""Parent-gate exceptions on complete_task, blocked create bookkeeping, unchanged_block promotion guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_parent_gate import clear_pr_merge_cache, record_parent_rehome


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


@pytest.fixture(autouse=True)
def _clear_pr_merge_cache_each_test():
    clear_pr_merge_cache()
    yield
    clear_pr_merge_cache()


def _open_parent_and_ready_child(conn, parent_title: str = "open parent"):
    parent = kb.create_task(conn, title=parent_title)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()
    child = kb.create_task(conn, title="child")
    kb.link_tasks(conn, parent, child)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    conn.commit()
    return parent, child


def test_free_text_on_child_status_keeps_parent_gate(kanban_home):
    with kbc.connect() as conn:
        parent, child = _open_parent_and_ready_child(conn)
        kb._append_event(
            conn,
            child,
            "status",
            {"note": "not rehomed; hold for review"},
        )
        assert kb.complete_task(conn, child, result="should not close") is False
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, parent).status == "todo"


def test_free_text_on_parent_events_keeps_parent_gate(kanban_home):
    with kbc.connect() as conn:
        parent, child = _open_parent_and_ready_child(conn)
        kb._append_event(
            conn,
            parent,
            "status",
            {"note": "not rehomed; hold for review"},
        )
        assert kb.complete_task(conn, child, result="nope") is False
        assert kb.get_task(conn, child).status == "ready"

        kb.add_comment(
            conn,
            parent,
            "reviewer",
            "not rehomed; hold for review",
        )
        assert kb.complete_task(conn, child, result="nope") is False
        assert kb.get_task(conn, child).status == "ready"

        kb._append_event(
            conn,
            parent,
            "rehomed",
            {"successor_id": "t_does_not_exist"},
        )
        assert kb.complete_task(conn, child, result="nope") is False
        assert kb.get_task(conn, child).status == "ready"


def test_record_parent_rehome_releases_only_that_parent(kanban_home):
    with kbc.connect() as conn:
        parent, child = _open_parent_and_ready_child(conn)
        successor = kb.create_task(conn, title="successor")
        assert record_parent_rehome(conn, parent, successor, action="rehomed") is True
        assert kb.complete_task(conn, child, result="closed copy") is True
        assert kb.get_task(conn, child).status == "done"

        parent_a = kb.create_task(conn, title="parent A rehomed")
        parent_b = kb.create_task(conn, title="parent B open")
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id IN (?, ?)",
            (parent_a, parent_b),
        )
        conn.commit()
        dual_child = kb.create_task(conn, title="two-parent child")
        kb.link_tasks(conn, parent_a, dual_child)
        kb.link_tasks(conn, parent_b, dual_child)
        successor_a = kb.create_task(conn, title="successor A")
        assert record_parent_rehome(conn, parent_a, successor_a, action="rehomed") is True
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (dual_child,))
        conn.commit()
        assert kb.complete_task(conn, dual_child, result="nope") is False
        assert kb.get_task(conn, dual_child).status == "ready"

        successor_b = kb.create_task(conn, title="successor B")
        assert record_parent_rehome(conn, parent_b, successor_b, action="rehomed") is True
        assert kb.complete_task(conn, dual_child, result="both parents satisfied") is True
        assert kb.get_task(conn, dual_child).status == "done"


def test_record_parent_superseded_releases_child(kanban_home):
    with kbc.connect() as conn:
        parent, child = _open_parent_and_ready_child(conn)
        successor = kb.create_task(conn, title="replacement")
        assert record_parent_rehome(conn, parent, successor, action="superseded") is True
        assert kb.complete_task(conn, child, result="done") is True
        assert kb.get_task(conn, child).status == "done"


def test_human_hold_keeps_gate_despite_recorded_rehome(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="held parent")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        conn.commit()
        kb.claim_task(conn, parent)
        kb.block_task(
            conn,
            parent,
            reason="review-required: human",
            expected_run_id=kb.get_task(conn, parent).current_run_id,
        )
        successor = kb.create_task(conn, title="successor")
        assert record_parent_rehome(conn, parent, successor, action="rehomed") is True
        child = kb.create_task(conn, title="child under hold")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="should not close") is False
        assert kb.get_task(conn, child).status == "ready"


def test_unmerged_pr_comment_text_keeps_gate_without_gh(kanban_home, monkeypatch):
    gh_calls: list[str] = []

    def _forbidden_query(_url: str):
        gh_calls.append(_url)
        return {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        _forbidden_query,
    )
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="Merge the PR")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
        conn.commit()
        child = kb.create_task(conn, title="merge child")
        kb.link_tasks(conn, parent, child)
        kb.add_comment(
            conn,
            parent,
            "reviewer",
            "PR not merged; https://github.com/acme/app/pull/15 remains open",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="nope") is False
        assert kb.get_task(conn, child).status == "ready"
        assert gh_calls == []


def test_completion_contract_unmerged_pr_keeps_gate(kanban_home, monkeypatch):
    pr_url = "https://github.com/acme/app/pull/15"

    def _open(_url: str):
        return {"state": "OPEN", "mergedAt": None}

    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        _open,
    )
    with kbc.connect() as conn:
        parent = kb.create_task(
            conn,
            title="Merge the PR",
            completion_contract=pr_url,
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
        conn.commit()
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="nope") is False
        assert kb.get_task(conn, child).status == "ready"


def test_completion_contract_gh_failure_keeps_gate(kanban_home, monkeypatch):
    pr_url = "https://github.com/acme/app/pull/16"

    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        lambda _url: None,
    )
    with kbc.connect() as conn:
        parent = kb.create_task(
            conn,
            title="Merge the PR",
            completion_contract=pr_url,
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
        conn.commit()
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="nope") is False
        assert kb.get_task(conn, child).status == "ready"


def test_verified_merged_pr_releases_child_other_parent_still_blocks(kanban_home, monkeypatch):
    pr_url = "https://github.com/acme/app/pull/20"

    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        lambda _url: {"state": "MERGED", "mergedAt": "2026-09-28T12:00:00Z"},
    )
    with kbc.connect() as conn:
        merged_parent = kb.create_task(
            conn,
            title="Merge the PR",
            completion_contract=pr_url,
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (merged_parent,))
        conn.commit()
        merged_child = kb.create_task(conn, title="merged child")
        kb.link_tasks(conn, merged_parent, merged_child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (merged_child,))
        conn.commit()
        assert kb.complete_task(conn, merged_child, result="shipped") is True
        assert kb.get_task(conn, merged_child).status == "done"

        parent_a = kb.create_task(
            conn,
            title="Merge the PR A",
            completion_contract=pr_url,
        )
        parent_b = kb.create_task(conn, title="Implement the feature B")
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id IN (?, ?)",
            (parent_a, parent_b),
        )
        conn.commit()
        dual_child = kb.create_task(conn, title="two-parent merge child")
        kb.link_tasks(conn, parent_a, dual_child)
        kb.link_tasks(conn, parent_b, dual_child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (dual_child,))
        conn.commit()
        assert kb.complete_task(conn, dual_child, result="nope") is False
        assert kb.get_task(conn, dual_child).status == "ready"

        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent_b,))
        conn.commit()
        assert kb.complete_task(conn, dual_child, result="shipped") is True
        assert kb.get_task(conn, dual_child).status == "done"


def test_human_hold_keeps_gate_despite_verified_merged_pr(kanban_home, monkeypatch):
    pr_url = "https://github.com/acme/app/pull/21"

    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        lambda _url: {"state": "MERGED", "mergedAt": "2026-09-28T12:00:00Z"},
    )
    with kbc.connect() as conn:
        parent = kb.create_task(
            conn,
            title="Merge the PR",
            completion_contract=pr_url,
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        conn.commit()
        kb.claim_task(conn, parent)
        kb.block_task(
            conn,
            parent,
            reason="review-required: human",
            expected_run_id=kb.get_task(conn, parent).current_run_id,
        )
        child = kb.create_task(conn, title="child under human hold")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="should not close") is False
        assert kb.get_task(conn, child).status == "ready"


@pytest.mark.parametrize("parent_status", ["review", "blocked", "triage", "running"])
def test_parent_awaiting_review_or_blocked_keeps_gate_despite_merged_pr(
    kanban_home, monkeypatch, parent_status
):
    """R2 HIGH: request_review writes no sticky block, so status itself must hold."""
    pr_url = "https://github.com/acme/app/pull/23"
    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        lambda _url: {"state": "MERGED", "mergedAt": "2026-09-28T12:00:00Z"},
    )
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="Implement", completion_contract=pr_url)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (parent_status, parent))
        conn.commit()
        child = kb.create_task(conn, title="deploy child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="should not close") is False
        assert kb.get_task(conn, child).status == "ready"


@pytest.mark.parametrize("parent_status", ["review", "triage", "running"])
def test_held_parent_keeps_gate_despite_recorded_rehome(kanban_home, parent_status):
    """R3 HIGH: triage (and review/running) is a person or worker hold; a
    recorded rehome must not release it."""
    with kbc.connect() as conn:
        successor = kb.create_task(conn, title="successor")
        parent = kb.create_task(conn, title="held parent")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (parent_status, parent))
        conn.commit()
        assert record_parent_rehome(conn, parent, successor, action="rehomed") is True
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="should not close") is False


def test_dependency_wait_parent_with_open_grandparent_keeps_gate(kanban_home, monkeypatch):
    """R4 HIGH: block_task(kind="dependency") parks a parent in todo while its
    own prerequisite is open; rehome or merged-PR evidence must not skip it."""
    pr_url = "https://github.com/acme/app/pull/24"
    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        lambda _url: {"state": "MERGED", "mergedAt": "2026-09-28T12:00:00Z"},
    )
    with kbc.connect() as conn:
        grandparent = kb.create_task(conn, title="upstream prerequisite")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (grandparent,))
        parent = kb.create_task(conn, title="waits on upstream", completion_contract=pr_url)
        kb.link_tasks(conn, grandparent, parent)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
        conn.commit()
        successor = kb.create_task(conn, title="successor")
        assert record_parent_rehome(conn, parent, successor, action="rehomed") is True
        child = kb.create_task(conn, title="downstream child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="should not close") is False

        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (grandparent,))
        conn.commit()
        assert kb.complete_task(conn, child, result="upstream done") is True


def test_pr_acceptance_system_event_releases_without_completion_contract(kanban_home, monkeypatch):
    pr_url = "https://github.com/acme/app/pull/22"

    monkeypatch.setattr(
        "hermes_cli.kanban_parent_gate.query_pr_merge_state",
        lambda url: (
            {"state": "MERGED", "mergedAt": "2026-09-28T12:00:00Z"}
            if url == pr_url
            else None
        ),
    )
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="Merge the PR")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
        conn.commit()
        kb._append_event(conn, parent, "pr_acceptance", {"pr_url": pr_url})
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.complete_task(conn, child, result="shipped") is True
        assert kb.get_task(conn, child).status == "done"


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
