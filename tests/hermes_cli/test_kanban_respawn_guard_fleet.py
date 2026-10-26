"""Fleet respawn-guard holds on the owner-runtime dispatcher: explicit holds, repeated
blocks and current-assignee PR attribution lifted only by fresh terminal PR evidence."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_state

_PR = "https://github.com/Owner/Repo/pull/51"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def test_newest_comment_hold_marker_holds_until_a_newer_comment(board):
    tid = kb.create_task(board, title="held", assignee="worker", body="do not dispatch other cards")
    assert kbd.check_respawn_guard(board, tid) is None  # body text describes other cards
    kb.add_comment(board, tid, author="king", body="Do not dispatch: king seat is on it")
    assert kbd.check_respawn_guard(board, tid) == "explicit_do_not_dispatch"
    kb.add_comment(board, tid, author="king", body="go ahead")
    assert kbd.check_respawn_guard(board, tid) is None


def test_same_block_reason_twice_holds_until_unblocked(board):
    tid = kb.create_task(board, title="looping", assignee="worker")
    for _ in range(2):
        kb._append_event(board, tid, "blocked", {"reason": "same obstacle"})
    assert kbd.check_respawn_guard(board, tid) == "unchanged_block_reason"
    kb._append_event(board, tid, "unblocked", {})
    assert kbd.check_respawn_guard(board, tid) is None


def test_active_pr_counts_only_the_current_assignees_new_pr(board):
    tid = kb.create_task(board, title="impl", assignee="worker", body=f"Input: {_PR}")
    kb.add_comment(board, tid, author="worker", body=f"Re-read {_PR}")  # a task input, not output
    kb.add_comment(board, tid, author="reviewer", body="Opened https://github.com/o/r/pull/7")
    assert kbd.check_respawn_guard(board, tid) is None
    kb.add_comment(board, tid, author="worker", body="Opened https://github.com/o/r/pull/8")
    assert kbd.check_respawn_guard(board, tid) == "active_pr"
    kb._append_event(board, tid, "unblocked", {})
    assert kbd.check_respawn_guard(board, tid) == "active_pr"  # same-second tie stays guarded
    with kb.write_txn(board):
        board.execute("UPDATE task_comments SET created_at = created_at - 10 WHERE task_id = ?", (tid,))
    assert kbd.check_respawn_guard(board, tid) is None  # an explicit unblock after the PR comment


def test_active_pr_lifts_only_on_fresh_terminal_pr_evidence(board, monkeypatch):
    tid = kb.create_task(board, title="impl", assignee="worker")
    kb.add_comment(board, tid, author="worker", body="Opened https://github.com/o/r/pull/8")
    seen = []
    monkeypatch.setattr(kanban_pr_state, "all_terminal", lambda prs, now, path=None: seen.append(prs) or False)
    lifted: list = []
    assert kbd.check_respawn_guard(board, tid, lifted=lifted) == "active_pr"
    assert seen == [{("o", "r", 8)}] and lifted == []
    monkeypatch.setattr(kanban_pr_state, "all_terminal", lambda prs, now, path=None: True)
    assert kbd.check_respawn_guard(board, tid, lifted=lifted) is None
    assert lifted == [(tid, "active_pr_lifted_terminal")]


def test_blocker_auth_reads_the_latest_runs_own_error(board):
    tid = kb.create_task(board, title="quota", assignee="worker")
    kb.claim_task(board, tid)
    kb._end_run(board, tid, outcome="failed", status="failed", error="401 authentication error", metadata={})
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                      "last_failure_error='worker process gone' WHERE id=?", (tid,))
    assert kbd.check_respawn_guard(board, tid) == "blocker_auth"
