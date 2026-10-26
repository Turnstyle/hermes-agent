"""Fleet unblock evaluations must survive CLI forwarding and optional DB fences."""

import argparse
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _snapshot(conn, tid):
    return tuple(
        [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE {key} = ? ORDER BY id", (tid,),
        )]
        for table, key in (
            ("tasks", "id"), ("task_runs", "task_id"),
            ("task_comments", "task_id"), ("task_events", "task_id"),
        )
    )


@pytest.mark.parametrize("history", ["current", "stale", "absent"])
def test_cli_unblock_compares_event_and_can_land_in_todo(board, history, capsys):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="cmd"))
    with pytest.raises(SystemExit) as help_exit:
        parser.parse_args(["kanban", "unblock", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--to-todo" in help_text
    assert "--expected-blocked-event-id" in help_text

    tid = kb.create_task(board, title="reviewed carry", assignee="worker")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        if history != "absent":
            kb._append_event(board, tid, "blocked", {"reason": "waiting"})
    event = next((e.id for e in kb.list_events(board, tid) if e.kind == "blocked"), 0)
    if history == "stale":
        with kb.write_txn(board):
            kb._append_event(board, tid, "blocked", {"reason": "new obstacle"})
    before = _snapshot(board, tid)
    args = parser.parse_args([
        "kanban", "unblock", tid, "--to-todo",
        "--expected-blocked-event-id", str(event),
    ])
    result = cli.kanban_command(args)
    if history == "stale":
        assert result == 1
        assert _snapshot(board, tid) == before
    else:
        assert result == 0
        assert kb.get_task(board, tid).status == "todo"
        assert kb.list_events(board, tid)[-1].kind == "unblocked"


@pytest.mark.parametrize("busy,parent_status,guarded", [
    (None, None, True), (None, "done", True), (None, "archived", True),
    (None, "ready", True), (None, "ready", False),
    ("current_run", None, True), ("orphan_run", None, True),
    ("claim_lock", None, True), ("claim_expires", None, True),
    ("worker_pid", None, True), ("current_run", None, False),
])
def test_unblock_optional_guards_preserve_busy_or_parent_gated_cards(
    board, busy, parent_status, guarded,
):
    tid = kb.create_task(board, title="guarded release", assignee="worker")
    if parent_status:
        parent = kb.create_task(board, title="upstream", assignee="worker")
        kb.link_tasks(board, parent_id=parent, child_id=tid)
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status=? WHERE id=?", (parent_status, parent))
            board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    if busy in {"current_run", "orphan_run"}:
        assert kb.claim_task(board, tid) is not None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        if busy == "orphan_run":
            board.execute(
                "UPDATE tasks SET current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?",
                (tid,),
            )
        if busy in {"claim_lock", "claim_expires", "worker_pid"}:
            value = "owner-lock" if busy == "claim_lock" else (os.getpid() if busy == "worker_pid" else 0)
            board.execute(f"UPDATE tasks SET {busy}=? WHERE id=?", (value, tid))
        kb._append_event(board, tid, "blocked", {"reason": "evaluated"})
    before = _snapshot(board, tid)
    kwargs = {"require_idle": True, "require_closed_parents": True} if guarded else {}
    released = kb.unblock_task(board, tid, actor="owner", reason="approved", **kwargs)
    if guarded and (busy or parent_status == "ready"):
        assert released is False
        assert _snapshot(board, tid) == before
    else:
        assert released is True
        assert kb.get_task(board, tid).status == ("todo" if parent_status == "ready" else "ready")
        assert kb.list_events(board, tid)[-1].kind == "unblocked"
