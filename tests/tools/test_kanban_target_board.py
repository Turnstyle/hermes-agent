"""Model-tool ``board=`` routing through the real tool handlers (fleet carry K17).

A dispatched worker's explicit ``board=`` resolves through its injected
``HERMES_KANBAN_DB`` pin (``agent.delegation_context.explicit_board_intent_is_pinned``:
workers physically cannot see other boards). The fleet's carried copy of this file
asserted the opposite for workers; that would open the board fence, so the port
asserts the fence end to end and keeps the carried orchestrator routing contract.
"""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt
from tools.registry import registry


def _two_pinned_boards(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.create_board("bizdev")
    kb.create_board("sales")
    source_db = kb.kanban_db_path(board="bizdev")
    target_db = kb.kanban_db_path(board="sales")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(source_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "bizdev")
    return source_db, target_db


def test_worker_named_board_stays_on_its_pinned_board(monkeypatch, tmp_path):
    source_db, target_db = _two_pinned_boards(monkeypatch, tmp_path)
    with kbc.connect(source_db) as conn:
        own = kb.create_task(conn, title="source", assignee="test-worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    out = json.loads(registry.dispatch("kanban_create", {
        "title": "cross-board prospect", "assignee": "test-worker",
        "board": "sales", "initial_status": "blocked",
    }))
    assert out.get("ok"), out
    tid = out["task_id"]
    with kbc.connect(source_db) as conn:
        assert kb.get_task(conn, tid) is not None
    with kbc.connect(target_db) as conn:
        assert kb.get_task(conn, tid) is None
    assert kb.kanban_db_path() == source_db
    assert kb.kanban_db_path(board="sales") == source_db
    # The pin does not remove the worker task-ownership guard either.
    refused = json.loads(kt._handle_complete({"task_id": tid, "board": "sales", "summary": "not mine"}))
    assert "error" in refused
    with kbc.connect(source_db) as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_orchestrator_named_board_routes_without_moving_the_pin(monkeypatch, tmp_path):
    source_db, target_db = _two_pinned_boards(monkeypatch, tmp_path)
    created = json.loads(kt._handle_create({
        "title": "sales prospect", "assignee": "test-worker",
        "board": "sales", "initial_status": "blocked",
    }))
    assert created.get("ok"), created
    tid = created["task_id"]
    with kbc.connect(target_db) as conn:
        assert kb.get_task(conn, tid) is not None
        parent = kb.create_task(conn, title="sales parent")
    with kbc.connect(source_db) as conn:
        assert kb.get_task(conn, tid) is None
    assert json.loads(kt._handle_show({"task_id": tid, "board": "sales"}))["task"]["id"] == tid
    comment = json.loads(kt._handle_comment({"task_id": tid, "board": "sales", "body": "target receipt"}))
    assert comment.get("ok"), comment
    linked = json.loads(kt._handle_link({"parent_id": parent, "child_id": tid, "board": "sales"}))
    assert linked.get("ok"), linked
    listed = json.loads(kt._handle_list({"board": "sales"}))
    assert {row["id"] for row in listed["tasks"]} == {tid, parent}
    done = json.loads(kt._handle_complete({"task_id": parent, "board": "sales", "summary": "target done"}))
    assert done.get("ok"), done
    with kbc.connect(target_db) as conn:
        assert kb.get_task(conn, parent).status == "done"
        assert kb.list_comments(conn, tid)[-1].body == "target receipt"
        assert [pid for pid, _ in kb.unsatisfied_parents(conn, tid)] == []
    assert kb.kanban_db_path() == source_db
