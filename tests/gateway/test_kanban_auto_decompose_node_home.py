"""Auto-decomposition respects the optional fleet card home, not just the local roster."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as dispatcher
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


def _dispatcher():
    settings = dispatcher._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return dispatcher._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)


def test_auto_decompose_only_uses_local_home_and_local_assignees(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(decomp.socket, "gethostname", lambda: "max.example")
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda kb: ["default"])
    monkeypatch.setattr(decomp, "_call_aux", lambda *args, **kwargs: (
        json.dumps({"fanout": True, "tasks": [
            {"title": "local work", "body": "do it", "assignee": "remote-king", "parents": []},
        ]}), "",
    ))
    with kbc.connect_closing() as conn:
        remote = kb.create_task(conn, title="remote", assignee="default", triage=True)
        local = kb.create_task(conn, title="local", assignee="default", triage=True)
        moved_here = kb.create_task(conn, title="moved here", assignee="default", triage=True)
        conn.execute("CREATE TABLE fleet_kanban_issue_map ("
                     "local_task_id TEXT PRIMARY KEY, source_node TEXT NOT NULL, current_node TEXT)")
        conn.executemany(
            "INSERT INTO fleet_kanban_issue_map VALUES (?, ?, ?)",
            [(remote, "max", "sheldon"), (local, "max", None), (moved_here, "sheldon", "max")],
        )
    assert _dispatcher().auto_decompose_tick(3) == 2
    with kbc.connect_closing() as conn:
        remote_root = kb.get_task(conn, remote)
        assert remote_root is not None and remote_root.status == "triage"
        for task_id in (local, moved_here):
            root = kb.get_task(conn, task_id)
            assert root is not None and root.status == "todo"
            child_id = conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
            ).fetchone()[0]
            child = kb.get_task(conn, child_id)
            assert child is not None and child.assignee == "default"
            assert child.status == "ready"


def test_auto_decompose_without_fleet_table_keeps_local_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda kb: ["default"])
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="local only", assignee="default", triage=True)
    seen = []
    monkeypatch.setattr(decomp, "decompose_task", lambda task_id, author: (
        seen.append((task_id, author)) or SimpleNamespace(ok=True, fanout=False, child_ids=None)
    ))
    assert _dispatcher().auto_decompose_tick(1) == 1
    assert seen == [(task_id, "auto-decomposer")]
