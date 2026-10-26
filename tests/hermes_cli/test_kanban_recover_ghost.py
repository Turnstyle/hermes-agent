"""Invariant tests for governed recovery of a claimless running Kanban attempt.

On an owner-managed board the recorded worker pid is the owner-submit waiter, not the agent
interpreter: a dead pid and a missing claim are never enough. Recovery also needs the owner's
evidence that the attempt is over (no unresolved admission receipt, no live bound interpreter).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_recover_ghost as ghost
from hermes_cli.process_identity import process_birth


def _absent_pid(base: int = 3_900_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


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
        assert kb.claim_task(conn, task_id, claimer=f"{kb._host_prefix()}test") is not None
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    return home, task_id, run_id


def _make_ghost(task_id, run_id):
    """The claimless running shape recover-ghost exists for: waiter gone, claim released."""
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


def _owner_event(task_id, run_id, kind, payload):
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind, payload, run_id=run_id)


def _admission(tmp_path, task_id, run_id, status, outcome=None, *, owner_pid=None, owner_birth=None):
    ledger = tmp_path / "owner-state.db"
    with closing(sqlite3.connect(ledger, isolation_level=None)) as store:
        store.execute("CREATE TABLE IF NOT EXISTS session_admissions "
                      "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
        store.execute("INSERT INTO session_admissions VALUES ('s-ghost','r-ghost',?,?)", (status, outcome))
    _owner_event(task_id, run_id, "owner_admitted", {
        "db": str(ledger), "session_id": "s-ghost", "request_id": "r-ghost",
        "pid": os.getpid() if owner_pid is None else owner_pid,
        "birth": process_birth(psutil.Process()) if owner_birth is None else owner_birth,
    })


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


def _invoke(task_id, run_id, *extra):
    parser = argparse.ArgumentParser(prog="hermes")
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "--board", "default", "recover-ghost",
                              task_id, "--run", str(run_id), "--apply", *extra])
    return cli.kanban_command(args)


def _failed(out: str) -> set[str]:
    return {line.split(": ", 2)[1] for line in out.splitlines() if line.startswith("FAIL: ")}


def test_fleet_owner_reads_installed_trigger_and_fails_closed(tmp_path, monkeypatch):
    home, task_id, run_id = _board(tmp_path, monkeypatch)
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

        conn.execute("UPDATE fleet_kanban_issue_map SET current_node='rosie' WHERE local_task_id=?", (task_id,))
        conn.commit()
        checks = ghost._checks(conn, snapshot, task_id, run_id, "default", home / "kanban.db", int(time.time()))
        owner = next(check for check in checks if check["check"] == "fleet owner")
        assert owner["result"] == "FAIL" and "foreign" in owner["reason"]

        conn.execute("DROP TRIGGER fleet_kanban_task_insert")
        conn.execute(
            "CREATE TRIGGER fleet_kanban_task_insert AFTER INSERT ON tasks BEGIN SELECT 1; END"
        )
        conn.commit()
        checks = ghost._checks(conn, snapshot, task_id, run_id, "default", home / "kanban.db", int(time.time()))
        owner = next(check for check in checks if check["check"] == "fleet owner")
        assert owner["result"] == "FAIL"
        assert "unreadable" in owner["reason"]


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
    _make_ghost(task_id, run_id)
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


@pytest.mark.parametrize("status", ["queued", "unknown"])
def test_unresolved_owner_receipt_refuses_without_writes(tmp_path, monkeypatch, capsys, status):
    """Waiter gone and claim released, but the owner may still run the admission: refuse."""
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    _admission(tmp_path, task_id, run_id, status)
    _make_ghost(task_id, run_id)
    before = _dump(home, task_id, run_id)
    assert _invoke(task_id, run_id) == 1
    out = capsys.readouterr().out
    assert _failed(out) == {"owner receipt"}
    assert "not proof" in out
    assert _dump(home, task_id, run_id) == before


def test_live_bound_interpreter_refuses_without_writes(tmp_path, monkeypatch, capsys):
    """The owner's interpreter outlives the waiter: a live worker_bound pid keeps the run."""
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    _owner_event(task_id, run_id, "worker_bound", {
        "pid": os.getpid(), "claim_lock": f"{kb._host_prefix()}test",
        "started_at": kbd._process_fingerprint(os.getpid())})
    _make_ghost(task_id, run_id)
    before = _dump(home, task_id, run_id)
    assert _invoke(task_id, run_id) == 1
    assert _failed(capsys.readouterr().out) == {"bound interpreter", "recorded worker pids"}
    assert _dump(home, task_id, run_id) == before


def test_owner_receipt_change_after_dry_run_refuses_apply(tmp_path, monkeypatch, capsys):
    """The apply re-checks under the write lock: an admission recorded after the dry run wins."""
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    _make_ghost(task_id, run_id)
    real_checks = ghost._checks
    calls = {"n": 0}

    def checks_then_admit(conn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            out = real_checks(conn, *args, **kwargs)
            _admission(tmp_path, task_id, run_id, "queued")
            return out
        return real_checks(conn, *args, **kwargs)

    monkeypatch.setattr(ghost, "_checks", checks_then_admit)
    before = _dump(home, task_id, run_id)
    assert _invoke(task_id, run_id) == 1
    assert "no recovery writes committed" in capsys.readouterr().out
    after = _dump(home, task_id, run_id)
    assert after[:2] == before[:2]  # task and run untouched; only the owner's own event landed
    assert [row for row in after[2] if "ghost_recovered" in row] == []


def test_settled_receipt_and_dead_interpreter_allow_recovery(tmp_path, monkeypatch):
    """Owner proof does not block a provably finished attempt."""
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    _admission(tmp_path, task_id, run_id, "terminal", "completed")
    _owner_event(task_id, run_id, "worker_bound", {
        "pid": _absent_pid(), "claim_lock": f"{kb._host_prefix()}test", "started_at": "1|1"})
    _make_ghost(task_id, run_id)
    assert _invoke(task_id, run_id, "--done", "--evidence", "artifact verified") == 0
    with sqlite3.connect(home / "kanban.db") as conn:
        assert conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone() == (
            "done", "artifact verified")
        assert conn.execute("SELECT status, outcome FROM task_runs WHERE id=?", (run_id,)).fetchone() == (
            "done", "reclaimed_ghost")


def test_unresolved_receipt_of_provably_dead_attempt_still_needs_expired_local_claim(tmp_path, monkeypatch, capsys):
    """owner_attempt_provably_over needs a host-local expired claim; a claimless row has none,
    so even a dead owner's unresolved receipt keeps refusing (missing proof is never proof)."""
    home, task_id, run_id = _board(tmp_path, monkeypatch)
    _admission(tmp_path, task_id, run_id, "unknown", owner_pid=_absent_pid(base=3_950_000), owner_birth="gone")
    _make_ghost(task_id, run_id)
    assert _invoke(task_id, run_id) == 1
    assert _failed(capsys.readouterr().out) == {"owner receipt"}
