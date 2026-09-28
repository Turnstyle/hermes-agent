"""Invariant tests for governed recovery of a claimless running Kanban attempt."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import time
from pathlib import Path

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_recover_ghost as ghost


def _board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The sandbox forbids ps; the invariants here are claim refusal and
    # idempotency, while production still fails closed if that probe fails.
    monkeypatch.setattr(ghost, "_process_marker_absent", lambda _task: (True, "no task marker"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ghost recovery", assignee="worker")
        assert kb.claim_task(conn, task_id, claimer=f"{socket.gethostname()}:test") is not None
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    return home, task_id, run_id


def _dump(home, task_id, run_id):
    path = home / "kanban.db"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        return tuple(
            tuple(conn.execute(sql, params).fetchall())
            for sql, params in (
                ("SELECT * FROM tasks WHERE id=?", (task_id,)),
                ("SELECT * FROM task_runs WHERE id=?", (run_id,)),
                ("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)),
            )
        )


def _invoke(task_id, run_id):
    parser = argparse.ArgumentParser(prog="hermes")
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "--board", "default", "recover-ghost",
                              task_id, "--run", str(run_id), "--apply"])
    return cli.kanban_command(args)


def test_fleet_owner_fallback_reads_installed_trigger_and_fails_closed(tmp_path, monkeypatch):
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    monkeypatch.delattr(kb, "_fleet_adapter_installed_node_id", raising=False)
    monkeypatch.delattr(kb, "_FLEET_NODE_UNPARSEABLE", raising=False)
    with kbc.connect_closing() as conn:
        conn.execute(
            "CREATE TABLE fleet_kanban_issue_map (local_task_id TEXT, issue_id TEXT, "
            "title TEXT, body TEXT, current_node TEXT, source_node TEXT)"
        )
        conn.execute(
            "INSERT INTO fleet_kanban_issue_map(local_task_id, issue_id, current_node) "
            "VALUES(?, 'issue-1', 'max')", (task_id,)
        )
        conn.execute(
            "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN "
            "INSERT INTO fleet_kanban_issue_map(local_task_id, issue_id, title, body, source_node) "
            "VALUES(NEW.id, 'issue-2', NEW.title, NEW.body, 'max'); END"
        )
        conn.commit()
        snapshot = ghost._snapshot(conn, task_id, run_id)
        checks = ghost._checks(conn, snapshot, task_id, run_id, "default", home / "kanban.db", int(time.time()))
        owner = next(check for check in checks if check["check"] == "fleet owner")
        assert owner["result"] == "PASS"

        conn.execute("DROP TRIGGER fleet_kanban_task_insert")
        conn.execute(
            "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN SELECT 1; END"
        )
        conn.commit()
        checks = ghost._checks(conn, snapshot, task_id, run_id, "default", home / "kanban.db", int(time.time()))
        owner = next(check for check in checks if check["check"] == "fleet owner")
        assert owner["result"] == "FAIL"
        assert "fleet-node-unparseable" in owner["reason"]


def test_live_worker_or_claim_refuses_without_writes(tmp_path, monkeypatch, capsys):
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid=?, claim_expires=? WHERE id=?",
                     (os.getpid(), int(time.time()) + 900, task_id))
        conn.execute("UPDATE task_runs SET worker_pid=?, claim_expires=? WHERE id=?",
                     (os.getpid(), int(time.time()) + 900, run_id))
        conn.commit()
    before = _dump(home, task_id, run_id)
    assert _invoke(task_id, run_id) != 0
    assert "FAIL" in capsys.readouterr().out
    assert _dump(home, task_id, run_id) == before


def test_already_recovered_apply_is_noop(tmp_path, monkeypatch, capsys):
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    stale = int(time.time()) - kb.DEFAULT_CLAIM_TTL_SECONDS - 10
    with kbc.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "worker_started_at=NULL, last_heartbeat_at=? WHERE id=?", (stale, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "worker_started_at=NULL, last_heartbeat_at=? WHERE id=?", (stale, run_id),
        )
        conn.commit()
    assert _invoke(task_id, run_id) == 0
    first = _dump(home, task_id, run_id)
    with sqlite3.connect(home / "kanban.db") as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "ready"
        assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == "reclaimed_ghost"
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='ghost_recovered'",
                              (task_id, run_id)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])["target_status"] == "ready"
        assert conn.execute("SELECT board_slug FROM kanban_board_identity WHERE singleton=1").fetchone()[0] == "default"
    capsys.readouterr()
    assert _invoke(task_id, run_id) == 0
    assert "already recovered" in capsys.readouterr().out
    assert _dump(home, task_id, run_id) == first
