"""Fleet CLI surfaces on ``hermes kanban``: read-only diagnostics, recover-ghost routing,
GC bundle gates and compare-event unblock, driven through ``kanban_command``."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
                 "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _run(*argv: str) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def _rows(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("tasks", "task_events", "task_runs", "task_comments")}
    finally:
        conn.close()


@pytest.mark.parametrize("argv", [("dispatch", "--dry-run", "--json"), ("refresh-pr-state",)])
def test_read_only_diagnostics_never_create_a_board(board_home, capsys, argv):
    db_path = kb.kanban_db_path()
    assert not db_path.exists()

    rc = _run(*argv)

    assert rc != 0
    assert "not initialized" in capsys.readouterr().err
    assert not db_path.exists()


def test_dry_run_dispatch_writes_nothing(board_home, capsys):
    kb.init_db()
    with kbc.connect_closing() as conn:
        ready = kb.create_task(conn, title="ready card", assignee="worker")
        stale = kb.create_task(conn, title="stale claim", assignee="worker")
        assert kb.claim_task(conn, stale) is not None
        # An expired claim is exactly what a real tick would reclaim.
        conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (stale,))
        conn.commit()
    db_path = kb.kanban_db_path()
    before = _rows(db_path)

    rc = _run("dispatch", "--dry-run", "--json")

    out = capsys.readouterr().out
    assert rc == 0, out
    assert _rows(db_path) == before
    assert ready in out


def test_recover_ghost_routes_before_board_init(board_home, monkeypatch):
    calls = []
    fake = types.ModuleType("hermes_cli.kanban_recover_ghost")
    fake.recover_ghost = lambda args: calls.append((args.task_id, args.run_id, args.apply)) or 0
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_recover_ghost", fake)

    assert _run("recover-ghost", "t_ghost", "--run", "7") == 0

    assert calls == [("t_ghost", 7, False)]
    assert not kb.kanban_db_path().exists()


@pytest.mark.parametrize("argv", [("recover-ghost", "t_ghost", "--run", "7"), ("refresh-pr-state",)])
def test_delegated_child_cannot_run_fleet_operator_verbs(board_home, monkeypatch, capsys, argv):
    import agent.delegation_context as delegation_context

    called = []
    fake = types.ModuleType("hermes_cli.kanban_recover_ghost")
    fake.recover_ghost = lambda args: called.append(args) or 0
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_recover_ghost", fake)
    monkeypatch.setattr(delegation_context, "kanban_path_is_fenced", lambda path: True)

    assert _run(*argv) == 1

    assert "cannot mutate Kanban tasks" in capsys.readouterr().err
    assert called == []


@pytest.mark.parametrize("gate_allows", [False, True])
def test_gc_keeps_archived_workspace_the_bundle_gate_refuses(board_home, monkeypatch, gate_allows):
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="archived with bundle", assignee="worker")
        workspace = kb.workspaces_root() / tid
        workspace.mkdir(parents=True)
        (workspace / "result.txt").write_text("accepted output", encoding="utf-8")
        conn.execute(
            "UPDATE tasks SET status = 'archived', workspace_kind = 'scratch', workspace_path = ? "
            "WHERE id = ?", (str(workspace), tid),
        )
        conn.commit()
    gate_calls = []
    monkeypatch.setattr(
        kbw, "preservation_gate",
        lambda task_id, path, conn=None: gate_calls.append(task_id) or gate_allows,
    )

    assert _run("gc") == 0

    assert gate_calls == [tid]
    assert workspace.exists() is (not gate_allows)


def test_unblock_compare_event_and_to_todo_reach_the_board(board_home, capsys):
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="blocked card", assignee="worker")
        assert kb.block_task(conn, tid, reason="waiting on operator")
        latest_blocked = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'blocked'", (tid,),
        ).fetchone()[0]

    assert _run("unblock", "--expected-blocked-event-id", str(latest_blocked + 1000), tid) == 1
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"

    assert _run("unblock", "--to-todo", "--expected-blocked-event-id", str(latest_blocked),
                "--reason", "operator answered", tid) == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "todo"
    assert f"Unblocked {tid}" in capsys.readouterr().out
