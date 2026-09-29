"""A diagnostic dispatch must leave the board and worker processes untouched."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban as kb_cli


def _tables(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("tasks", "task_runs", "task_events")
    }


def _board_file():
    path = kb.kanban_db_path()
    return path.read_bytes(), path.stat().st_mtime_ns


def test_dry_run_keeps_full_board_unchanged(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    (home / "config.yaml").write_text("kanban:\n  max_spawn: 0\n", encoding="utf-8")
    worker_home = home / "profiles" / "worker-a"
    worker_home.mkdir(parents=True)
    (worker_home / "config.yaml").write_text("{}\n", encoding="utf-8")

    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker-a")
        child = kb.create_task(conn, title="child", assignee="worker-a", parents=(parent,))
        assert kb.get_task(conn, child).status == "todo"
        claimed_parent = kb.claim_task(conn, parent)
        assert claimed_parent is not None
        assert kb.complete_task(conn, parent, result="finished", expected_run_id=claimed_parent.current_run_id)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))

        dead = kb.create_task(conn, title="dead worker", assignee="worker-a")
        assert kb.claim_task(conn, dead) is not None
        ready = kb.create_task(conn, title="ready work", assignee="worker-a")
        conn.execute(
            "UPDATE tasks SET started_at = started_at - 600, claim_expires = 1, "
            "worker_pid = 999999, worker_started_at = 'missing' WHERE id = ?", (dead,),
        )
        before = _tables(conn)
        before_board_file = _board_file()

        signals = []
        real_kill = os.kill

        def kill(pid, sig):
            if sig == 0 and pid == 999999:
                raise ProcessLookupError(pid)
            if sig != 0:
                signals.append((pid, sig))
            else:
                real_kill(pid, sig)

        monkeypatch.setattr(os, "kill", kill)
        if hasattr(os, "killpg"):
            monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

        result = kbd.dispatch_once(conn, dry_run=True)
        assert _tables(conn) == before
        assert _board_file() == before_board_file
        assert signals == []

        repo = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(repo)
        cli = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", "dispatch", "--dry-run", "--json"],
            cwd=repo, env=env, capture_output=True, text=True, check=False,
        )
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["reclaim_phase"] == "skipped (dry-run)"
        assert _board_file() == before_board_file
        assert result.reclaim_phase == "skipped (dry-run)"
        assert ready in [task_id for task_id, _assignee, _workspace in result.spawned]

        args = argparse.Namespace(
            kanban_action="dispatch", board=None, dry_run=True, max=None,
            failure_limit=kbd.DEFAULT_FAILURE_LIMIT, json=True,
        )
        assert kb_cli.kanban_command(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["reclaim_phase"] == "skipped (dry-run)"
        assert "snapshot" not in payload
        assert _tables(conn) == before
        assert _board_file() == before_board_file
        assert signals == []


def test_dry_run_cli_sees_uncheckpointed_wal_row(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    board_path = kb.kanban_db_path()

    # Keep the writer connection open so the committed task remains in WAL.
    with kbc.connect_closing() as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        checkpointed_board = board_path.read_bytes()
        task_id = kb.create_task(writer, title="WAL-only ready task")
        assert board_path.read_bytes() == checkpointed_board
        assert board_path.with_name(board_path.name + "-wal").stat().st_size > 0

        repo = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(repo)
        cli = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", "dispatch", "--dry-run", "--json"],
            cwd=repo, env=env, capture_output=True, text=True, check=False,
        )
        assert cli.returncode == 0, cli.stderr
        payload = json.loads(cli.stdout)
        assert payload["reclaim_phase"] == "skipped (dry-run)"
        assert task_id in payload["skipped_unassigned"]
        assert board_path.read_bytes() == checkpointed_board


def test_normal_dispatch_still_reclaims_and_promotes(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(conn, title="child", assignee="default", parents=(parent,))
        claimed_parent = kb.claim_task(conn, parent)
        assert claimed_parent is not None
        assert kb.complete_task(conn, parent, result="finished", expected_run_id=claimed_parent.current_run_id)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        dead = kb.create_task(conn, title="dead worker", assignee="default")
        assert kb.claim_task(conn, dead) is not None
        conn.execute(
            "UPDATE tasks SET started_at = started_at - 600, claim_expires = 1, "
            "worker_pid = 999999, worker_started_at = 'missing' WHERE id = ?", (dead,),
        )

        result = kbd.dispatch_once(conn, max_spawn=0)

        assert dead in result.crashed
        assert result.promoted >= 1
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, dead).status != "running"
