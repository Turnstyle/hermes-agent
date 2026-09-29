"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_json_includes_runtime_limit(kanban_home):
    with kbc.connect() as conn:
        bounded_id = kb.create_task(
            conn, title="bounded task", max_runtime_seconds=2700
        )
        uncapped_id = kb.create_task(conn, title="uncapped task")

    bounded = json.loads(kc.run_slash(f"show {bounded_id} --json"))
    uncapped = json.loads(kc.run_slash(f"show {uncapped_id} --json"))

    assert bounded["task"]["max_runtime_seconds"] == 2700
    assert uncapped["task"]["max_runtime_seconds"] is None


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert "child task" in output
    assert parent_id in output
    assert "Cannot operate on a closed database" not in output


def test_kanban_edit_updates_documented_task_fields(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="old title", body="old body", priority=2)

    kc.run_slash(
        f"edit {task_id} --title 'new title' --body 'new body' --priority 70"
    )

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
    assert (task.title, task.body, task.priority) == ("new title", "new body", 70)
    assert any(event.kind == "reprioritized" for event in events)


@pytest.mark.parametrize("status", ["todo", "triage", "missing", "done", "archived"])
@pytest.mark.parametrize("result", ["work finished", None])
def test_complete_refusal_names_task_status(kanban_home, capsys, status, result):
    with kbc.connect_closing() as conn:
        if status == "todo":
            parent_id = kb.create_task(conn, title="parent")
            task_id = kb.create_task(conn, title="waiting child", parents=(parent_id,))
            # A removed dependency can leave the card in todo until it is promoted.
            conn.execute("DELETE FROM task_links WHERE child_id = ?", (task_id,))
            conn.commit()
        elif status == "missing":
            task_id = "t_ffffffff"
        else:
            task_id = kb.create_task(conn, title=status, triage=status == "triage")
            if status in {"done", "archived"}:
                assert kb.complete_task(conn, task_id, result="already finished")
                if status == "archived":
                    assert kb.archive_task(conn, task_id)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        before = dict(row) if row else None

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result=result, summary=None, metadata=None, force=False,
    ))
    output = capsys.readouterr()

    assert rc != 0
    expected = (
        f"cannot complete {task_id}: card is {status}; promote it to ready first "
        f"(hermes kanban promote {task_id})"
        if status in {"todo", "triage"} else
        f"cannot complete {task_id}: unknown id" if status == "missing" else
        f"cannot complete {task_id}: already {status}"
    )
    assert expected in output.err
    with kbc.connect_closing() as conn:
        after_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert (dict(after_row) if after_row else None) == before


@pytest.mark.parametrize("status", ["todo", "triage"])
def test_complete_waiting_card_names_open_parents(kanban_home, capsys, status):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="open parent")
        other_parent = kb.create_task(conn, title="blocked parent", initial_status="blocked")
        task_id = kb.create_task(
            conn, title="waiting child", parents=(parent, other_parent),
            triage=status == "triage",
        )
        assert kb.get_task(conn, task_id).status == status
        blockers = kb.unsatisfied_parents(conn, task_id)

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result="finished", summary=None, metadata=None, force=False,
    ))
    output = capsys.readouterr().err

    detail = ", ".join(f"{pid} ({parent_status})" for pid, parent_status in blockers)
    assert rc == 1
    assert output.strip() == (
        f"cannot complete {task_id}: card is {status} and has unsatisfied parent dependencies: "
        f"{detail}; complete the parents first, or `hermes kanban unlink <parent> {task_id}`."
    )
    assert "promote it to ready first" not in output


def test_complete_todo_without_parent_suggests_promotion(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="former parent")
        task_id = kb.create_task(conn, title="todo child", parents=(parent,))
        # Keep the status after removing the dependency, as a stale todo can remain.
        conn.execute("DELETE FROM task_links WHERE child_id = ?", (task_id,))
        conn.commit()
        assert kb.get_task(conn, task_id).status == "todo"
        assert kb.unsatisfied_parents(conn, task_id) == []

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result="finished", summary=None, metadata=None, force=False,
    ))
    assert rc == 1
    assert capsys.readouterr().err.strip() == (
        f"cannot complete {task_id}: card is todo; promote it to ready first "
        f"(hermes kanban promote {task_id})"
    )


def test_complete_done_with_open_parent_stays_already_done(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="completed child")
        assert kb.complete_task(conn, task_id, result="finished")
        parent = kb.create_task(conn, title="open parent")
        kb.link_tasks(conn, parent, task_id)
        assert kb.unsatisfied_parents(conn, task_id) == [(parent, "ready")]

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result="finished", summary=None, metadata=None, force=False,
    ))
    assert rc == 1
    assert capsys.readouterr().err.strip() == f"cannot complete {task_id}: already done"


def test_complete_ready_with_open_parent_keeps_existing_text(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        assert kb.complete_task(conn, parent, result="finished")
        task_id = kb.create_task(conn, title="ready child", parents=(parent,))
        # A parent can reopen after a child was made ready.
        conn.execute(
            "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (parent,)
        )
        conn.commit()
        assert kb.get_task(conn, task_id).status == "ready"

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result="finished", summary=None, metadata=None, force=False,
    ))
    assert rc == 1
    assert capsys.readouterr().err.strip() == (
        f"cannot complete {task_id}: unsatisfied parent dependencies: {parent} (ready); "
        f"complete the parents first, or `hermes kanban unlink <parent> {task_id}`."
    )


def test_bulk_complete_waiting_and_missing_have_separate_messages(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="open parent")
        task_id = kb.create_task(conn, title="waiting child", parents=(parent,))
    missing_id = "t_ffffffff"

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id, missing_id], result="finished", summary=None,
        metadata=None, force=False,
    ))
    lines = capsys.readouterr().err.splitlines()

    assert rc == 1
    assert lines == [
        f"cannot complete {task_id}: card is todo and has unsatisfied parent dependencies: "
        f"{parent} (ready); complete the parents first, or "
        f"`hermes kanban unlink <parent> {task_id}`.",
        f"cannot complete {missing_id}: unknown id",
    ]


def test_ready_complete_path_unchanged(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ready card")
        assert kb.get_task(conn, task_id).status == "ready"

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result="finished", summary=None, metadata=None, force=False,
    ))

    assert rc == 0
    assert f"Completed {task_id}" in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert (task.status, task.result) == ("done", "finished")


def test_worker_link_preserves_foreign_child_rules(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        worker = kb.create_task(conn, title="worker")
        assert kb.claim_task(conn, worker, claimer="worker") is not None
        worker_run_id = kb.get_task(conn, worker).current_run_id
        parent = kb.create_task(conn, title="unfinished parent")
        ready_child = kb.create_task(conn, title="foreign ready child")
        running_child = kb.create_task(conn, title="foreign running child")
        assert kb.claim_task(conn, running_child, claimer="other") is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", worker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(worker_run_id))

    assert kc._cmd_link(argparse.Namespace(
        parent_id=parent, child_id=ready_child,
    )) == 0
    with pytest.raises(ValueError, match="child is already running"):
        kc._cmd_link(argparse.Namespace(
            parent_id=parent, child_id=running_child,
        ))
    # Owner handoff: the worker links its own running card, proving ownership
    # with HERMES_KANBAN_RUN_ID — the one path _cmd_link forwards a run id for.
    assert kc._cmd_link(argparse.Namespace(
        parent_id=parent, child_id=worker,
    )) == 0

    with kbc.connect_closing() as conn:
        assert kb.parent_ids(conn, ready_child) == [parent]
        assert kb.parent_ids(conn, running_child) == []
        assert kb.parent_ids(conn, worker) == [parent]


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kbc.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kbc.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db_connect as kbc

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kbc.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------
