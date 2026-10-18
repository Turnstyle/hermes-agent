"""Unblock is not an implicit request to dispatch another review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles


@pytest.fixture
def isolated_board(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def _blocked_review(conn, parents=None):
    tid = kb.create_task(conn, title="review handoff", assignee="builder", parents=parents or ())
    run = kb.claim_task(conn, tid)
    assert run is not None
    assert kb.request_review(conn, tid, summary="implemented", reviewer="reviewer",
                             expected_run_id=run.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    assert kb.block_task(conn, tid, reason="operator follow-up", kind="needs_input",
                         expected_run_id=review.current_run_id)
    assert kb._resume_status_from_events(conn, tid) == "review"
    return tid


@pytest.mark.parametrize("restore", [False, True])
def test_unblock_dispatch_requires_explicit_review_intent(isolated_board, restore):
    conn = isolated_board
    tid = _blocked_review(conn)
    if restore:
        assert kb.unblock_task(conn, tid, restore_review=True)
    else:
        assert kb.unblock_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == ("review" if restore else "ready")
    assert task.current_run_id is None
    seen = []

    def spawn(task, workspace, board=None):
        seen.append(task.skills or [])
        return None

    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert [row[0] for row in result.spawned] == [tid]
    assert ("sdlc-review" in seen[0]) is restore
    claimed = [e for e in kb.list_events(conn, tid) if e.kind == "claimed"][-1]
    assert claimed.payload is not None
    assert (claimed.payload.get("source_status") == "review") is restore


@pytest.mark.parametrize("restore", [False, True])
def test_unblock_parent_gate_remembers_explicit_intent(isolated_board, restore):
    conn = isolated_board
    parent = kb.create_task(conn, title="parent", assignee="planner")
    assert kb.complete_task(conn, parent, result="done")
    tid = _blocked_review(conn, parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
    assert kb.unblock_task(conn, tid, restore_review=restore)
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "todo"
    assert kb._resume_status_from_events(conn, tid) == ("review" if restore else "ready")
    assert kb.complete_task(conn, parent, result="done again")
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == ("review" if restore else "ready")


@pytest.mark.parametrize("status", ["blocked", "scheduled"])
@pytest.mark.parametrize("restore", [False, True])
def test_restore_flag_does_not_create_review_provenance(isolated_board, status, restore):
    conn = isolated_board
    tid = kb.create_task(conn, title="implementation work", assignee="builder")
    assert kb.block_task(conn, tid, reason="waiting")
    if status == "scheduled":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    assert kb.unblock_task(conn, tid, restore_review=restore)
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "ready"


@pytest.mark.parametrize("restore", [False, True])
def test_cli_unblock_carries_explicit_review_flag(isolated_board, restore):
    tid = _blocked_review(isolated_board)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    argv = ["kanban", "unblock", tid]
    if restore:
        argv.append("--restore-review")
    args = parser.parse_args(argv)
    assert cli.kanban_command(args) == 0
    task = kb.get_task(isolated_board, tid)
    assert task is not None and task.status == ("review" if restore else "ready")


@pytest.mark.parametrize("restore", [False, True, "false"])
def test_native_unblock_tool_requires_boolean_review_flag(isolated_board, restore):
    from tools import kanban_tools as tools
    from tools.registry import registry

    assert tools.KANBAN_UNBLOCK_SCHEMA["parameters"]["properties"]["restore_review"]["type"] == "boolean"
    tid = _blocked_review(isolated_board)
    args = {"task_id": tid}
    if restore is not False:
        args["restore_review"] = restore
    raw = registry.dispatch("kanban_unblock", args)
    assert isinstance(raw, str)
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["status"] == ("review" if restore is True else "ready")


def test_restore_flag_cannot_unblock_an_already_ready_task(isolated_board):
    conn = isolated_board
    tid = kb.create_task(conn, title="already ready", assignee="builder")
    events = kb.list_events(conn, tid)
    assert kb.unblock_task(conn, tid, restore_review=True) is False
    assert kb.list_events(conn, tid) == events
