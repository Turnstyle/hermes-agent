"""Named tool targets must override dispatcher path pins without changing them."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt
from tools.registry import registry


def test_worker_create_targets_other_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.create_board("bizdev")
    kb.create_board("sales")
    source_db = kb.kanban_db_path(board="bizdev")
    target_db = kb.kanban_db_path(board="sales")
    with kbc.connect(source_db) as conn:
        own = kb.create_task(conn, title="source", assignee="test-worker")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(source_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "bizdev")
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    out = json.loads(registry.dispatch("kanban_create", {
        "title": "cross-board prospect", "assignee": "test-worker",
        "board": "sales", "initial_status": "blocked",
    }))
    assert out.get("ok"), out
    tid = out["task_id"]
    with kbc.connect(target_db) as conn:
        assert kb.get_task(conn, tid) is not None
    with kbc.connect(source_db) as conn:
        assert kb.get_task(conn, tid) is None
    assert json.loads(kt._handle_show({"task_id": tid, "board": "sales"}))["task"]["id"] == tid
    comment = json.loads(kt._handle_comment({"task_id": tid, "board": "sales", "body": "target receipt"}))
    assert comment.get("ok"), comment
    with kbc.connect(target_db) as conn:
        assert kb.list_comments(conn, tid)[-1].body == "target receipt"
    assert kb.kanban_db_path() == source_db
    assert json.loads(kt._handle_show({}))["task"]["id"] == own
    # Routing must not remove the existing worker task-ownership guard.
    refused = json.loads(kt._handle_complete({"task_id": tid, "board": "sales", "summary": "not mine"}))
    assert "error" in refused
    with kbc.connect(target_db) as conn:
        parent = kb.create_task(conn, title="sales parent")
    linked = json.loads(kt._handle_link({"parent_id": parent, "child_id": tid, "board": "sales"}))
    assert linked.get("ok"), linked
    with kbc.connect(target_db) as conn:
        assert [pid for pid, _ in kb.unsatisfied_parents(conn, tid)] == [parent]
    # List and arbitrary completion are available only to an orchestrator.
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    listed = json.loads(kt._handle_list({"board": "sales"}))
    assert {row["id"] for row in listed["tasks"]} == {tid, parent}
    done = json.loads(kt._handle_complete({"task_id": parent, "board": "sales", "summary": "target done"}))
    assert done.get("ok"), done
    with kbc.connect(target_db) as conn:
        assert kb.get_task(conn, parent).status == "done"
    with kbc.connect(source_db) as conn:
        assert kb.get_task(conn, own).status != "done"
    assert kb.kanban_db_path() == source_db
